"""Play a file that reads slowly: keep a local copy, and start once playing cannot stall.

What counts as slow is measured, never assumed from where the file is: a network drive, a
tired USB disk and a busy NAS all look the same here.

* :func:`choose` times cold reads spread across the file and compares them with what the
  video needs. There is no switch for this.
* :class:`BufferedMedia` owns the :class:`~pyatv_cast.cache.BlockCache` and a loopback HTTP
  server. A read of bytes not yet copied waits for them and makes the copy fetch them next,
  which is how seeking works, for ffmpeg and for a renderer alike.
* :meth:`BufferedMedia.wait_ready` holds playback back until what is already copied, plus
  what the source will deliver while the video plays, covers the whole video. The same
  sum is redone if playback later runs short: see :meth:`BufferedMedia.running_dry`.
"""

import asyncio
import logging
import mimetypes
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

from aiohttp import web

from pyatv_cast import cache as cache_mod
from pyatv_cast import hls, mkvindex, mp4index
from pyatv_cast.cache import BlockCache, CacheError
from pyatv_cast.errors import UserError
from pyatv_cast.sources import _in_daemon_thread

_LOGGER = logging.getLogger(__name__)

FAST_FACTOR = 6.0  # a file this many times faster than the video needs is left alone
STARTUP_SECONDS = 20.0  # always have this much video copied before starting
LOW_WATER_SECONDS = 10.0  # less than this ahead of the renderer, and playback is held
SAFETY = 0.8  # trust only this share of the measured download speed
WATCH_SECONDS = 5.0  # how often a file playing directly is checked on
KEEP_UP_FACTOR = 1.5  # a file playing directly must keep reading this much faster than the video
LOW_READINGS = 2  # this many low readings in a row, in case one was only a hiccup
# Players read the start of the file to learn what is in it before going anywhere else,
# and a renderer kept waiting for that gives up rather than waiting
HEAD_BYTES = 4 * 1024 * 1024
# A renderer told to start part way in often plays the opening for a few seconds before
# it is moved on, and one starved while doing that stops dead
OPENING_SECONDS = 10.0
MEASURE_SECONDS = 2.0
MEASURE_SPOTS = 4  # places across the file to time, so one fast patch doesn't fool it
MEASURE_SPOT_BYTES = 1024 * 1024
MEASURE_TIMEOUT = 20.0  # a source that cannot do one read in this long counts as stalled
CHUNK = 256 * 1024

Status = Callable[[int, int, Optional[float]], None]  # (have bytes, needed bytes, bytes/s)


# -- is this file slow? ------------------------------------------------------------------


def _skip_os_cache(fd: int) -> None:
    """Ask for real reads: what the OS remembers says nothing about the drive."""
    try:
        if sys.platform == "darwin":
            import fcntl

            fcntl.fcntl(fd, fcntl.F_NOCACHE, 1)
        elif hasattr(os, "posix_fadvise"):
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except OSError:
        pass


def measure_spots(size: int) -> List[int]:
    """One random place in each equal part of the file, aligned to 4 KiB."""
    if size <= MEASURE_SPOT_BYTES * MEASURE_SPOTS:
        return [0]
    part = size // MEASURE_SPOTS
    return [
        (i * part + random.randrange(0, part - MEASURE_SPOT_BYTES)) // 4096 * 4096
        for i in range(MEASURE_SPOTS)
    ]


def _measure(path: Path, spots: List[int]) -> float:
    fd = os.open(path, os.O_RDONLY)
    try:
        _skip_os_cache(fd)
        start, done = time.monotonic(), 0
        for offset in spots:
            read = 0
            while read < MEASURE_SPOT_BYTES:
                chunk = os.pread(fd, CHUNK, offset + read)
                if not chunk:
                    break
                read += len(chunk)
            done += read
            if time.monotonic() - start >= MEASURE_SECONDS:
                break
        return done / max(time.monotonic() - start, 1e-6)
    finally:
        os.close(fd)


async def measure_rate(path: Path) -> Optional[float]:
    """Bytes per second this file reads at, including the seeks a video makes.

    Returns 0 for a source that doesn't answer, None if the file can't be read.
    """
    try:
        spots = measure_spots(path.stat().st_size)
        return await asyncio.wait_for(_in_daemon_thread(_measure, path, spots), MEASURE_TIMEOUT)
    except asyncio.TimeoutError:
        return 0.0
    except OSError:
        return None


# -- when to start -----------------------------------------------------------------------


def needed_bytes(
    remaining_bytes: int, remaining_seconds: float, rate: Optional[float], bitrate: float
) -> int:
    """How many bytes must be copied ahead of the start so the video never runs dry.

    The video eats ``bitrate`` bytes a second and the drive supplies ``rate`` while it
    plays, so the shortfall over the whole remaining video has to be in hand at the start.
    """
    if rate is None:
        return remaining_bytes  # no speed measured yet: don't start on a guess
    startup = min(remaining_bytes, int(bitrate * STARTUP_SECONDS))
    deficit = bitrate - rate * SAFETY
    if deficit <= 0:
        return startup
    return int(min(remaining_bytes, max(startup, deficit * remaining_seconds)))


# -- the loopback server ffmpeg reads from -----------------------------------------------


class CacheServer:
    """Serves the cached file over HTTP on 127.0.0.1, waiting for bytes not copied yet."""

    def __init__(self, cache: BlockCache) -> None:
        """Initialize a new CacheServer."""
        self._cache = cache
        self._runner: Optional[web.AppRunner] = None
        self._name = f"media{cache.path.suffix}"
        self._mime = mimetypes.guess_type(self._name)[0] or "application/octet-stream"

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        return await self.respond(request)

    async def respond(
        self, request: web.Request, extra: Optional[dict] = None
    ) -> web.StreamResponse:
        """Answer a GET or HEAD for the video, honouring Range, with ``extra`` headers."""
        size = self._cache.size
        start, stop = 0, size  # [start, stop)
        status = 200
        try:
            spec = request.http_range
        except ValueError as ex:
            raise web.HTTPRequestRangeNotSatisfiable(
                headers={"Content-Range": f"bytes */{size}"}
            ) from ex
        if spec.start is not None or spec.stop is not None:
            start = spec.start if spec.start is not None else 0
            stop = spec.stop if spec.stop is not None else size
            if start < 0:  # a suffix range: the last N bytes
                start = max(0, size + start)
            stop = min(stop, size)
            if start >= stop:
                raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})
            status = 206
        headers = {
            **(extra or {}),
            "Accept-Ranges": "bytes", "Content-Type": self._mime,
            "Content-Length": str(stop - start),
        }  # fmt: skip
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{stop - 1}/{size}"
        response = web.StreamResponse(status=status, headers=headers)
        await response.prepare(request)
        if request.method == "HEAD":
            return response
        position = start
        began, waited = time.monotonic(), 0.0
        try:
            while position < stop:
                asked = time.monotonic()
                data = await self._cache.read(position, min(CHUNK, stop - position))
                waited += time.monotonic() - asked
                await response.write(data)
                position += len(data)
        except (ConnectionResetError, ConnectionError):  # ffmpeg stopped reading: normal
            pass
        except CacheError as ex:
            _LOGGER.error("cannot serve the video: %s", ex)
            if request.transport:
                request.transport.close()
        finally:
            _LOGGER.debug(
                "served bytes %d-%d to %s in %.1fs, %.1fs of it waiting for the drive",
                start, position, request.remote, time.monotonic() - began, waited,
            )  # fmt: skip
        return response

    async def start(self) -> str:
        """Start serving and return the URL ffmpeg should read."""
        app = web.Application()
        app.router.add_route("*", f"/{self._name}", self._handle)
        # ffmpeg can be blocked on bytes that will never come once we are stopping
        self._runner = web.AppRunner(app, access_log=None, shutdown_timeout=1.0)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        return f"http://127.0.0.1:{port}/{self._name}"

    async def stop(self) -> None:
        """Stop serving."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None


# -- the whole thing ---------------------------------------------------------------------


class BufferedMedia:
    """One video being played through a local copy."""

    def __init__(self, cache: BlockCache, server: CacheServer, url: str) -> None:
        """Use :meth:`open`."""
        self.cache = cache
        self._server = server
        self.url = url
        self.index: Optional[mp4index.Mp4Index] = None
        self._indexed = False

    @classmethod
    async def open(
        cls,
        path: Path,
        root: Path,
        workers: int = cache_mod.WORKERS,
        on_note: Optional[Callable[[str], None]] = None,
    ) -> "BufferedMedia":
        """Open the copy of ``path`` under ``root`` and start filling it."""
        cache = BlockCache.open(path, root, workers=workers, on_note=on_note)
        server = CacheServer(cache)
        try:
            url = await server.start()
        except BaseException:
            cache.close()
            raise
        cache.start()
        return cls(cache, server, url)

    @property
    def local_path(self) -> Path:
        """The copy on disk. Only whole once :attr:`cache` is complete."""
        return self.cache.path

    async def load_index(self) -> Optional[mp4index.Mp4Index]:
        """Find and read the keyframe index (``moov`` or ``Cues``) ahead of everything else."""
        if not self._indexed:
            read_at = self.cache.reader(asyncio.get_running_loop())
            size = self.cache.size
            self.index = await _in_daemon_thread(mkvindex.load, read_at, size)
            if self.index is None:
                self.index = await _in_daemon_thread(mp4index.load, read_at, size)
            self._indexed = True
        return self.index

    @property
    def respond(self) -> Callable[..., Any]:
        """Answers a web request for the video, for a server the TV connects to itself."""
        return self._server.respond

    def subtitle(self, sub: Optional[Any]) -> Optional[Any]:
        """``sub`` if it can be had without reading the whole drive, else None.

        A track inside the video is spread through all of it, so it has to wait for the
        finished copy. Once that exists it is read from there.
        """
        if sub is None or sub.sidecar is not None:
            return sub
        if self.cache.complete:
            sub.video = self.cache.path
            return sub
        return None

    async def plan(
        self, subtitle: Optional[Any] = None, on_progress: Optional[Status] = None
    ) -> "hls.MediaPlan":
        """The HLS plan for the video, readable before the copy is complete if it can be.

        With an index, ffmpeg reads the copy through the loopback server. Without one
        the keyframes can only be found by reading the whole file, so the whole copy is
        made first and reported through ``on_progress``.
        """
        index = await self.load_index()
        if index is None:
            _LOGGER.info("no keyframe index: copying the whole file before planning")
            await self.cache.wait_complete(on_progress)
            return await hls.plan(self.cache.path, subtitle)
        source = self.cache.path if self.cache.complete else self.url
        return await hls.plan(source, subtitle, index=index)

    def offset_at(self, seconds: float, duration: float) -> int:
        """The file position of ``seconds`` into the video."""
        if self.index:
            return self.index.offset_at(seconds)
        # No index to look it up in: assume an even bitrate
        return int(self.cache.size * min(seconds / max(duration, 1.0), 1.0))

    async def wait_ready(
        self,
        start: float,
        duration: float,
        on_status: Optional[Status] = None,
        opening: bool = False,
    ) -> None:
        """Wait until playing from ``start`` seconds on cannot stall for lack of data.

        ``opening``: the renderer will play the first seconds before being moved to
        ``start``, so those have to be here as well.
        """
        await self.load_index()  # an index at the end of the file is fetched ahead of play
        await self.fill_head(on_status, OPENING_SECONDS if opening else 0.0, duration)
        await self.fill_ahead(self.offset_at(start, duration), duration, on_status)

    def head_bytes(self, seconds: float = 0.0, duration: float = 1.0) -> int:
        """How much of the start of the file is read to play its first ``seconds``."""
        if seconds <= 0:
            need = HEAD_BYTES
        elif self.index:  # up to the keyframe after them: a whole group of pictures is read
            later = [at for pts, _, at in self.index.frames if pts > seconds]
            need = max(HEAD_BYTES, later[0] if later else self.cache.size)
        else:
            need = HEAD_BYTES + self.offset_at(seconds, duration)
        return min(need, self.cache.size)

    async def fill_head(
        self, on_status: Optional[Status] = None, seconds: float = 0.0, duration: float = 1.0
    ) -> None:
        """Wait for the start of the file, which is read first whatever is played."""
        cache = self.cache
        need = self.head_bytes(seconds, duration)
        cache.hint(0)
        while not cache.complete and cache.contiguous_from(0) < need:
            if cache.error:
                raise CacheError(cache.error)
            if on_status:
                on_status(cache.contiguous_from(0), need, cache.rate)
            await asyncio.sleep(0.5)

    async def fill_ahead(
        self, offset: int, duration: float, on_status: Optional[Status] = None
    ) -> None:
        """Wait until the copy ahead of ``offset`` lasts to the end at the current speed."""
        cache = self.cache
        cache.hint(offset)
        bitrate = cache.size / max(duration, 1.0)
        remaining = cache.size - offset
        while not cache.complete:
            if cache.error:
                raise CacheError(cache.error)
            have = cache.contiguous_from(offset)
            rate = cache.rate
            need = needed_bytes(remaining, remaining / bitrate, rate, bitrate)
            if self.index and rate is not None:
                need = max(need, self.lead_needed(offset, rate, duration))
            if have >= need:
                return
            if on_status:
                on_status(have, need, rate)
            await asyncio.sleep(0.5)

    def lead_needed(self, offset: int, rate: float, duration: float) -> int:
        """Bytes to have past ``offset`` so the copy, at ``rate``, keeps ahead to the end.

        Uses where each keyframe really is in the file, so a busy opening scene (often many
        times the average bitrate) is waited for at the start rather than stalled on.
        """
        frames = self.index.frames if self.index else []
        start = next((pts for pts, _, at in reversed(frames) if at <= offset), 0.0)
        points = [(pts, at) for pts, _, at in frames if at > offset]
        points.append((duration, self.cache.size))
        lead = max((at - offset) - rate * SAFETY * (pts - start) for pts, at in points)
        return int(min(self.cache.size - offset, max(0.0, lead)))

    def running_dry(self, offset: int, duration: float) -> bool:
        """Whether playback at ``offset`` is about to overtake the copy."""
        cache = self.cache
        if cache.complete:
            return False
        low = LOW_WATER_SECONDS * cache.size / max(duration, 1.0)
        return cache.contiguous_from(offset) < min(low, cache.size - offset)

    async def finish(self, on_progress: Optional[Status] = None) -> Path:
        """Copy the rest of the file now and return the local copy."""
        await self.cache.wait_complete(on_progress)
        return self.cache.path

    async def close(self) -> None:
        """Stop serving and downloading. Progress is saved for next time."""
        await self._server.stop()
        await self.cache.stop()


async def choose(
    path: Path,
    duration: Optional[float],
    root: Optional[Path] = None,
    workers: int = cache_mod.WORKERS,
    on_note: Optional[Callable[[str], None]] = None,
) -> Optional[BufferedMedia]:
    """Play ``path`` through a local copy if that is what it takes, opening it if so.

    A copy from an earlier run is always used. Otherwise it comes down to whether the file
    reads :data:`FAST_FACTOR` times faster than the video needs; a video of unknown length
    has no such figure and plays directly. If no copy can be kept, it plays directly too.
    """
    note = on_note or (lambda text: None)
    if not duration:
        return None
    root = root or cache_mod.default_cache_root()
    ready = BlockCache.peek(path, root)
    size = path.stat().st_size
    if ready == "complete":
        note("using the verified local copy from an earlier run.")
    elif ready == "partial":
        note("carrying on with the local copy from an earlier run.")
    else:
        rate = await measure_rate(path)
        bitrate = size / duration
        if rate is None or rate >= FAST_FACTOR * bitrate:
            return None
        note(
            f"this file reads at {rate / 1e6:.1f} MB/s and the video needs "
            f"{bitrate / 1e6:.1f} MB/s: buffering through a local copy."
        )
    return await _open_copy(path, root, workers, on_note)


async def _open_copy(
    path: Path, root: Path, workers: int, on_note: Optional[Callable[[str], None]]
) -> Optional[BufferedMedia]:
    """Open the copy of ``path``, or say why not and return None."""
    try:
        keep = cache_mod.cache_dir(root, path)
        size = path.stat().st_size
        cache_mod.require_cache_space(root, size, BlockCache.peek_bytes(path, root), keep)
        return await BufferedMedia.open(path, root, workers, on_note)
    except (UserError, OSError) as ex:
        if on_note:
            on_note(f"not using a local copy ({ex}); playing straight from the file.")
        return None


class Fallback:
    """For a file playing directly: notices if it stops keeping up, and opens a copy then.

    A drive that was fast when timed can slow down or stall later. Moving playback onto a
    copy costs at most a short interruption; the alternative is the video dying on the TV.
    """

    def __init__(
        self,
        path: Path,
        duration: float,
        root: Optional[Path] = None,
        workers: int = cache_mod.WORKERS,
        on_note: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Initialize a new Fallback."""
        self.path = path
        self.duration = duration
        self.root = root
        self.workers = workers
        self.media: Optional[BufferedMedia] = None
        self._note = on_note or (lambda text: None)

    async def slowed(self, stalled: Optional[Callable[[], Any]] = None) -> None:
        """Return once the file has stopped keeping up, or ``stalled()`` says playback stuck."""
        bitrate = self.path.stat().st_size / self.duration
        low = 0
        while low < LOW_READINGS:
            await asyncio.sleep(WATCH_SECONDS)
            if stalled and await stalled():
                return
            rate = await measure_rate(self.path)
            low = low + 1 if rate is not None and rate < KEEP_UP_FACTOR * bitrate else 0

    async def open(self) -> Optional[BufferedMedia]:
        """Start the copy, or None if it can't be kept."""
        self._note("this file has stopped keeping up: moving playback to a local copy.")
        root = self.root or cache_mod.default_cache_root()
        self.media = await _open_copy(self.path, root, self.workers, self._note)
        return self.media
