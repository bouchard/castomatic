"""Reading keyframes from the moov box of an MP4."""

import struct

import pytest

from pyatv_cast import mp4index


def box(kind, *parts):
    body = b"".join(parts)
    return struct.pack(">I4s", len(body) + 8, kind) + body


def full(kind, *parts, version=0):
    return box(kind, bytes([version, 0, 0, 0]), *parts)


def table(kind, fmt, rows, version=0):
    packed = b"".join(struct.pack(fmt, *row) for row in rows)
    return full(kind, struct.pack(">I", len(rows)), packed, version=version)


def video_trak(*, handler=b"vide", stbl_extra=(), wide=False, edts=None, timescale=1000, sizes=None,
               stss=None, stsc=None, mdhd=None):
    """Six samples of 100 bytes, 100 ticks each, two per chunk, keyframes at 1 and 4."""
    chunks = table(b"co64", ">Q", [(1000,), (2000,), (3000,)]) if wide else table(
        b"stco", ">I", [(1000,), (2000,), (3000,)]
    )
    stbl = box(
        b"stbl",
        table(b"stts", ">II", [(6, 100)]),
        stss if stss is not None else table(b"stss", ">I", [(1,), (4,)]),
        sizes or full(b"stsz", struct.pack(">II", 0, 6), struct.pack(">6I", *[100] * 6)),
        stsc or table(b"stsc", ">III", [(1, 2, 1)]),
        chunks,
        *stbl_extra,
    )
    mdhd = mdhd or full(b"mdhd", struct.pack(">IIII", 0, 0, timescale, 600), b"\0" * 4)
    hdlr = full(b"hdlr", b"\0" * 4, handler, b"\0" * 12)
    mdia = box(b"mdia", mdhd, hdlr, box(b"minf", stbl))
    return box(b"trak", *([edts] if edts else []), mdia)


def mvhd(timescale=1000):
    return full(b"mvhd", struct.pack(">III", 0, 0, timescale), b"\0" * 4)


def moov(*traks, header=None):
    return box(b"moov", *([header] if header is not None else [mvhd()]), *traks)


def reader(data):
    return lambda offset, length: data[offset : offset + length]


def test_keyframes_and_offsets():
    index = mp4index.parse_moov(moov(video_trak()), (5, 6))
    assert index.frames == [(0.0, 0.0, 1000), (0.3, 0.3, 2100)]
    assert index.keyframes == [(0.0, 0.0), (0.3, 0.3)]
    assert index.moov == (5, 6)
    assert [index.offset_at(t) for t in (0, 0.29, 0.3, 99)] == [1000, 1000, 2100, 2100]


def test_offset_before_the_first_keyframe_is_the_start():
    assert mp4index.Mp4Index([(1.0, 1.0, 500)]).offset_at(0.5) == 0


def test_skips_an_audio_track_and_reads_co64():
    index = mp4index.parse_moov(moov(video_trak(handler=b"soun"), video_trak(wide=True)))
    assert index.frames[1][2] == 2100


def test_uniform_sample_size():
    uniform = full(b"stsz", struct.pack(">II", 50, 6))
    assert mp4index.parse_moov(moov(video_trak(sizes=uniform))).frames[1][2] == 2050


def test_every_sample_is_a_keyframe_without_stss():
    assert len(mp4index.parse_moov(moov(video_trak(stss=b""))).frames) == 6


@pytest.mark.parametrize(
    "ctts",
    [
        table(b"ctts", ">II", [(6, 200)]),
        table(b"ctts", ">Ii", [(6, 200)], version=1),
    ],
)
def test_composition_offset_and_edit_list_shift(ctts):
    elst = box(b"edts", table(b"elst", ">Iihh", [(600, 200, 1, 0)]))
    index = mp4index.parse_moov(moov(video_trak(stbl_extra=[ctts], edts=elst)))
    # ffmpeg's convention: the first frame shows at 0 and decodes before it
    assert index.frames[0][:2] == (0.0, -0.2)


def test_unsigned_negative_composition_offsets():
    ctts = table(b"ctts", ">II", [(6, 0xFFFFFF9C)])
    assert mp4index.parse_moov(moov(video_trak(stbl_extra=[ctts]))).frames[1][0] == pytest.approx(0.2)


def test_empty_edit_delays_the_track():
    rows = struct.pack(">Iihh", 500, -1, 1, 0) + struct.pack(">Iihh", 600, 0, 1, 0)
    elst = box(b"edts", full(b"elst", struct.pack(">I", 2), rows))
    assert mp4index.parse_moov(moov(video_trak(edts=elst))).frames[0][0] == 0.5


def test_version_1_edit_list_of_only_empty_edits():
    rows = struct.pack(">QqHH", 500, -1, 1, 0)
    elst = box(b"edts", full(b"elst", struct.pack(">I", 1), rows, version=1))
    assert mp4index.parse_moov(moov(video_trak(edts=elst))).frames[0][0] == 0.5


def test_version_1_headers_and_missing_mvhd():
    mdhd = full(b"mdhd", struct.pack(">QQIQ", 0, 0, 500, 600), b"\0" * 4, version=1)
    assert mp4index.parse_moov(moov(video_trak(mdhd=mdhd))).frames[1][0] == pytest.approx(0.6)
    assert mp4index.parse_moov(moov(video_trak(), header=b"")) is not None
    header = box(b"moov", full(b"mvhd", struct.pack(">QQI", 0, 0, 600), b"\0" * 4, version=1))
    assert mp4index._movie_timescale(header, (8, len(header))) == 600


def _top_level(*boxes):
    return b"".join(boxes)


def test_load_finds_the_index_wherever_it_is():
    index = moov(video_trak())
    front = _top_level(box(b"ftyp", b"isom"), index, box(b"mdat", b"x" * 40))
    behind = _top_level(box(b"ftyp", b"isom"), box(b"mdat", b"x" * 40), index)
    for data in (front, behind):
        found = mp4index.load(reader(data), len(data))
        assert found.moov == (data.index(index), len(index)) and len(found.frames) == 2


def test_load_steps_over_a_64_bit_box():
    mdat = struct.pack(">I4sQ", 1, b"mdat", 16 + 10) + b"x" * 10
    data = box(b"ftyp", b"isom") + mdat + moov(video_trak())
    assert mp4index.load(reader(data), len(data)) is not None


@pytest.mark.parametrize(
    "data",
    [
        b"RIFFxxxxAVI LIST",
        b"short",
        box(b"ftyp", b"isom"),  # no index
        box(b"ftyp") + struct.pack(">I4s", 4, b"mdat"),  # a box smaller than its header
        box(b"ftyp") + struct.pack(">I4s", 0, b"mdat"),  # runs to the end, no index
        box(b"ftyp") + struct.pack(">I4s", 1, b"mdat"),  # 64-bit size cut off
    ],
)
def test_load_gives_up_on_anything_else(data):
    assert mp4index.load(reader(data), len(data)) is None


def test_load_gives_up_on_a_short_read_or_an_enormous_index():
    data = box(b"ftyp", b"isom") + moov(video_trak())
    head = data[:24]
    assert mp4index.load(reader(head), len(data)) is None
    huge = box(b"ftyp") + struct.pack(">I4s", mp4index.MAX_MOOV + 9, b"moov")
    assert mp4index.load(reader(huge), mp4index.MAX_MOOV * 2) is None
    assert mp4index.find_moov(lambda o, n: b"", 100) is None


def test_parse_rejects_what_it_cannot_trust():
    assert mp4index.parse_moov(box(b"free")) is None
    assert mp4index.parse_moov(b"") is None
    assert mp4index.parse_moov(moov()) is None  # no video
    assert mp4index.parse_moov(moov(box(b"trak", box(b"free")))) is None
    hollow = box(b"trak", box(b"mdia", full(b"hdlr", b"\0" * 4, b"vide", b"\0" * 12)))
    assert mp4index.parse_moov(moov(hollow)) is None
    good = video_trak()
    for marker in (b"stsc", b"stts"):  # a required table missing
        assert mp4index.parse_moov(moov(good.replace(marker, b"xxxx"))) is None


def test_parse_rejects_tables_that_do_not_add_up():
    assert mp4index.parse_moov(moov(video_trak(sizes=full(b"stsz", struct.pack(">II", 0, 99))))) is None
    good = video_trak()
    short_stts = good.replace(table(b"stts", ">II", [(6, 100)]), table(b"stts", ">II", [(2, 100)]))
    assert mp4index.parse_moov(moov(short_stts)) is None
    bad_count = good.replace(table(b"stss", ">I", [(1,), (4,)]), full(b"stss", struct.pack(">I", 99), b"\0" * 8))
    assert mp4index.parse_moov(moov(bad_count)) is None
    bad_sync = good.replace(table(b"stss", ">I", [(1,), (4,)]), table(b"stss", ">I", [(1,), (40,)]))
    assert mp4index.parse_moov(moov(bad_sync)) is None
    few_chunks = video_trak(stsc=table(b"stsc", ">III", [(1, 1, 1)]))  # 3 chunks, 6 samples
    assert mp4index.parse_moov(moov(few_chunks)) is None


def test_samples_per_chunk_can_change():
    changed = video_trak(stsc=table(b"stsc", ">III", [(1, 1, 1), (2, 5, 1)]))
    # chunk 1 holds sample 1; chunk 2 holds samples 2-6, so sample 4 is 200 bytes in
    assert mp4index.parse_moov(moov(changed)).frames[1][2] == 2200


def test_zero_length_stts_runs_and_zero_sized_boxes():
    zero = video_trak().replace(table(b"stts", ">II", [(6, 100)]), table(b"stts", ">II", [(6, 0)]))
    assert mp4index.parse_moov(moov(zero)).frames[1][0] == 0.0
    # A box of size 0 runs to the end of its parent
    tail = struct.pack(">I4s", 0, b"free") + b"\0\0"
    assert [k for k, _, _ in mp4index._boxes(tail, 0, len(tail))] == [b"free"]
    wide = struct.pack(">I4sQ", 1, b"free", 16)
    assert [k for k, _, _ in mp4index._boxes(wide, 0, len(wide))] == [b"free"]
    assert list(mp4index._boxes(wide[:12], 0, 12)) == []
    assert list(mp4index._boxes(struct.pack(">I4s", 3, b"free"), 0, 8)) == []
