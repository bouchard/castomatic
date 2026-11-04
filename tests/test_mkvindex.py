"""Tests for reading the Matroska keyframe index."""

from pyatv_cast import mkvindex as mk


def size(n):
    return (0x0100000000000000 | n).to_bytes(8, "big")


def el(element, payload):
    data = element.to_bytes((element.bit_length() + 7) // 8, "big")
    return data + size(len(payload)) + payload


def uint(n, width=4):
    return n.to_bytes(width, "big")


def tracks(video=2):
    audio = el(mk.TRACK_ENTRY, el(mk.TRACK_NUMBER, uint(1, 1)) + el(mk.TRACK_TYPE, uint(2, 1)))
    picture = el(mk.TRACK_ENTRY, el(mk.TRACK_NUMBER, uint(video, 1)) + el(mk.TRACK_TYPE, uint(1, 1)))
    return el(mk.TRACKS, audio + picture)


def cue(ms, offset, track=2):
    where = el(mk.CUE_TRACK, uint(track, 1)) + el(mk.CUE_CLUSTER_POSITION, uint(offset))
    return el(mk.CUE_POINT, el(mk.CUE_TIME, uint(ms)) + el(mk.CUE_TRACK_POSITIONS, where))


def matroska(points, scale=1_000_000, seek=True, front=b""):
    """A file with clusters of filler, the Cues last and a SeekHead pointing at them."""
    info = el(mk.INFO, el(mk.TIMESTAMP_SCALE, uint(scale)))
    head_length = len(el(mk.SEEK_HEAD, el(mk.SEEK, el(mk.SEEK_ID, uint(mk.CUES)) + el(mk.SEEK_POSITION, uint(0)))))
    body = info + tracks() + front + el(mk.CLUSTER, b"\0" * 5000)
    cues_at = (head_length if seek else 0) + len(body)
    entry = el(mk.SEEK, el(mk.SEEK_ID, uint(mk.CUES)) + el(mk.SEEK_POSITION, uint(cues_at)))
    head = el(mk.SEEK_HEAD, entry) if seek else b""
    content = head + body + el(mk.CUES, b"".join(points))
    return el(mk.EBML, b"\x42\x82\x84webm") + el(mk.SEGMENT, content)


def reader(data, log=None):
    def read_at(offset, length):
        if log is not None:
            log.append(length)
        return data[offset : offset + length]

    return read_at


def segment_start(data):
    return data.index(mk.SEGMENT.to_bytes(4, "big")) + 12


def test_reads_the_keyframes_of_the_video_track():
    data = matroska([cue(0, 100), cue(8500, 200), cue(8500, 999, track=1), cue(16000, 300)])
    index = mk.load(reader(data), len(data))
    start = segment_start(data)
    assert index.frames == [(0.0, 0.0, start + 100), (8.5, 8.5, start + 200), (16.0, 16.0, start + 300)]
    assert index.offset_at(9.0) == start + 200
    assert index.keyframes[1] == (8.5, 8.5)


def test_a_few_small_reads_are_enough():
    data = matroska([cue(0, 100)]) + b"\0" * 10_000_000
    log = []
    assert mk.load(reader(data, log), len(data)).frames
    assert sum(log) < 20_000


def test_honours_the_timestamp_scale():
    data = matroska([cue(2000, 100)], scale=500_000)
    assert mk.load(reader(data), len(data)).frames[0][0] == 1.0


def test_cues_before_the_clusters_need_no_seek_head():
    head = el(mk.CUES, cue(1000, 100))
    data = matroska([], seek=False, front=head)
    assert mk.load(reader(data), len(data)).frames[0][0] == 1.0


def test_no_index_means_none():
    empty = matroska([])
    no_seek = matroska([cue(0, 100)], seek=False)
    audio_only = matroska([cue(0, 100, track=1)])
    for data in (empty, no_seek, audio_only, b"", b"\0" * 5000, b"....ftypisom" + b"\0" * 64):
        assert mk.load(reader(data), len(data)) is None


def test_unknown_lengths_end_with_their_parent():
    point = cue(1000, 50)
    data = mk.CUES.to_bytes(4, "big") + b"\xff" + point
    header = mk._header(data, 0)  # noqa: SLF001
    assert header == (mk.CUES, 5, None)
    assert [e for e, _, _ in mk._children(data, 0, len(data))] == [mk.CUES]  # noqa: SLF001
    assert mk.parse_cues(data[5:], 0, 1_000_000, 2).frames == [(1.0, 1.0, 50)]


def test_broken_elements_are_left_alone():
    assert list(mk._children(b"\x00\x00", 0, 2)) == []  # noqa: SLF001
    assert list(mk._children(b"\xec", 0, 1)) == []  # noqa: SLF001
    assert list(mk._children(b"\xec\x88\x00", 0, 3)) == []  # noqa: SLF001
    assert mk._element_id(b"\x08\x00\x00\x00\x00", 0) is None  # noqa: SLF001
    assert mk._element_id(b"\x1a", 0) is None  # noqa: SLF001
    assert mk._size(b"\x40", 0) is None  # noqa: SLF001
    assert mk._header(b"\x1a\x45\xdf\xa3", 0) is None  # noqa: SLF001
