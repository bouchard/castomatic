"""Read the keyframe index of a Matroska or WebM file without reading the file.

The index is the ``Cues`` element, usually at the very end. The ``SeekHead`` near the start
says where it is, so a slow file needs only a few small reads, which is what lets it play
before it has been copied. Reads go through a ``read_at(offset, length)`` callable.
"""

from typing import Callable, Dict, Iterator, List, Optional, Tuple

from pyatv_cast.mp4index import Mp4Index

ReadAt = Callable[[int, int], bytes]

EBML = 0x1A45DFA3
SEGMENT = 0x18538067
SEEK_HEAD = 0x114D9B74
SEEK = 0x4DBB
SEEK_ID = 0x53AB
SEEK_POSITION = 0x53AC
INFO = 0x1549A966
TIMESTAMP_SCALE = 0x2AD7B1
TRACKS = 0x1654AE6B
TRACK_ENTRY = 0xAE
TRACK_NUMBER = 0xD7
TRACK_TYPE = 0x83
VIDEO = 1
CLUSTER = 0x1F43B675
CUES = 0x1C53BB6B
CUE_POINT = 0xBB
CUE_TIME = 0xB3
CUE_TRACK_POSITIONS = 0xB7
CUE_TRACK = 0xF7
CUE_CLUSTER_POSITION = 0xF1

HEADER = 12  # the most an element's ID and size can take
DEFAULT_SCALE = 1_000_000  # nanoseconds per timestamp unit
MAX_HEAD = 4 * 1024 * 1024  # a SeekHead, Info or Tracks bigger than this isn't one
MAX_CUES = 64 * 1024 * 1024
MAX_WALK = 256  # elements to step through looking for what comes before the first Cluster


def _element_id(data: bytes, pos: int) -> Optional[Tuple[int, int]]:
    """(ID, bytes it took) at ``pos``: IDs keep their length marker."""
    if pos >= len(data) or data[pos] == 0:
        return None
    length = 9 - data[pos].bit_length()
    if length > 4 or pos + length > len(data):
        return None
    return int.from_bytes(data[pos : pos + length], "big"), length


def _size(data: bytes, pos: int) -> Optional[Tuple[Optional[int], int]]:
    """(size, bytes it took) at ``pos``; the size is None for "until the parent ends"."""
    if pos >= len(data) or data[pos] == 0:
        return None
    length = 9 - data[pos].bit_length()
    if pos + length > len(data):
        return None
    mask = (1 << (7 * length)) - 1
    value = int.from_bytes(data[pos : pos + length], "big") & mask
    return (None if value == mask else value), length


def _header(data: bytes, pos: int) -> Optional[Tuple[int, int, Optional[int]]]:
    """(ID, payload start, payload size) of the element at ``pos``, if there is one."""
    found = _element_id(data, pos)
    if found is None:
        return None
    element, id_length = found
    size = _size(data, pos + id_length)
    if size is None:
        return None
    return element, pos + id_length + size[1], size[0]


def _children(data: bytes, start: int, end: int) -> Iterator[Tuple[int, int, int]]:
    """Yield (ID, payload start, payload end) for each element in ``data[start:end]``."""
    pos = start
    while pos < end:
        header = _header(data, pos)
        if header is None:
            return
        element, payload, size = header
        stop = end if size is None else payload + size
        if stop > end:  # corrupt or cut off
            return
        yield element, payload, stop
        pos = stop


def _uint(data: bytes, start: int, end: int) -> int:
    return int.from_bytes(data[start:end], "big")


def _find_segment(read_at: ReadAt, size: int) -> Optional[int]:
    """Where the Segment's content starts: every position in the file counts from here."""
    head = read_at(0, min(4096, size))
    header = _header(head, 0)
    if header is None or header[0] != EBML or header[2] is None:
        return None
    pos = header[1] + header[2]
    segment = _header(head, pos)
    if segment is None or segment[0] != SEGMENT:
        return None
    return segment[1]


def _walk(read_at: ReadAt, start: int, size: int) -> Dict[int, Tuple[int, int]]:
    """The Segment's top-level elements up to the first Cluster: {ID: (payload, length)}."""
    found: Dict[int, Tuple[int, int]] = {}
    pos = start
    for _ in range(MAX_WALK):
        if pos >= size:
            break
        header = _header(read_at(pos, HEADER), 0)
        if header is None or header[0] == CLUSTER or header[2] is None:
            break
        element, payload, length = header
        found.setdefault(element, (pos + payload, length))
        pos += payload + length
    return found


def _follow_seek_head(
    read_at: ReadAt, where: Tuple[int, int], segment: int, found: Dict[int, Tuple[int, int]]
) -> None:
    """Add what the SeekHead points at to ``found``."""
    if where[1] > MAX_HEAD:
        return
    data = read_at(*where)
    for element, start, end in _children(data, 0, len(data)):
        if element != SEEK:
            continue
        target = position = None
        for child, payload, stop in _children(data, start, end):
            if child == SEEK_ID:
                target = _uint(data, payload, stop)
            elif child == SEEK_POSITION:
                position = _uint(data, payload, stop)
        if target is None or position is None or target in found:
            continue
        header = _header(read_at(segment + position, HEADER), 0)
        if header is not None and header[0] == target and header[2] is not None:
            found[target] = (segment + position + header[1], header[2])


def _timestamp_scale(read_at: ReadAt, where: Optional[Tuple[int, int]]) -> int:
    if where is None or where[1] > MAX_HEAD:
        return DEFAULT_SCALE
    data = read_at(*where)
    for element, start, end in _children(data, 0, len(data)):
        if element == TIMESTAMP_SCALE:
            return _uint(data, start, end) or DEFAULT_SCALE
    return DEFAULT_SCALE


def _video_track(read_at: ReadAt, where: Optional[Tuple[int, int]]) -> Optional[int]:
    if where is None or where[1] > MAX_HEAD:
        return None
    data = read_at(*where)
    for element, start, end in _children(data, 0, len(data)):
        if element != TRACK_ENTRY:
            continue
        number = kind = None
        for child, payload, stop in _children(data, start, end):
            if child == TRACK_NUMBER:
                number = _uint(data, payload, stop)
            elif child == TRACK_TYPE:
                kind = _uint(data, payload, stop)
        if kind == VIDEO:
            return number
    return None


def parse_cues(data: bytes, segment: int, scale: int, track: int) -> Mp4Index:
    """Turn a Cues payload into the keyframes of ``track``.

    Matroska stores no decode times, so they are given as the presentation times. Those
    only differ by the few frames B-frames reorder, and only copied H.264 is cut on them.
    """
    frames: List[Tuple[float, float, int]] = []
    for element, start, end in _children(data, 0, len(data)):
        if element != CUE_POINT:
            continue
        seconds = None
        offsets = []
        for child, payload, stop in _children(data, start, end):
            if child == CUE_TIME:
                seconds = _uint(data, payload, stop) * scale / 1e9
            elif child == CUE_TRACK_POSITIONS:
                entry = dict((c, (p, s)) for c, p, s in _children(data, payload, stop))
                if CUE_TRACK in entry and CUE_CLUSTER_POSITION in entry:
                    if _uint(data, *entry[CUE_TRACK]) == track:
                        offsets.append(_uint(data, *entry[CUE_CLUSTER_POSITION]))
        if seconds is not None and offsets:
            frames.append((seconds, seconds, segment + offsets[0]))
    frames.sort()
    return Mp4Index(frames)


def load(read_at: ReadAt, size: int) -> Optional[Mp4Index]:
    """Read the keyframe index of a Matroska file through ``read_at``, or None if it can't be."""
    segment = _find_segment(read_at, size)
    if segment is None:
        return None
    found = _walk(read_at, segment, size)
    if SEEK_HEAD in found:
        _follow_seek_head(read_at, found[SEEK_HEAD], segment, found)
    track = _video_track(read_at, found.get(TRACKS))
    where = found.get(CUES)
    if track is None or where is None or where[1] > MAX_CUES:
        return None
    index = parse_cues(read_at(*where), segment, _timestamp_scale(read_at, found.get(INFO)), track)
    return index if index.frames else None
