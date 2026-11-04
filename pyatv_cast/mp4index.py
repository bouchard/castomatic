"""Read an MP4/MOV's keyframe index from its ``moov`` box, without touching the media data.

``ffprobe -show_packets`` has to read every packet of the file, which is hopeless for a
file on a slow network drive, or one that is only partly downloaded. The same facts (when
each keyframe is, and where in the file its bytes are) are in the ``moov`` box, which is
small. :func:`load` finds it with a handful of tiny reads, so it works through any
``read_at(offset, length)`` function, including one backed by a partial local copy.

Only the first video track is read. Anything unexpected returns ``None`` rather than a
wrong answer, and the caller falls back to ffprobe on a complete file.
"""

import logging
import struct
from dataclasses import dataclass, field
from typing import Callable, Iterator, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)

ReadAt = Callable[[int, int], bytes]

MAX_MOOV = 256 * 1024 * 1024  # a larger "index" is more likely a corrupt file than a real one
_FIRST_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot"}


@dataclass
class Mp4Index:
    """The video track's keyframes."""

    # (presentation time, decode time, byte offset in the file), ascending by presentation
    frames: List[Tuple[float, float, int]] = field(default_factory=list)
    moov: Tuple[int, int] = (0, 0)  # (offset, length) of the index box in the file

    @property
    def keyframes(self) -> List[Tuple[float, float]]:
        """(presentation, decode) time of every keyframe: what ``hls.keyframes`` returns."""
        return [(pts, dts) for pts, dts, _ in self.frames]

    def offset_at(self, seconds: float) -> int:
        """File offset of the last keyframe at or before ``seconds``."""
        found = 0
        for pts, _, offset in self.frames:
            if pts > seconds + 1e-6:
                break
            found = offset
        return found


def _boxes(data: bytes, start: int, end: int) -> Iterator[Tuple[bytes, int, int]]:
    """Yield (type, payload start, box end) for each box in ``data[start:end]``."""
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:  # corrupt or cut off
            return
        yield kind, pos + header, pos + size
        pos += size


def _child(data: bytes, start: int, end: int, kind: bytes) -> Optional[Tuple[int, int]]:
    for found, payload, stop in _boxes(data, start, end):
        if found == kind:
            return payload, stop
    return None


def find_moov(read_at: ReadAt, size: int) -> Optional[Tuple[int, int]]:
    """Locate the ``moov`` box by walking the top-level boxes: a few 16-byte reads.

    Returns (offset, length), or None if this is not an MP4 or has no index.
    """
    pos, first = 0, True
    while pos + 8 <= size:
        head = read_at(pos, 16)
        if len(head) < 8:
            return None
        length, kind = struct.unpack_from(">I4s", head)
        if first and kind not in _FIRST_BOXES:
            return None
        first = False
        header = 8
        if length == 1:
            if len(head) < 16:
                return None
            length = struct.unpack_from(">Q", head, 8)[0]
            header = 16
        elif length == 0:
            length = size - pos
        if length < header:
            return None
        if kind == b"moov":
            return pos, length
        pos += length
    return None


def _full_box(data: bytes, start: int) -> Tuple[int, int]:
    """(version, position after the version/flags word)."""
    return data[start], start + 4


def _handler(data: bytes, mdia: Tuple[int, int]) -> Optional[bytes]:
    hdlr = _child(data, mdia[0], mdia[1], b"hdlr")
    return data[hdlr[0] + 8 : hdlr[0] + 12] if hdlr else None


def _timescale(data: bytes, mdhd: Tuple[int, int]) -> int:
    version, pos = _full_box(data, mdhd[0])
    return struct.unpack_from(">I", data, pos + (16 if version == 1 else 8))[0]


def _movie_timescale(data: bytes, moov: Tuple[int, int]) -> int:
    mvhd = _child(data, moov[0], moov[1], b"mvhd")
    if not mvhd:
        return 1000
    version, pos = _full_box(data, mvhd[0])
    return struct.unpack_from(">I", data, pos + (16 if version == 1 else 8))[0] or 1000


def _edit_times(data: bytes, trak: Tuple[int, int], movie_ts: int) -> Tuple[int, float]:
    """(media time the track starts at, seconds of empty edit before it).

    This is how ffmpeg places the first edit: an MP4 with B-frames starts its media at
    the first frame's composition delay, so the first decode time comes out negative.
    """
    edts = _child(data, trak[0], trak[1], b"edts")
    elst = _child(data, edts[0], edts[1], b"elst") if edts else None
    if not elst:
        return 0, 0.0
    version, pos = _full_box(data, elst[0])
    count = struct.unpack_from(">I", data, pos)[0]
    pos += 4
    delay = 0.0
    for _ in range(count):
        if version == 1:
            duration, media = struct.unpack_from(">Qq", data, pos)
            pos += 20
        else:
            duration, media = struct.unpack_from(">Ii", data, pos)
            pos += 12
        if media == -1:  # an empty edit: the track starts later
            delay += duration / movie_ts
            continue
        return media, delay
    return 0, delay


def _table(data: bytes, box: Tuple[int, int], fmt: str, width: int) -> List[tuple]:
    """Rows of a box that is a full-box header, a count, then fixed-size rows."""
    _, pos = _full_box(data, box[0])
    count = struct.unpack_from(">I", data, pos)[0]
    if count * width > box[1] - pos - 4:
        raise ValueError("table runs past its box")
    return [struct.unpack_from(fmt, data, pos + 4 + i * width) for i in range(count)]


def _sample_sizes(data: bytes, stsz: Tuple[int, int]) -> List[int]:
    _, pos = _full_box(data, stsz[0])
    uniform, count = struct.unpack_from(">II", data, pos)
    if uniform:
        return [uniform] * count
    if count * 4 > stsz[1] - pos - 8:
        raise ValueError("stsz runs past its box")
    return list(struct.unpack_from(f">{count}I", data, pos + 8))


def _sample_offsets(
    sizes: List[int], chunk_offsets: List[int], stsc: List[Tuple[int, int, int]]
) -> List[int]:
    offsets: List[int] = []
    run = 0
    for chunk, base in enumerate(chunk_offsets, 1):
        while run + 1 < len(stsc) and stsc[run + 1][0] <= chunk:
            run += 1
        pos = base
        for _ in range(stsc[run][1]):
            if len(offsets) >= len(sizes):
                return offsets
            offsets.append(pos)
            pos += sizes[len(offsets) - 1]
    return offsets


def parse_moov(data: bytes, moov_at: Tuple[int, int] = (0, 0)) -> Optional[Mp4Index]:
    """Parse a whole ``moov`` box (header included) into an :class:`Mp4Index`."""
    try:
        return _parse(data, moov_at)
    except (struct.error, IndexError, ValueError, ZeroDivisionError) as ex:
        _LOGGER.debug("could not read the moov box: %s", ex)
        return None


def _parse(data: bytes, moov_at: Tuple[int, int]) -> Optional[Mp4Index]:
    top = next(_boxes(data, 0, len(data)), None)
    if not top or top[0] != b"moov":
        return None
    moov = (top[1], top[2])
    movie_ts = _movie_timescale(data, moov)
    for kind, start, stop in _boxes(data, *moov):
        if kind != b"trak":
            continue
        mdia = _child(data, start, stop, b"mdia")
        if not mdia or _handler(data, mdia) != b"vide":
            continue
        trak = (start, stop)
        mdhd = _child(data, *mdia, b"mdhd")
        minf = _child(data, *mdia, b"minf")
        stbl = _child(data, *minf, b"stbl") if minf else None
        if not (mdhd and stbl):
            return None
        return _video_index(data, trak, mdhd, stbl, movie_ts, moov_at)
    return None


def _video_index(data, trak, mdhd, stbl, movie_ts, moov_at) -> Optional[Mp4Index]:
    timescale = _timescale(data, mdhd)
    boxes = {kind: (start, stop) for kind, start, stop in _boxes(data, *stbl)}
    stts, stsz, stsc = boxes.get(b"stts"), boxes.get(b"stsz"), boxes.get(b"stsc")
    chunks = boxes.get(b"stco") or boxes.get(b"co64")
    if not (timescale and stts and stsz and stsc and chunks):
        return None
    sizes = _sample_sizes(data, stsz)
    count = len(sizes)

    decode: List[int] = []
    now = 0
    for run, delta in _table(data, stts, ">II", 8):
        decode.extend(range(now, now + run * delta, delta) if delta else [now] * run)
        now += run * delta
    if len(decode) < count:
        return None

    delay_by_sample = [0] * count
    if b"ctts" in boxes:
        version, _ = _full_box(data, boxes[b"ctts"][0])
        at = 0
        for run, offset in _table(data, boxes[b"ctts"], ">Ii" if version else ">II", 8):
            if not version and offset >= 0x80000000:  # some muxers write negatives unsigned
                offset -= 1 << 32
            delay_by_sample[at : at + run] = [offset] * len(delay_by_sample[at : at + run])
            at += run

    wide = b"co64" in boxes and b"stco" not in boxes
    chunk_offsets = [row[0] for row in _table(data, chunks, ">Q" if wide else ">I", 8 if wide else 4)]
    offsets = _sample_offsets(sizes, chunk_offsets, _table(data, stsc, ">III", 12))
    if len(offsets) < count:
        return None

    if b"stss" in boxes:
        sync = [row[0] - 1 for row in _table(data, boxes[b"stss"], ">I", 4)]
    else:
        sync = list(range(count))
    media_start, empty = _edit_times(data, trak, movie_ts)
    frames = []
    for i in sync:
        if not 0 <= i < count:
            return None
        dts = (decode[i] - media_start) / timescale + empty
        pts = (decode[i] + delay_by_sample[i] - media_start) / timescale + empty
        frames.append((pts, dts, offsets[i]))
    frames.sort()
    return Mp4Index(frames, moov_at)


def load(read_at: ReadAt, size: int) -> Optional[Mp4Index]:
    """Read the keyframe index of an MP4/MOV through ``read_at``, or None if it can't be."""
    where = find_moov(read_at, size)
    if where is None or where[1] > MAX_MOOV:
        return None
    offset, length = where
    data = read_at(offset, length)
    if len(data) < length:
        return None
    return parse_moov(data, where)
