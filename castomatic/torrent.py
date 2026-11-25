"""Play a video a torrent client is still downloading.

A torrent client fetches a file in pieces, in whatever order peers offer them (usually
rarest first), and writes each where it belongs. So a half-downloaded file is the right size
but full of holes, and a player reading straight through it plays garbage where a piece is
missing. :class:`TorrentFile` knows which parts are really there:

* With the ``.torrent`` (found in the client's own folder, or given), every piece is checked
  against its hash, so only bytes that are exactly the finished file's count.
* Without one, a part counts once the filesystem says it has been written (a sparse file's
  holes are the parts never written). That needs a client that leaves holes, which most do.

It stands in for :class:`~castomatic.cache.BlockCache`: the same reads that wait for bytes
not here yet, so :class:`~castomatic.buffering.BufferedMedia` plays it the same way, except
that nothing here can choose what is fetched next. The pieces are re-checked as they arrive.
"""

import asyncio
import hashlib
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Iterable, List, Optional, Tuple

from castomatic.errors import UserError
from castomatic.sources import _in_daemon_thread

_LOGGER = logging.getLogger(__name__)

# What clients add to a file's name until it is finished: Transmission (and browsers),
# qBittorrent, uTorrent/BitTorrent, Vuze, aria2-style ".incomplete"
PARTIAL_SUFFIXES = (".part", ".!qb", ".!ut", ".!bt", ".az!", ".incomplete")
POLL_SECONDS = 2.0  # how often new pieces are looked for
RATE_WINDOW = 30.0
RATE_MINIMUM = 4.0
STALL_SECONDS = 30.0  # nothing new for this long: the download has stopped
UNHASHED_BLOCK = 1024 * 1024  # without a .torrent, the file is judged in parts this big
MAX_TORRENT_FILE = 16 * 1024 * 1024  # a .torrent this big is not one


def _client_folders() -> List[Path]:
    """Where torrent clients keep a copy of each .torrent they are downloading."""
    home = Path.home()
    folders = [
        "Library/Application Support/Transmission/Torrents",  # Transmission, macOS
        ".config/transmission/torrents",  # Transmission, Linux
        ".config/transmission-daemon/torrents",
        "Library/Application Support/qBittorrent/BT_backup",  # qBittorrent
        ".local/share/qBittorrent/BT_backup",
        ".local/share/data/qBittorrent/BT_backup",
        "Library/Application Support/Deluge/state",  # Deluge
        ".config/deluge/state",
    ]
    found = [home / f for f in folders]
    if sys.platform.startswith("linux"):
        found.append(Path("/var/lib/transmission-daemon/.config/transmission-daemon/torrents"))
    appdata = os.environ.get("APPDATA")
    if appdata:  # Windows
        found += [Path(appdata) / "transmission" / "torrents", Path(appdata) / "qBittorrent" / "BT_backup"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        found.append(Path(local) / "qBittorrent" / "BT_backup")
    return found


def finished_name(path: Path) -> str:
    """The file's name once its download is finished (``Movie.mkv.part`` -> ``Movie.mkv``)."""
    lower = path.name.lower()
    for suffix in PARTIAL_SUFFIXES:
        if lower.endswith(suffix) and len(path.name) > len(suffix):
            return path.name[: -len(suffix)]
    return path.name


def looks_partial(path: Path) -> bool:
    """Whether the name says a download is still writing this file."""
    return finished_name(path) != path.name


def finished_if_renamed(text: str) -> str:
    """``Movie.mkv.part`` as ``Movie.mkv`` if the download has finished and been renamed."""
    path = Path(text).expanduser()
    if looks_partial(path) and not path.exists():
        done = path.with_name(finished_name(path))
        if done.is_file():
            return str(done)
    return text


# -- .torrent files ----------------------------------------------------------------------


class BencodeError(ValueError):
    """Not a valid bencoded value."""


def bdecode(data: bytes) -> Any:
    """Decode one bencoded value (a .torrent file is one dictionary)."""
    value, end = _decode(data, 0)
    if end != len(data):
        raise BencodeError("trailing data")
    return value


def _decode(data: bytes, pos: int) -> Tuple[Any, int]:
    try:
        lead = data[pos : pos + 1]
        if lead == b"i":
            end = data.index(b"e", pos)
            return int(data[pos + 1 : end]), end + 1
        if lead == b"l":
            items, pos = [], pos + 1
            while data[pos : pos + 1] != b"e":
                item, pos = _decode(data, pos)
                items.append(item)
            return items, pos + 1
        if lead == b"d":
            entries, pos = {}, pos + 1
            while data[pos : pos + 1] != b"e":
                key, pos = _decode(data, pos)
                entries[key], pos = _decode(data, pos)
            return entries, pos + 1
        colon = data.index(b":", pos)
        length = int(data[pos:colon])
        if length < 0 or colon + 1 + length > len(data):
            raise BencodeError("string runs past the end")
        return data[colon + 1 : colon + 1 + length], colon + 1 + length
    except (ValueError, IndexError) as ex:
        if isinstance(ex, BencodeError):
            raise
        raise BencodeError(str(ex)) from ex


@dataclass
class Layout:
    """Where one file sits in a torrent, and the hashes that check it."""

    torrent: Path
    piece_length: int
    hashes: bytes  # 20 bytes of SHA-1 per piece
    offset: int  # where the file starts in the torrent's run of bytes
    length: int
    # The torrent's other files as (offset, length, places on disk to look), for the pieces
    # this file shares with its neighbours
    others: List[Tuple[int, int, List[Path]]] = field(default_factory=list)

    @property
    def total(self) -> int:
        """The length of the whole torrent, every file end to end."""
        return max([self.offset + self.length] + [at + n for at, n, _ in self.others])

    @property
    def pieces(self) -> int:
        """How many pieces there are in the whole torrent."""
        return len(self.hashes) // 20


def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def layout_for(torrent: Path, path: Path, size: int) -> Optional[Layout]:
    """Where ``path`` (of ``size`` bytes) is in ``torrent``, or None if it isn't there."""
    try:
        if torrent.stat().st_size > MAX_TORRENT_FILE:
            return None
        meta = bdecode(torrent.read_bytes())
        info = meta[b"info"]
        piece_length = int(info[b"piece length"])
        hashes = info.get(b"pieces")
    except (OSError, BencodeError, KeyError, TypeError, ValueError, AttributeError):
        return None
    if not isinstance(hashes, bytes) or not hashes or len(hashes) % 20 or piece_length <= 0:
        return None  # a BitTorrent v2-only torrent: per-file hash trees, not handled here
    want = finished_name(path)
    name = _text(info.get(b"name", b""))
    if b"length" in info:  # one file
        length = int(info[b"length"])
        if name == want and length == size:
            return Layout(torrent, piece_length, hashes, 0, length)
        return None
    entries = []
    offset = 0
    for entry in info.get(b"files", []):
        parts = [_text(p) for p in entry.get(b"path.utf-8", entry.get(b"path", []))]
        length = int(entry.get(b"length", 0))
        entries.append((offset, length, parts))
        offset += length
    for at, length, parts in entries:
        if parts and parts[-1] == want and length == size:
            root = path.parent
            for _ in parts[:-1]:
                root = root.parent
            others = [
                (o, n, _places(root / Path(*p))) for o, n, p in entries if o != at and n and p
            ]
            return Layout(torrent, piece_length, hashes, at, length, others)
    return None


def _places(finished: Path) -> List[Path]:
    """Where a torrent's file may be on disk: finished, or under a partial name."""
    return [finished] + [finished.with_name(finished.name + s) for s in (".part", ".!qB", ".!ut")]


def find_layout(path: Path, size: int, extra: Iterable[Path] = ()) -> Optional[Layout]:
    """The torrent ``path`` comes from, looked for in ``extra``, beside it and in the clients'
    folders, or None."""
    candidates: List[Path] = list(extra)
    for folder in [path.parent, path.parent.parent, *_client_folders()]:
        try:
            candidates += sorted(folder.glob("*.torrent"))
        except OSError:
            continue
    seen = set()
    for torrent in candidates:
        if torrent in seen:
            continue
        seen.add(torrent)
        layout = layout_for(torrent, path, size)
        if layout:
            _LOGGER.info("pieces of %s are checked against %s", path.name, torrent)
            return layout
    return None


# -- which parts are here ----------------------------------------------------------------


def _has_hole(fd: int, start: int, stop: int) -> bool:
    """Whether the filesystem says part of [start, stop) was never written."""
    if not hasattr(os, "SEEK_HOLE"):  # pragma: no cover
        return False
    try:
        return os.lseek(fd, start, os.SEEK_HOLE) < stop
    except OSError:
        return False


def is_sparse(path: Path) -> bool:
    """Whether the file has parts never written (a download still in progress, usually)."""
    try:
        st = path.stat()
        if getattr(st, "st_blocks", None) is not None and st.st_blocks * 512 < st.st_size:
            return True
        fd = os.open(path, os.O_RDONLY)
        try:
            return _has_hole(fd, 0, st.st_size)
        finally:
            os.close(fd)
    except OSError:
        return False


class TorrentFile:
    """A file a torrent client is writing, read only where it is really finished.

    Has the reading side of :class:`~castomatic.cache.BlockCache`'s interface, so
    :class:`~castomatic.buffering.BufferedMedia` can play it.
    """

    external = True  # something else fills this: it cannot be told what to fetch

    def __init__(self, path: Path, layout: Optional[Layout], on_note: Optional[Callable[[str], None]] = None) -> None:
        """Use :meth:`open`."""
        self.source = path
        self.layout = layout
        self.size = layout.length if layout else path.stat().st_size
        self.error: Optional[str] = None
        self.suffix = Path(finished_name(path)).suffix
        self._note = on_note or (lambda text: None)
        if layout:
            self.unit = layout.piece_length
            self._first = layout.offset // layout.piece_length
            last = (layout.offset + max(layout.length, 1) - 1) // layout.piece_length
            self._shift = layout.offset - self._first * layout.piece_length  # bytes of piece 0 before us
        else:
            self.unit = UNHASHED_BLOCK
            self._first = 0
            last = (self.size - 1) // UNHASHED_BLOCK
            self._shift = 0
        self.units = last - self._first + 1
        self._have = bytearray(self.units)
        self._fd = os.open(path, os.O_RDONLY)
        self._cursor = 0
        self._cond: Optional[asyncio.Condition] = None
        self._task: Optional["asyncio.Future[None]"] = None
        self._samples: Deque[Tuple[float, int]] = deque()
        self._started = 0.0
        self._last_new = time.monotonic()
        self._mtime = -1
        self._stopping = False
        self.waiting_for: Optional[int] = None  # a file position a read is waiting for
        self.fetched = 0

    @classmethod
    def open(
        cls,
        path: Path,
        torrent: Optional[Path] = None,
        on_note: Optional[Callable[[str], None]] = None,
    ) -> "TorrentFile":
        """Open ``path``, checking it against ``torrent`` or the one found for it.

        Raises :class:`UserError` if there is no telling which parts are finished.
        """
        size = path.stat().st_size
        if size <= 0:
            raise UserError(f"{path.name} is empty: the download has not started.", "Wait for some of it to arrive.")
        layout = None
        if torrent is not None:
            layout = layout_for(torrent, path, size)
            if layout is None:
                raise UserError(
                    f"{torrent.name} does not describe {path.name} (no file of that name and size in it).",
                    "Give the .torrent this file is being downloaded from.",
                )
        else:
            layout = find_layout(path, size)
        if layout is None and not is_sparse(path):
            raise UserError(
                f"cannot tell which parts of {path.name} have downloaded.",
                "Its .torrent was not found in the torrent client's folder, and the file has no "
                "unwritten gaps to go by. Give the .torrent: `--torrent FILE`.",
            )
        tfile = cls(path, layout, on_note)
        tfile._scan()
        return tfile

    # -- positions -------------------------------------------------------------------------

    def _unit_of(self, offset: int) -> int:
        return (offset + self._shift) // self.unit

    def _unit_start(self, unit: int) -> int:
        """Where ``unit`` starts in the file (negative for a piece starting before it)."""
        return unit * self.unit - self._shift

    def _span(self, offset: int, length: int) -> Tuple[int, int]:
        end = min(self.size, offset + max(length, 1))
        return self._unit_of(offset), self._unit_of(end - 1)

    # -- what is here ----------------------------------------------------------------------

    @property
    def path(self) -> Path:
        """The file on disk. A client renames it when it finishes (``.part`` is dropped).

        Reading never depends on this: the file stays open, and an open file is the same
        file whatever it is renamed to, or wherever it is moved.
        """
        if not self.source.exists():
            done = self.source.with_name(finished_name(self.source))
            if done.exists():
                return done
        return self.source

    def _renamed(self) -> Optional[Path]:
        """The new name, if the client has renamed the file since it was opened."""
        if self.source.exists():
            return None
        done = self.path
        return done if done != self.source else None

    @property
    def complete(self) -> bool:
        return self._have.find(b"\x00") == -1

    @property
    def have_units(self) -> int:
        return self._have.count(1)

    @property
    def have_bytes(self) -> int:
        return sum(self._unit_bytes(u) for u in range(self.units) if self._have[u])

    def _unit_bytes(self, unit: int) -> int:
        start = max(0, self._unit_start(unit))
        return max(0, min(self.size, self._unit_start(unit + 1)) - start)

    def has(self, offset: int, length: int) -> bool:
        if offset >= self.size:
            return True
        first, last = self._span(offset, length)
        return self._have.find(b"\x00", first, last + 1) == -1

    def contiguous_from(self, offset: int) -> int:
        """How many bytes from ``offset`` on are finished, without a gap."""
        if offset >= self.size:
            return 0
        gap = self._have.find(b"\x00", self._unit_of(offset))
        stop = self.size if gap == -1 else max(0, self._unit_start(gap))
        return max(0, min(self.size, stop) - offset)

    def missing_runs(self, limit: int = 3) -> List[Tuple[int, int]]:
        """The first few runs of missing bytes, as (start, stop) file positions."""
        runs: List[Tuple[int, int]] = []
        at = 0
        while len(runs) < limit:
            gap = self._have.find(b"\x00", at)
            if gap == -1:
                break
            end = self._have.find(b"\x01", gap)
            end = self.units if end == -1 else end
            runs.append((max(0, self._unit_start(gap)), min(self.size, self._unit_start(end))))
            at = end
        return runs

    @property
    def rate(self) -> Optional[float]:
        """Bytes per second recently finished by the client; 0 when it has stopped."""
        now = time.monotonic()
        while len(self._samples) > 1 and now - self._samples[0][0] > RATE_WINDOW:
            self._samples.popleft()
        if not self._samples or now - self._started < RATE_MINIMUM:
            return None
        first_at, first = self._samples[0]
        span = max(now - first_at, RATE_MINIMUM)
        return max(0, self.have_bytes - first) / span

    @property
    def stalled(self) -> bool:
        """Whether nothing new has arrived for a while (the download paused or stuck)."""
        return not self.complete and time.monotonic() - self._last_new > STALL_SECONDS

    def describe(self) -> str:
        """What has downloaded, in a few words."""
        size = _format_size
        done = self.have_bytes
        what = "pieces" if self.layout else "parts"
        text = f"{100 * done / self.size:.0f}% downloaded ({self.have_units} of {self.units} {what})"
        prefix = self.contiguous_from(0)
        if not self.complete:
            text += f", {size(prefix)} of it in order from the start"
        return text

    # -- checking pieces -------------------------------------------------------------------

    def _read_torrent_bytes(self, start: int, stop: int) -> Optional[bytes]:
        """The torrent's bytes [start, stop), from this file and its neighbours."""
        layout = self.layout
        assert layout is not None
        parts = []
        for at, length, places in [(layout.offset, layout.length, None)] + layout.others:
            lo, hi = max(start, at), min(stop, at + length)
            if lo >= hi:
                continue
            if places is None:
                parts.append((lo, os.pread(self._fd, hi - lo, lo - at)))
                continue
            data = None
            for place in places:
                try:
                    with open(place, "rb") as other:
                        other.seek(lo - at)
                        data = other.read(hi - lo)
                    break
                except OSError:
                    continue
            if data is None or len(data) != hi - lo:
                return None
            parts.append((lo, data))
        parts.sort()
        joined = b"".join(d for _, d in parts)
        return joined if len(joined) == stop - start else None

    def _check(self, unit: int) -> bool:
        """Whether ``unit`` is finished (a blocking read)."""
        start = max(0, self._unit_start(unit))
        stop = min(self.size, self._unit_start(unit + 1))
        if _has_hole(self._fd, start, stop):
            return False
        layout = self.layout
        if layout is None:
            return True
        piece = self._first + unit
        begin = piece * layout.piece_length
        end = min(begin + layout.piece_length, layout.total)
        data = self._read_torrent_bytes(begin, end)
        if data is None:  # shared with a neighbour that isn't on disk: go by our part
            return True
        return hashlib.sha1(data).digest() == layout.hashes[piece * 20 : piece * 20 + 20]

    def _scan(self) -> int:
        """Check every piece not yet known to be here; returns how many arrived."""
        try:
            mtime = os.fstat(self._fd).st_mtime_ns
        except OSError:
            mtime = -2
        if mtime == self._mtime:
            return 0
        self._mtime = mtime
        found = 0
        for unit in range(self.units):
            if not self._have[unit] and self._check(unit):
                self._have[unit] = 1
                found += 1
        return found

    # -- reading ---------------------------------------------------------------------------

    @property
    def position(self) -> int:
        return max(0, self._unit_start(self._cursor))

    def hint(self, offset: int) -> None:
        """Where playback is. Nothing to fetch; remembered for :attr:`position`."""
        self._cursor = min(self.units - 1, max(0, self._unit_of(offset)))

    async def read(self, offset: int, length: int) -> bytes:
        """``length`` bytes at ``offset``, waiting until the client has finished them."""
        if offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        first, last = self._span(offset, length)
        self._cursor = first
        if self._have.find(b"\x00", first, last + 1) != -1:
            cond = self._condition()
            self.waiting_for = offset
            try:
                async with cond:
                    while self._have.find(b"\x00", first, last + 1) != -1:
                        if self._stopping:
                            raise Stopped("stopped while waiting for the download")
                        await cond.wait()
            finally:
                self.waiting_for = None
        return os.pread(self._fd, length, offset)

    def read_now(self, offset: int, length: int) -> bytes:
        """``length`` bytes at ``offset`` if finished, else raise :class:`Missing`."""
        length = max(0, min(length, self.size - offset))
        if length and not self.has(offset, length):
            raise Missing(offset, length)
        return os.pread(self._fd, length, offset)

    def reader(self, loop: asyncio.AbstractEventLoop) -> Callable[[int, int], bytes]:
        """A blocking ``read_at(offset, length)`` for code running in another thread."""

        def read_at(offset: int, length: int) -> bytes:
            return asyncio.run_coroutine_threadsafe(self.read(offset, length), loop).result()

        return read_at

    # -- following the download ------------------------------------------------------------

    def _condition(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    def start(self) -> None:
        """Begin following the download."""
        if self._task or self.complete:
            return
        self._started = time.monotonic()
        self._last_new = self._started
        self._samples.append((self._started, self.have_bytes))
        self._task = asyncio.get_running_loop().create_task(self._follow())

    async def _follow(self) -> None:
        cond = self._condition()
        noted = False
        renamed = False
        while not self.complete and not self._stopping:
            await asyncio.sleep(POLL_SECONDS)
            try:
                found = await _in_daemon_thread(self._scan)
            except OSError as ex:
                self.error = f"cannot read {self.source.name}: {ex}"
                _LOGGER.error("%s", self.error)
                found = 0
            if not renamed and self._renamed() is not None:
                renamed = True
                _LOGGER.info("%s is now %s", self.source.name, self.path.name)
            now = time.monotonic()
            self._samples.append((now, self.have_bytes))
            if found:
                self._last_new = now
                noted = False
                async with cond:
                    cond.notify_all()
            elif self.stalled and not noted:
                noted = True
                self._note(
                    f"nothing new has downloaded for {STALL_SECONDS:.0f}s: is the torrent "
                    "paused in the torrent client? Waiting for it."
                )
        if self.complete and not self._stopping:
            where = self._renamed()
            self._note(
                "the download has finished" + (f" (it is now {where.name})" if where else "")
                + "; playing on from the whole file."
            )
        async with cond:
            cond.notify_all()

    async def wait_complete(self, on_progress: Optional[Callable[[int, int, Optional[float]], None]] = None) -> None:
        """Wait until the whole file has downloaded, reporting (have, total, bytes/s)."""
        self.start()
        while not self.complete:
            if on_progress:
                on_progress(self.have_bytes, self.size, self.rate)
            await asyncio.sleep(1.0)

    async def stop(self) -> None:
        """Stop following the download."""
        self._stopping = True
        cond = self._condition()
        async with cond:
            cond.notify_all()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self.close()

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


class Stopped(ConnectionResetError):
    """Playback ended while a read was waiting for the download: whoever asked has gone."""


class Missing(Exception):
    """Bytes a reader wanted have not downloaded yet."""

    def __init__(self, offset: int, length: int) -> None:
        """Initialize a new Missing."""
        super().__init__(f"bytes {offset}-{offset + length} have not downloaded")
        self.offset = offset
        self.length = length


def _format_size(num: float) -> str:
    from castomatic.sources import format_size

    return format_size(num)


# -- what playing needs first ------------------------------------------------------------

PROBE_BYTES = 8 * 1024 * 1024  # ffprobe reads about 5 MB from the start to see what is inside


@dataclass
class Readiness:
    """What a partly downloaded video lacks before anything can be done with it."""

    missing: Optional[str] = None  # what is lacking, in words; None when it can start
    index: Any = None  # the keyframe index, once read
    kind: str = ""  # "mkv", "mp4" or "" when neither

    @property
    def ready(self) -> bool:
        return self.missing is None


def readiness(tfile: TorrentFile) -> Readiness:
    """Whether the parts of the file every player reads first are here.

    That is the start of the file (what is in it, read by ffprobe and every TV), and the
    keyframe index: MP4's ``moov``, without which an MP4 cannot be played at all, and
    Matroska's ``Cues``, without which it cannot be seeked in or cut into HLS segments.
    """
    from castomatic import mkvindex, mp4index

    size = tfile.size
    head = min(PROBE_BYTES, size)
    have = tfile.contiguous_from(0)
    if have < head:
        return Readiness(
            f"the start of the file (what is in it: {_format_size(have)} of the first "
            f"{_format_size(head)} so far)"
        )
    read_at = tfile.read_now
    try:
        start = read_at(0, 12)
    except Missing:  # pragma: no cover - the head is here
        start = b""
    if start[:4] == b"\x1a\x45\xdf\xa3":
        try:
            index = mkvindex.load(read_at, size)
        except Missing as ex:
            where = "at the end of the file" if ex.offset > size // 2 else f"at {100 * ex.offset / size:.0f}% of the file"
            return Readiness(f"the keyframe index (Matroska Cues) {where}", kind="mkv")
        return Readiness(index=index, kind="mkv")
    if start[4:8] in (b"ftyp", b"moov", b"free", b"mdat", b"wide", b"skip"):
        try:
            index = mp4index.load(read_at, size)
        except Missing as ex:
            where = "at the end of the file" if ex.offset > size // 2 else f"at {100 * ex.offset / size:.0f}% of the file"
            return Readiness(
                f"the MP4 index (moov) {where}: an MP4 cannot play at all without it", kind="mp4"
            )
        if index is None:
            return Readiness("the MP4 index (moov): it was not found where expected", kind="mp4")
        return Readiness(index=index, kind="mp4")
    return Readiness()  # some other container: the head is all that can be checked


def find_partial(path: Path, torrent: Optional[Path] = None) -> bool:
    """Whether ``path`` should be played as a download still in progress.

    A name like ``Movie.mkv.part`` says so. A file with its finished name counts when a
    .torrent for it is given, or when it has unwritten gaps and a torrent client has its
    .torrent (so a finished file is never read end to end just to check it).
    """
    if torrent is not None or looks_partial(path):
        return True
    if not is_sparse(path):
        return False
    try:
        return find_layout(path, path.stat().st_size) is not None
    except OSError:
        return False


def summary_lines(tfile: TorrentFile) -> List[str]:
    """A few lines on the download for ``castomatic explain``."""
    lines = [tfile.describe()]
    if tfile.layout:
        lines.append(f"pieces checked against {tfile.layout.torrent.name}")
    else:
        lines.append("no .torrent found: judged by which parts the client has written")
    return lines


__all__ = [
    "Layout", "Missing", "PARTIAL_SUFFIXES", "Readiness", "TorrentFile", "bdecode",
    "find_layout", "find_partial", "finished_name", "layout_for", "looks_partial", "readiness",
]  # fmt: skip
