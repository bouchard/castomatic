"""A local copy of a file that is slow to read, filled in the order the video needs it.

A movie on a network drive can read slower than the movie plays. :class:`BlockCache` keeps a
sparse local copy of such a file, divided into blocks, and fetches the blocks in this order:

1. blocks something is waiting for right now (ffmpeg reading a segment),
2. then the first missing block at or after the *cursor*, the playback position, so
   playback always has the bytes it will want next,
3. and when the end is reached, the first block still missing from the start.

So whatever order the video is watched in, including seeking around, the copy ends up
complete. It lives in a folder named after the source path, remembers which blocks it has
(so an interrupted download resumes) and is trusted for a later run only if the source is
still exactly the same size.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Set, Tuple

from castomatic.errors import UserError
from castomatic.sources import _in_daemon_thread

try:  # not on Windows
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

BLOCK_SIZE = 512 * 1024
WORKERS = 4  # measured on an SMB share: 0.6 MB/s with 1, about 0.8 with 2, 0.9-1.8 with 4
META_NAME = "cache.json"
LOCK_NAME = "cache.lock"
SAVE_INTERVAL = 5.0  # seconds between saves of which blocks we have
RATE_WINDOW = 30.0  # seconds of history the download speed is averaged over
RATE_MINIMUM = 3.0  # seconds of download before the speed is worth quoting
RETRY_DELAYS = (0.5, 1, 2, 4, 8, 15, 30)  # then the download gives up
SLOW_NOTE_AFTER = 4.0  # a read waiting this long makes the CLI say so
Notify = Callable[[str], None]


class CacheError(UserError):
    """The source could not be read, so the copy cannot be completed."""

    def __init__(self, message: str) -> None:
        """Initialize a new CacheError."""
        super().__init__(
            message, "Is the drive still connected? Playing again carries on from the same place."
        )


def default_cache_root() -> Path:
    """Where copies are kept: our folder in the system's temporary folder, one per user."""
    owner = os.getuid() if hasattr(os, "getuid") else "user"  # the temporary folder is shared
    return Path(tempfile.gettempdir()) / f"castomatic-cache-{owner}" / "files"


def cache_dir(root: Path, source: Path) -> Path:
    """The folder for ``source``'s copy. The same path always gives the same folder."""
    full = os.path.abspath(os.path.expanduser(str(source)))
    key = hashlib.sha256(full.encode()).hexdigest()[:16]
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(full).stem)[:40].strip("_") or "video"
    return root / f"{label}-{key}"


def _ranges(have: bytearray) -> List[List[int]]:
    """The blocks we have, as [first, last + 1] runs."""
    runs: List[List[int]] = []
    for i, flag in enumerate(have):
        if flag:
            if runs and runs[-1][1] == i:
                runs[-1][1] = i + 1
            else:
                runs.append([i, i + 1])
    return runs


def _close_quietly(fd: int) -> None:
    """Close ``fd`` in the background: closing a file on a hung drive can hang too."""

    def close() -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    threading.Thread(target=close, daemon=True).start()


class BlockCache:
    """A resumable, on-demand local copy of one file."""

    def __init__(
        self,
        source: Path,
        directory: Path,
        size: int,
        workers: int = WORKERS,
        block_size: int = BLOCK_SIZE,
        on_note: Optional[Notify] = None,
    ) -> None:
        """Use :meth:`open`, which checks any earlier copy first."""
        self.source = source
        self.directory = directory
        self.size = size
        self.block_size = block_size
        self.blocks = max(1, -(-size // block_size))
        self.workers = workers
        self.path = directory / f"video{source.suffix or '.mp4'}"
        self.error: Optional[str] = None
        self._note = on_note or (lambda text: None)
        self._have = bytearray(self.blocks)
        self._cursor = 0  # block the playback is at
        self._demand: Dict[int, int] = {}  # block -> how many readers wait for it
        self._inflight: Set[int] = set()
        self._cond: Optional[asyncio.Condition] = None
        self._tasks: List["asyncio.Future[None]"] = []
        self._fd = -1
        self._lock_fd = -1
        self._dirty = False
        self._stopping = False
        self._samples: Deque[Tuple[float, int]] = deque()
        self._started = 0.0
        self._last_slow_note = -1e9
        self.fetched = 0  # bytes read from the source by this run

    # -- opening ---------------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        source: Path,
        root: Path,
        workers: int = WORKERS,
        block_size: int = BLOCK_SIZE,
        on_note: Optional[Notify] = None,
    ) -> "BlockCache":
        """Open (or start) the copy of ``source``, discarding an old one of a different size.

        Raises :class:`CacheError` if the folder cannot be used (including when another
        castomatic is already filling the same copy).
        """
        size = source.stat().st_size
        if size <= 0:
            raise CacheError(f"{source} is empty")
        cache = cls(source, cache_dir(root, source), size, workers, block_size, on_note)
        try:
            cache.directory.mkdir(parents=True, exist_ok=True)
            cache._lock()
            cache._load()
            cache._open_data()
        except OSError as ex:
            cache.close()
            raise CacheError(f"cannot use the cache folder {cache.directory}: {ex}") from ex
        except CacheError:
            cache.close()
            raise
        return cache

    @staticmethod
    def _saved(source: Path, root: Path) -> Optional[dict]:
        """The saved map for ``source`` if it is for a file of exactly this size, else None."""
        directory = cache_dir(root, source)
        try:
            meta = json.loads((directory / META_NAME).read_text())
            data = directory / f"video{source.suffix or '.mp4'}"
            if meta["size"] != source.stat().st_size or data.stat().st_size != meta["size"]:
                return None
            return meta
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @classmethod
    def peek(cls, source: Path, root: Path) -> Optional[str]:
        """``"complete"`` or ``"partial"`` if a usable copy exists, else None. Opens nothing."""
        meta = cls._saved(source, root)
        if meta is None:
            return None
        return "complete" if meta.get("complete") else "partial"

    @classmethod
    def peek_bytes(cls, source: Path, root: Path) -> int:
        """Roughly how many bytes a usable earlier copy already holds."""
        meta = cls._saved(source, root)
        try:
            blocks = sum(stop - first for first, stop in meta["have"]) if meta else 0
            return min(source.stat().st_size, blocks * meta["block_size"]) if meta else 0
        except (KeyError, TypeError, ValueError, OSError):
            return 0

    def _lock(self) -> None:
        self._lock_fd = os.open(self.directory / LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
        if fcntl is None:  # pragma: no cover
            return
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as ex:
            raise CacheError("another castomatic is already using this video's cache") from ex

    def _load(self) -> None:
        """Read the saved block map, if it still describes this source and data file."""
        meta_path = self.directory / META_NAME
        try:
            meta = json.loads(meta_path.read_text())
            valid = (
                meta["size"] == self.size
                and meta["block_size"] == self.block_size
                and self.path.stat().st_size == self.size
            )
            runs = meta["have"]
        except (OSError, ValueError, KeyError, TypeError):
            valid, runs = False, []
        if valid:
            try:
                for first, stop in runs:
                    if not 0 <= first <= stop <= self.blocks:
                        raise ValueError("block run out of range")
                    self._have[first:stop] = b"\x01" * (stop - first)
            except (ValueError, TypeError):
                valid = False
        if not valid:
            if meta_path.exists() or self.path.exists():
                _LOGGER.info("discarding the old copy of %s: it does not match", self.source)
            self._have = bytearray(self.blocks)
            for stale in (meta_path, self.path):
                stale.unlink(missing_ok=True)

    def _open_data(self) -> None:
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        if os.fstat(self._fd).st_size != self.size:
            os.ftruncate(self._fd, self.size)  # sparse: takes no space until written
        self._save()

    # -- what we have ----------------------------------------------------------------------

    @property
    def complete(self) -> bool:
        """Whether every block is here."""
        return self.have_blocks == self.blocks

    @property
    def have_blocks(self) -> int:
        return self._have.count(1)

    @property
    def have_bytes(self) -> int:
        count = self.have_blocks
        if count and self._have[-1]:  # the last block may be short
            return (count - 1) * self.block_size + (self.size - (self.blocks - 1) * self.block_size)
        return count * self.block_size

    def _span(self, offset: int, length: int) -> Tuple[int, int]:
        end = min(self.size, offset + max(length, 1))
        return offset // self.block_size, (end - 1) // self.block_size

    def has(self, offset: int, length: int) -> bool:
        """Whether those bytes are all in the copy."""
        if offset >= self.size:
            return True
        first, last = self._span(offset, length)
        return self._have.find(b"\x00", first, last + 1) == -1

    def contiguous_from(self, offset: int) -> int:
        """How many bytes, starting at ``offset``, are already in the copy."""
        if offset >= self.size:
            return 0
        block = offset // self.block_size
        gap = self._have.find(b"\x00", block)
        stop = self.size if gap == -1 else gap * self.block_size
        return max(0, stop - offset)

    @property
    def rate(self) -> Optional[float]:
        """Bytes per second recently read from the source, or None before there is a sample."""
        now = time.monotonic()
        while self._samples and now - self._samples[0][0] > RATE_WINDOW:
            self._samples.popleft()
        if not self._samples or self._stopping:
            return None
        span = min(RATE_WINDOW, now - self._started)
        if span < RATE_MINIMUM:
            return None
        return sum(n for _, n in self._samples) / span

    # -- reading ---------------------------------------------------------------------------

    @property
    def position(self) -> int:
        """Where in the file the latest read or hint was."""
        return self._cursor * self.block_size

    def hint(self, offset: int) -> None:
        """Say where playback is: fetching continues from here once demand is met."""
        self._cursor = min(self.blocks - 1, max(0, offset // self.block_size))

    async def read(self, offset: int, length: int) -> bytes:
        """Return ``length`` bytes at ``offset``, waiting for them to arrive if need be."""
        if offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        first, last = self._span(offset, length)
        self._cursor = first
        if self._have.find(b"\x00", first, last + 1) != -1:
            await self._wait_for(first, last)
        return os.pread(self._fd, length, offset)

    async def _wait_for(self, first: int, last: int) -> None:
        cond = self._condition()
        wanted = [b for b in range(first, last + 1) if not self._have[b]]
        for b in wanted:
            self._demand[b] = self._demand.get(b, 0) + 1
        started = time.monotonic()
        try:
            async with cond:
                cond.notify_all()
                while True:
                    if self.error:
                        raise CacheError(self.error)
                    if self._have.find(b"\x00", first, last + 1) == -1:
                        return
                    try:
                        await asyncio.wait_for(cond.wait(), SLOW_NOTE_AFTER)
                    except asyncio.TimeoutError:
                        self._maybe_note(time.monotonic() - started)
        finally:
            for b in wanted:
                if self._demand.get(b, 0) <= 1:
                    self._demand.pop(b, None)
                else:
                    self._demand[b] -= 1

    def _maybe_note(self, waited: float) -> None:
        now = time.monotonic()
        if now - self._last_slow_note >= 30:
            self._last_slow_note = now
            self._note(f"waiting for the video to arrive from the drive ({waited:.0f}s so far) ...")

    def reader(self, loop: asyncio.AbstractEventLoop) -> Callable[[int, int], bytes]:
        """A blocking ``read_at(offset, length)`` for code running in another thread."""

        def read_at(offset: int, length: int) -> bytes:
            return asyncio.run_coroutine_threadsafe(self.read(offset, length), loop).result()

        return read_at

    # -- fetching --------------------------------------------------------------------------

    def _condition(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    def start(self) -> None:
        """Begin filling the copy in the background (nothing to do if it is complete)."""
        if self.complete or self._tasks:
            return
        self._started = time.monotonic()
        self._stopping = False
        loop = asyncio.get_running_loop()
        self._condition()
        self._tasks = [loop.create_task(self._worker()) for _ in range(self.workers)]
        self._tasks.append(loop.create_task(self._saver()))

    def _next_block(self) -> Optional[int]:
        """The block to fetch next: demanded ones first, then onward from the cursor."""
        for block in self._demand:
            if not self._have[block] and block not in self._inflight:
                return block
        for begin, stop in ((self._cursor, self.blocks), (0, self._cursor)):
            at = begin
            while True:
                at = self._have.find(b"\x00", at, stop)
                if at == -1:
                    break
                if at not in self._inflight:
                    return at
                at += 1
        return None

    def _read_source(self, fd: int, block: int) -> bytes:
        """Blocking read of one block (runs in a thread)."""
        offset = block * self.block_size
        want = min(self.block_size, self.size - offset)
        parts: List[bytes] = []
        while want > 0:
            chunk = os.pread(fd, want, offset)
            if not chunk:
                raise CacheError(f"{self.source} ended early: it has changed since it was opened")
            parts.append(chunk)
            offset += len(chunk)
            want -= len(chunk)
        return b"".join(parts)

    async def _worker(self) -> None:
        cond = self._condition()
        source_fd = -1
        failures = 0
        try:
            while not self._stopping:
                block = self._next_block()
                if block is None:
                    if self.complete:
                        return
                    async with cond:  # the rest is being fetched by the other workers
                        try:
                            await asyncio.wait_for(cond.wait(), 1.0)
                        except asyncio.TimeoutError:
                            pass
                    continue
                self._inflight.add(block)
                try:
                    if source_fd < 0:
                        source_fd = await _in_daemon_thread(os.open, str(self.source), os.O_RDONLY)
                    data = await _in_daemon_thread(self._read_source, source_fd, block)
                    os.pwrite(self._fd, data, block * self.block_size)
                except (OSError, CacheError) as ex:
                    self._inflight.discard(block)
                    if source_fd >= 0:
                        _close_quietly(source_fd)
                        source_fd = -1
                    if failures >= len(RETRY_DELAYS):
                        await self._fail(f"cannot read {self.source}: {ex}")
                        return
                    self._maybe_note_retry(ex, failures)
                    await asyncio.sleep(RETRY_DELAYS[failures])
                    failures += 1
                    continue
                failures = 0
                self._inflight.discard(block)
                self._have[block] = 1
                self._dirty = True
                self.fetched += len(data)
                self._samples.append((time.monotonic(), len(data)))
                async with cond:
                    cond.notify_all()
        finally:
            if source_fd >= 0:
                _close_quietly(source_fd)

    def _maybe_note_retry(self, ex: BaseException, failures: int) -> None:
        if failures >= 2:
            self._note(f"the drive is not answering ({ex}); retrying ...")

    async def _fail(self, message: str) -> None:
        self.error = message
        cond = self._condition()
        async with cond:
            cond.notify_all()

    async def _saver(self) -> None:
        while not self._stopping:
            await asyncio.sleep(SAVE_INTERVAL)
            if self._dirty:
                self._save()

    def _save(self) -> None:
        """Write which blocks we have. The data goes to disk first, so the map never lies."""
        if self._fd < 0:
            return
        try:
            os.fsync(self._fd)
            meta = {
                "source": str(self.source),
                "size": self.size,
                "block_size": self.block_size,
                "complete": self.complete,
                "have": _ranges(self._have),
            }
            part = self.directory / (META_NAME + ".part")
            part.write_text(json.dumps(meta))
            part.replace(self.directory / META_NAME)
            self._dirty = False
        except OSError as ex:
            _LOGGER.warning("could not save the cache map: %s", ex)

    async def wait_complete(self, on_progress: Optional[Callable[[int, int, Optional[float]], None]] = None) -> None:
        """Wait until the whole file is here, reporting (have, total, bytes/s) each second."""
        self.start()
        while not self.complete:
            if self.error:
                raise CacheError(self.error)
            if on_progress:
                on_progress(self.have_bytes, self.size, self.rate)
            await asyncio.sleep(1.0)
        self._save()

    async def stop(self) -> None:
        """Stop fetching and save the block map. The copy stays for next time."""
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self.close()

    def close(self) -> None:
        """Save and release the files."""
        if self._fd >= 0:
            self._save()
            os.close(self._fd)
            self._fd = -1
        if self._lock_fd >= 0:
            os.close(self._lock_fd)
            self._lock_fd = -1


def _free_space(root: Path) -> int:
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _last_used(folder: Path) -> float:
    try:
        return (folder / META_NAME).stat().st_mtime
    except OSError:
        return 0.0


def _in_use(folder: Path) -> bool:
    """Whether a running castomatic holds this copy."""
    if fcntl is None:  # pragma: no cover
        return False
    try:
        fd = os.open(folder / LOCK_NAME, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def _evict(root: Path, needed: float, keep: Optional[Path]) -> None:
    """Delete the least recently used copies until ``needed`` bytes are free."""
    if not root.is_dir():
        return
    folders = [p for p in root.iterdir() if p.is_dir() and p != keep]
    for folder in sorted(folders, key=_last_used):
        if _free_space(root) >= needed:
            return
        if not _in_use(folder):
            _LOGGER.info("removing the old copy %s to make room", folder.name)
            shutil.rmtree(folder, ignore_errors=True)


def require_cache_space(
    root: Path, source_size: int, already: int = 0, keep: Optional[Path] = None
) -> None:
    """Make sure the rest of the file fits, removing old copies (never ``keep``) if needed."""
    needed = (source_size - already) * 1.1 + 100 * 1024 * 1024
    if _free_space(root) < needed:
        _evict(root, needed, keep)
    free = _free_space(root)
    if free < needed:
        raise UserError(
            f"not enough free space in {root} for a copy of the video "
            f"({source_size / 1e9:.1f} GB needed, {free / 1e9:.1f} GB free)."
        )
