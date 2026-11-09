"""Stream a yt-dlp video through a local buffer, and re-stream it to the TV once it is safe.

yt-dlp (its own downloader, so signed addresses, DASH, HLS and live streams all work) writes
an MPEG-TS stream to a growing file. Playback starts only when enough has been buffered that
it cannot run dry, and a small HTTP server then sends the file to the TV, following it as it
grows. Nothing here asks ffmpeg to open the site's address, so sites that refuse ffmpeg
(HTTP 403) are not a problem.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional

from aiohttp import web

from castomatic import stream
from castomatic.errors import UserError
from castomatic.sources import MediaSource, _clean

_LOGGER = logging.getLogger(__name__)

LIVE_LEAD = 20.0  # seconds of a live stream to hold back before playing
MIN_LEAD = 10.0  # seconds of any video to hold back before playing
TRUST = 0.8  # how much of the measured download speed to count on
POLL = 1.0
START_TIMEOUT = 120.0  # how long to wait for the first bytes from yt-dlp
CHUNK = 65536


def lead_needed(duration: Optional[float], rate: float) -> float:
    """Seconds of video that must be buffered before playing cannot run dry.

    ``rate`` is how many seconds of video arrive per second. A live stream (``duration`` is
    None) can only ever arrive in real time, so it gets a fixed cushion. A finished video
    needs the cushion, plus whatever the connection will not deliver while it plays.
    """
    if duration is None:
        return LIVE_LEAD
    shortfall = duration * max(0.0, 1.0 - TRUST * rate)
    return min(duration, max(MIN_LEAD, shortfall))


def ytdlp_command(source: MediaSource) -> List[str]:
    """The yt-dlp command that writes the video, as MPEG-TS, to stdout."""
    cmd = [
        sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--no-progress",
        "--retries", "10", "--fragment-retries", "10", "-f", source.ytdlp_format, "-o", "-",
        # Video and audio are separate live playlists that rarely begin at the same moment.
        # ffmpeg normally starts each at zero, hiding the gap and playing the audio seconds
        # early; -copyts keeps their real timestamps so they stay in step.
        "--downloader-args", "ffmpeg_i:-copyts",
    ]  # fmt: skip
    if source.cookies_from_browser:
        cmd += ["--cookies-from-browser", source.cookies_from_browser]
    return cmd + [source.page_url]


class YtdlpBuffer:
    """yt-dlp writing a video into ``path`` while it is being watched."""

    def __init__(self, source: MediaSource, directory: Path) -> None:
        """Initialize a new YtdlpBuffer."""
        self.source = source
        self.path = directory / "buffer.ts"
        self.size = 0
        self.finished = asyncio.Event()  # yt-dlp has stopped, however it ended
        self.error: Optional[str] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._tasks: List["asyncio.Task[Any]"] = []
        self._stderr = b""

    async def start(self) -> None:
        """Start yt-dlp."""
        cmd = ytdlp_command(self.source)
        _LOGGER.debug("yt-dlp: %s", cmd)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"")
        # Its own process group, so ffmpeg children go with it when we stop
        self._proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )  # fmt: skip
        self._tasks = [asyncio.ensure_future(self._pump()), asyncio.ensure_future(self._errors())]

    async def _errors(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        self._stderr = await self._proc.stderr.read()

    async def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            with self.path.open("ab", buffering=0) as out:
                while chunk := await self._proc.stdout.read(CHUNK):
                    out.write(chunk)
                    self.size += len(chunk)
            code = await self._proc.wait()
            await asyncio.sleep(0)  # let the error reader finish
            if code not in (0, -signal.SIGTERM, -signal.SIGKILL):
                text = _clean(self._stderr.decode(errors="replace"))
                self.error = text if text != "no details" else f"yt-dlp exited with {code}"
        except OSError as ex:
            self.error = f"could not write the buffer: {ex.strerror or ex}"
            self._kill()
        finally:
            self.finished.set()

    async def seconds_buffered(self) -> Optional[float]:
        """How much video is in the buffer, or None if it cannot be told yet."""
        if self.size < CHUNK:
            return None
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json",
            str(self.path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        out, _ = await proc.communicate()
        try:
            return float(json.loads(out)["format"]["duration"])
        except (ValueError, KeyError, TypeError):
            return None

    async def wait_ready(
        self,
        start: float = 0.0,
        on_status: Optional[Callable[[float, float, float], None]] = None,
        timeout: float = START_TIMEOUT,
        poll: float = POLL,
    ) -> None:
        """Wait until enough is buffered to play from ``start`` seconds without stopping.

        ``on_status`` hears (seconds buffered, seconds wanted, bytes per second).
        """
        loop = asyncio.get_running_loop()
        began = loop.time()
        first: Optional[tuple] = None  # (time, seconds buffered) at the first reading
        while True:
            have = await self.seconds_buffered()
            now = loop.time()
            if have is not None:
                first = first or (now, have)
                rate = (have - first[1]) / (now - first[0]) if now > first[0] else 0.0
                want = start + lead_needed(self.source.duration, rate)
                if on_status:
                    on_status(have, want, self.size / max(now - began, 1e-6))
                if have >= want:
                    return
            if self.finished.is_set():
                if self.error:
                    raise self._failure()
                if self.size:  # a short video that is all here already
                    return
                raise self._failure()
            if self.size == 0 and now - began > timeout:
                await self.stop()
                raise UserError(
                    f"yt-dlp did not deliver any video within {timeout:.0f} seconds.",
                    "The site may be slow or blocking it. Try again, or add "
                    "`--cookies-from-browser chrome`, or use `--download-first`.",
                )
            await asyncio.sleep(poll)

    def _failure(self) -> UserError:
        return UserError(
            f"yt-dlp could not stream {self.source.title!r}: {self.error or 'no video came out'}",
            "If the site needs you to be logged in, use your browser's cookies: "
            "`--cookies-from-browser chrome`. Sites change often, so upgrading yt-dlp fixes many "
            "failures. Use `--download-first` to fetch the whole video first instead.",
        )

    def _kill(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                os.killpg(self._proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                self._proc.kill()

    async def stop(self) -> None:
        """Stop yt-dlp and everything it started."""
        self._kill()
        if self._proc:
            await self._proc.wait()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self.finished.set()


class BufferServer:
    """HTTP server on this computer that feeds a ``YtdlpBuffer`` to ffmpeg, following it as it grows.

    Every request starts at the beginning of the buffer. ``requests`` counts them, which
    tells "the TV never connected" (a firewall) from "it connected but could not play it".
    """

    def __init__(self, buffer: YtdlpBuffer, address: str, port: int = 0) -> None:
        """Initialize a new BufferServer."""
        self._buffer = buffer
        self._address = address
        self._port = port
        self._runner: Optional[web.AppRunner] = None
        self.requests = 0
        self.last_error: Optional[str] = None
        self.mime = stream.TS_MIMES[0]

    async def _send(self, request: web.Request) -> web.StreamResponse:
        self.requests += 1
        _LOGGER.debug("%s %s from %s", request.method, request.path, request.remote)
        response = web.StreamResponse(headers={
            "Content-Type": self.mime,
            "transferMode.dlna.org": "Streaming",
            "contentFeatures.dlna.org": stream.STREAM_FLAGS,
        })  # fmt: skip
        response.enable_chunked_encoding()
        await response.prepare(request)
        if request.method == "HEAD":
            return response
        buf = self._buffer
        sent = 0
        try:
            with buf.path.open("rb") as source:
                while True:
                    done = buf.finished.is_set()  # looked at before reading, so no tail is missed
                    chunk = source.read(CHUNK)
                    if chunk:
                        await response.write(chunk)
                        sent += len(chunk)
                    elif done:
                        break
                    else:
                        await asyncio.sleep(0.1)
            if buf.error:
                self.last_error = buf.error
            await response.write_eof()
        except ConnectionError:
            _LOGGER.debug("renderer closed the stream after %d bytes", sent)
        return response

    async def start(self) -> str:
        """Start serving and return the stream URL."""
        app = web.Application()
        app.router.add_route("*", "/stream.ts", self._send)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._address, self._port)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        return f"http://{self._address}:{port}/stream.ts"

    async def stop(self) -> None:
        """Stop serving."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

