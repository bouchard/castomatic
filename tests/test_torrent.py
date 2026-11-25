"""Playing a file a torrent client is still downloading."""

import asyncio
import hashlib
import os

import pytest

from castomatic import buffering, torrent
from castomatic.errors import UserError
from tests.test_mkvindex import cue, matroska
from tests.test_mp4index import box, moov, video_trak

PIECE = 16 * 1024


def bencode(value):
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(v) for k, v in sorted(value.items())) + b"e"


def hashes(data, piece=PIECE):
    return b"".join(hashlib.sha1(data[i : i + piece]).digest() for i in range(0, len(data), piece))


def single(tmp_path, name, data, piece=PIECE, where=None):
    meta = {"info": {"name": name, "length": len(data), "piece length": piece, "pieces": hashes(data, piece)}}
    path = (where or tmp_path) / f"{name}.torrent"
    path.write_bytes(bencode(meta))
    return path


def partial(path, data, pieces, piece=PIECE):
    """Write ``data`` as a client would with only ``pieces`` downloaded: holes elsewhere."""
    with open(path, "wb") as out:
        out.truncate(len(data))
        for p in pieces:
            out.seek(p * piece)
            out.write(data[p * piece : (p + 1) * piece])
    return path


def fill(path, data, pieces, piece=PIECE):
    with open(path, "r+b") as out:
        for p in pieces:
            out.seek(p * piece)
            out.write(data[p * piece : (p + 1) * piece])


def payload(n_pieces, last=1000):
    return bytes((i * 7 + i // 300) % 251 + 1 for i in range(n_pieces * PIECE + last))


holes = pytest.mark.skipif(not hasattr(os, "SEEK_HOLE"), reason="no SEEK_HOLE here")


# -- names and .torrent files --------------------------------------------------------------


@pytest.mark.parametrize(
    "name, finished",
    [
        ("Movie.mkv.part", "Movie.mkv"),
        ("Movie.mkv.!qB", "Movie.mkv"),
        ("Movie.mp4.incomplete", "Movie.mp4"),
        ("Movie.mkv", "Movie.mkv"),
        (".part", ".part"),
    ],
)
def test_finished_name(tmp_path, name, finished):
    assert torrent.finished_name(tmp_path / name) == finished
    assert torrent.looks_partial(tmp_path / name) == (name != finished)


def test_bdecode():
    assert torrent.bdecode(b"d1:ai-3e1:bl2:xyee") == {b"a": -3, b"b": [b"xy"]}
    for bad in (b"", b"i3", b"5:ab", b"d1:a", b"x", b"i1ei2e", b"-1:a"):
        with pytest.raises(torrent.BencodeError):
            torrent.bdecode(bad)


def test_layout_of_a_single_file_torrent(tmp_path):
    data = payload(3)
    meta = single(tmp_path, "Movie.mkv", data)
    layout = torrent.layout_for(meta, tmp_path / "Movie.mkv.part", len(data))
    assert (layout.offset, layout.length, layout.pieces, layout.total) == (0, len(data), 4, len(data))
    assert torrent.layout_for(meta, tmp_path / "Other.mkv.part", len(data)) is None
    assert torrent.layout_for(meta, tmp_path / "Movie.mkv.part", len(data) + 1) is None


def test_torrents_that_cannot_be_used(tmp_path, monkeypatch):
    v2 = tmp_path / "v2.torrent"
    v2.write_bytes(bencode({"info": {"name": "Movie.mkv", "length": 10, "piece length": PIECE}}))
    junk = tmp_path / "junk.torrent"
    junk.write_bytes(b"not bencode")
    for path in (v2, junk, tmp_path / "absent.torrent"):
        assert torrent.layout_for(path, tmp_path / "Movie.mkv.part", 10) is None
    monkeypatch.setattr(torrent, "MAX_TORRENT_FILE", 5)
    assert torrent.layout_for(junk, tmp_path / "Movie.mkv.part", 10) is None


def test_layout_inside_a_multi_file_torrent(tmp_path):
    files = [("Show/extras/a.nfo", 5000), ("Show/Movie.mkv", 40000), ("Show/z.srt", 3000)]
    meta = {
        "info": {
            "name": "Show",
            "piece length": PIECE,
            "pieces": b"\0" * 20 * 3,
            "files": [{"length": n, "path": p.split("/")[1:]} for p, n in files],
        }
    }
    path = tmp_path / "Show.torrent"
    path.write_bytes(bencode(meta))
    folder = tmp_path / "Show"
    layout = torrent.layout_for(path, folder / "Movie.mkv.part", 40000)
    assert (layout.offset, layout.length, layout.total) == (5000, 40000, 48000)
    assert [(o, n) for o, n, _ in layout.others] == [(0, 5000), (45000, 3000)]
    assert layout.others[0][2][0] == folder / "extras" / "a.nfo"


def test_the_torrent_is_found_beside_the_file_or_in_a_client_folder(tmp_path, monkeypatch):
    data = payload(2)
    video = partial(tmp_path / "Movie.mkv.part", data, [0])
    assert torrent.find_layout(video, len(data)) is None
    client = tmp_path / "client"
    client.mkdir()
    single(tmp_path, "Unrelated.mkv", b"x" * 100, where=client)
    meta = single(tmp_path, "Movie.mkv", data, where=client)
    monkeypatch.setattr(torrent, "_client_folders", lambda: [client, tmp_path / "missing"])
    assert torrent.find_layout(video, len(data)).torrent == meta


# -- which pieces are here -----------------------------------------------------------------


def test_only_pieces_matching_their_hash_count(tmp_path):
    data = payload(5)
    meta = single(tmp_path, "Movie.mkv", data)
    video = partial(tmp_path / "Movie.mkv.part", data, [0, 1, 3, 5])
    with open(video, "r+b") as out:  # a piece with a bad block in it
        out.seek(3 * PIECE + 10)
        out.write(b"garbage")
    tfile = torrent.TorrentFile.open(video, meta)
    try:
        assert tfile.have_units == 3 and tfile.units == 6 and not tfile.complete
        assert tfile.contiguous_from(0) == 2 * PIECE
        assert tfile.contiguous_from(PIECE + 5) == PIECE - 5
        assert tfile.contiguous_from(2 * PIECE) == 0
        assert tfile.contiguous_from(len(data)) == 0
        assert tfile.has(0, 2 * PIECE) and not tfile.has(0, 2 * PIECE + 1)
        assert tfile.has(len(data), 5)
        assert tfile.missing_runs() == [(2 * PIECE, 5 * PIECE)]
        assert tfile.have_bytes == 2 * PIECE + 1000
        assert "3 of 6 pieces" in tfile.describe() and "in order from the start" in tfile.describe()
        assert tfile.suffix == ".mkv" and tfile.path == video
        assert tfile.read_now(0, 10) == data[:10]
        with pytest.raises(torrent.Missing):
            tfile.read_now(2 * PIECE, 10)
    finally:
        tfile.close()


def test_a_torrent_that_does_not_describe_the_file(tmp_path):
    data = payload(2)
    other = single(tmp_path, "Other.mkv", data)
    video = partial(tmp_path / "Movie.mkv.part", data, [0])
    with pytest.raises(UserError, match="does not describe"):
        torrent.TorrentFile.open(video, other)
    (tmp_path / "Empty.mkv.part").write_bytes(b"")
    with pytest.raises(UserError, match="empty"):
        torrent.TorrentFile.open(tmp_path / "Empty.mkv.part")


def test_without_a_torrent_a_file_with_no_gaps_cannot_be_judged(tmp_path):
    video = tmp_path / "Movie.mkv.part"
    video.write_bytes(payload(2))
    with pytest.raises(UserError, match="cannot tell") as caught:
        torrent.TorrentFile.open(video)
    assert "--torrent" in caught.value.hint


@holes
def test_without_a_torrent_written_parts_count(tmp_path, monkeypatch):
    monkeypatch.setattr(torrent, "UNHASHED_BLOCK", 64 * 1024)
    data = payload(64)  # 1 MiB
    video = partial(tmp_path / "Movie.mkv.part", data, range(8), piece=PIECE)
    if not torrent.is_sparse(video):
        pytest.skip("this filesystem keeps no holes")
    tfile = torrent.TorrentFile.open(video)
    try:
        assert tfile.layout is None
        assert tfile.contiguous_from(0) == 2 * 64 * 1024
        assert "parts" in tfile.describe()
        assert "no .torrent" in torrent.summary_lines(tfile)[1]
    finally:
        tfile.close()


def test_a_piece_shared_with_a_neighbouring_file(tmp_path):
    first, movie = payload(0, 5000), payload(2, 3000)
    whole = first + movie
    meta = {
        "info": {
            "name": "Show", "piece length": PIECE, "pieces": hashes(whole),
            "files": [{"length": len(first), "path": ["a.nfo"]}, {"length": len(movie), "path": ["Movie.mkv"]}],
        }
    }  # fmt: skip
    path = tmp_path / "Show.torrent"
    path.write_bytes(bencode(meta))
    folder = tmp_path / "Show"
    folder.mkdir()
    video = folder / "Movie.mkv.part"
    video.write_bytes(movie)
    tfile = torrent.TorrentFile.open(video, path)
    try:  # without the neighbour, the shared first piece goes by this file's own part
        assert tfile.complete
        assert tfile.contiguous_from(0) == len(movie)
        assert tfile.contiguous_from(100) == len(movie) - 100
    finally:
        tfile.close()
    (folder / "a.nfo").write_bytes(b"\xff" * len(first))  # the wrong neighbour
    tfile = torrent.TorrentFile.open(video, path)
    try:
        assert not tfile.has(0, 1) and tfile.has(PIECE, 1)
    finally:
        tfile.close()
    (folder / "a.nfo").write_bytes(first)
    tfile = torrent.TorrentFile.open(video, path)
    try:
        assert tfile.complete
    finally:
        tfile.close()


def test_finished_file_renamed_by_the_client(tmp_path):
    data = payload(1)
    meta = single(tmp_path, "Movie.mkv", data)
    video = partial(tmp_path / "Movie.mkv.part", data, [0, 1])
    tfile = torrent.TorrentFile.open(video, meta)
    try:
        video.rename(tmp_path / "Movie.mkv")
        assert tfile.path == tmp_path / "Movie.mkv"
        assert tfile.read_now(0, 4) == data[:4]  # still reading the same file
    finally:
        tfile.close()


def test_find_partial(tmp_path, monkeypatch):
    data = payload(2)
    done = tmp_path / "Done.mkv"
    done.write_bytes(data)
    assert not torrent.find_partial(done)
    assert torrent.find_partial(done, tmp_path / "given.torrent")
    assert torrent.find_partial(tmp_path / "Movie.mkv.part")
    sparse = partial(tmp_path / "Movie.mkv", data, [0])
    if torrent.is_sparse(sparse):
        assert not torrent.find_partial(sparse)  # gaps, but no client has it
        single(tmp_path, "Movie.mkv", data)
        assert torrent.find_partial(sparse)
    assert not torrent.is_sparse(tmp_path / "absent")


# -- following the download ----------------------------------------------------------------


async def test_reads_wait_for_pieces_to_arrive(tmp_path, monkeypatch):
    monkeypatch.setattr(torrent, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(torrent, "RATE_MINIMUM", 0.0)
    data = payload(3)
    meta = single(tmp_path, "Movie.mkv", data)
    video = partial(tmp_path / "Movie.mkv.part", data, [0])
    tfile = torrent.TorrentFile.open(video, meta)
    tfile.start()
    try:
        assert await tfile.read(5, 10) == data[5:15]
        assert await tfile.read(len(data), 10) == b""
        waiting = asyncio.ensure_future(tfile.read(PIECE * 2 + 3, PIECE))
        await asyncio.sleep(0.1)
        assert not waiting.done() and tfile.waiting_for == PIECE * 2 + 3
        fill(video, data, [2, 3])
        await asyncio.sleep(0.1)
        assert waiting.done() and waiting.result() == data[PIECE * 2 + 3 : PIECE * 3 + 3]
        assert tfile.rate is not None and tfile.rate > 0
        read_at = tfile.reader(asyncio.get_running_loop())
        got = await asyncio.get_running_loop().run_in_executor(None, read_at, 0, 4)
        assert got == data[:4]
        fill(video, data, [1])
        await asyncio.wait_for(tfile.wait_complete(lambda *a: None), 2)
        assert tfile.complete
    finally:
        await tfile.stop()


async def test_a_stopped_download_is_noticed_and_stopping_frees_readers(tmp_path, monkeypatch):
    monkeypatch.setattr(torrent, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(torrent, "STALL_SECONDS", 0.05)
    monkeypatch.setattr(torrent, "RATE_MINIMUM", 0.0)
    data = payload(2)
    meta = single(tmp_path, "Movie.mkv", data)
    video = partial(tmp_path / "Movie.mkv.part", data, [0])
    notes = []
    tfile = torrent.TorrentFile.open(video, meta, on_note=notes.append)
    tfile.start()
    tfile.start()  # once is enough
    waiting = asyncio.ensure_future(tfile.read(PIECE, 10))
    await asyncio.sleep(0.2)
    assert tfile.stalled and len(notes) == 1 and "paused" in notes[0]
    assert tfile.rate == 0
    await tfile.stop()
    with pytest.raises(torrent.Stopped):
        await waiting
    tfile.close()  # twice is harmless


async def test_a_file_that_cannot_be_read_any_more(tmp_path, monkeypatch):
    monkeypatch.setattr(torrent, "POLL_SECONDS", 0.02)
    data = payload(2)
    meta = single(tmp_path, "Movie.mkv", data)
    video = partial(tmp_path / "Movie.mkv.part", data, [0])
    tfile = torrent.TorrentFile.open(video, meta)

    def broken():
        raise OSError("gone")

    monkeypatch.setattr(tfile, "_scan", broken)
    tfile.start()
    await asyncio.sleep(0.1)
    assert "gone" in tfile.error
    await tfile.stop()


# -- what has to be here before anything can start ----------------------------------------


def test_readiness_of_a_matroska_file(tmp_path, monkeypatch):
    monkeypatch.setattr(torrent, "PROBE_BYTES", 4096)
    data = matroska([cue(0, 0), cue(2000, 9000)], front=b"\0" * (4 * PIECE))
    pieces = -(-len(data) // PIECE)
    meta = single(tmp_path, "Movie.mkv", data)
    video = partial(tmp_path / "Movie.mkv.part", data, [0])
    tfile = torrent.TorrentFile.open(video, meta)
    try:
        ready = torrent.readiness(tfile)
        assert not ready.ready and "Cues" in ready.missing and "end of the file" in ready.missing
        fill(video, data, [pieces - 1])
        tfile._mtime = -1
        tfile._scan()
        ready = torrent.readiness(tfile)
        assert ready.ready and ready.kind == "mkv" and len(ready.index.frames) == 2
    finally:
        tfile.close()


def test_readiness_needs_the_start_first(tmp_path, monkeypatch):
    monkeypatch.setattr(torrent, "PROBE_BYTES", 2 * PIECE)
    data = payload(4)
    meta = single(tmp_path, "Movie.mkv", data)
    tfile = torrent.TorrentFile.open(partial(tmp_path / "Movie.mkv.part", data, [0, 3]), meta)
    try:
        ready = torrent.readiness(tfile)
        assert not ready.ready and "start of the file" in ready.missing
        tfile._have[1] = 1
        assert torrent.readiness(tfile).ready  # not a container it knows: the start will do
    finally:
        tfile.close()


def test_readiness_of_an_mp4_with_its_index_at_the_end(tmp_path, monkeypatch):
    monkeypatch.setattr(torrent, "PROBE_BYTES", 4096)
    data = box(b"ftyp", b"isom") + box(b"mdat", b"x" * (5 * PIECE)) + moov(video_trak())
    meta = single(tmp_path, "Movie.mp4", data)
    video = partial(tmp_path / "Movie.mp4.part", data, [0, 1])
    tfile = torrent.TorrentFile.open(video, meta)
    try:
        ready = torrent.readiness(tfile)
        assert not ready.ready and "moov" in ready.missing and "cannot play at all" in ready.missing
        fill(video, data, range(tfile.units))
        tfile._mtime = -1
        tfile._scan()
        ready = torrent.readiness(tfile)
        assert ready.ready and ready.kind == "mp4"
    finally:
        tfile.close()


def test_readiness_of_an_mp4_whose_index_cannot_be_found(tmp_path, monkeypatch):
    monkeypatch.setattr(torrent, "PROBE_BYTES", 16)
    data = box(b"ftyp", b"isom") + box(b"mdat", b"x" * 100)
    meta = single(tmp_path, "Movie.mp4", data)
    tfile = torrent.TorrentFile.open(partial(tmp_path / "Movie.mp4.part", data, [0]), meta)
    try:
        assert "not found" in torrent.readiness(tfile).missing
    finally:
        tfile.close()


# -- playing it ----------------------------------------------------------------------------


async def test_playback_goes_ahead_with_a_little_in_hand(tmp_path, monkeypatch):
    monkeypatch.setattr(torrent, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(buffering, "HEAD_BYTES", 100)
    data = payload(9)  # 10 pieces for a 100 s video: a piece is 10 s
    meta = single(tmp_path, "Movie.mkv", data)
    video = partial(tmp_path / "Movie.mkv.part", data, [0, 1])
    tfile = torrent.TorrentFile.open(video, meta)
    media = await buffering.BufferedMedia.following(tfile)
    try:
        assert media.external and media.url.endswith("/media.mkv")
        assert media.bytes_ahead(0, 30, 100) == int(30 * len(data) / 100)
        statuses = []
        gate = asyncio.ensure_future(media.wait_ready(0, 100, lambda *a: statuses.append(a)))
        await asyncio.sleep(0.1)
        assert not gate.done() and statuses
        fill(video, data, [2, 3])
        await asyncio.wait_for(gate, 2)  # 40 s in hand, though most of it is missing
        assert media.running_dry(4 * PIECE - 100, 100)
    finally:
        await media.close()


async def test_bytes_ahead_uses_the_keyframes(tmp_path):
    data = payload(3)
    meta = single(tmp_path, "Movie.mkv", data)
    tfile = torrent.TorrentFile.open(partial(tmp_path / "Movie.mkv.part", data, [0]), meta)
    from castomatic.mp4index import Mp4Index

    index = Mp4Index(frames=[(0.0, 0.0, 0), (20.0, 20.0, 1000), (40.0, 40.0, 5000)])
    media = await buffering.BufferedMedia.following(tfile, index)
    try:
        assert await media.load_index() is index
        assert media.bytes_ahead(1500, 15, 60) == 5000 - 1500  # from the 20 s keyframe to the one after 35 s
        assert media.bytes_ahead(0, 50, 60) == len(data)  # no keyframe past it: the end
    finally:
        await media.close()


async def test_playing_on_when_the_finished_file_is_renamed(tmp_path, monkeypatch):
    monkeypatch.setattr(torrent, "POLL_SECONDS", 0.02)
    data = payload(3)
    meta = single(tmp_path, "Movie.mkv", data)
    video = partial(tmp_path / "Movie.mkv.part", data, [0, 1])
    notes = []
    tfile = torrent.TorrentFile.open(video, meta, on_note=notes.append)
    tfile.start()
    try:
        waiting = asyncio.ensure_future(tfile.read(2 * PIECE, 10))
        await asyncio.sleep(0.05)
        fill(video, data, [2, 3])  # the last pieces, then the client renames it
        video.rename(tmp_path / "Movie.mkv")
        assert await asyncio.wait_for(waiting, 2) == data[2 * PIECE : 2 * PIECE + 10]
        await asyncio.wait_for(tfile.wait_complete(), 2)
        await asyncio.sleep(0.05)
        assert tfile.path == tmp_path / "Movie.mkv"
        assert await tfile.read(len(data) - 5, 5) == data[-5:]
        assert any("finished" in n and "Movie.mkv" in n for n in notes)
    finally:
        await tfile.stop()


async def test_playing_on_when_the_file_is_moved_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setattr(torrent, "POLL_SECONDS", 0.02)
    data = payload(2)
    meta = single(tmp_path, "Movie.mkv", data)
    video = partial(tmp_path / "Movie.mkv.part", data, [0, 1, 2])
    tfile = torrent.TorrentFile.open(video, meta)
    try:
        (tmp_path / "done").mkdir()
        video.rename(tmp_path / "done" / "Movie.mkv")  # a client moving it to its final folder
        assert tfile.complete and await tfile.read(0, 4) == data[:4]
        assert tfile.path == video  # not to be found: nothing reads by name
    finally:
        await tfile.stop()


def test_a_partial_name_given_after_the_download_finished(tmp_path):
    part = tmp_path / "Movie.mkv.part"
    assert torrent.finished_if_renamed(str(part)) == str(part)
    (tmp_path / "Movie.mkv").write_bytes(b"x")
    assert torrent.finished_if_renamed(str(part)) == str(tmp_path / "Movie.mkv")
    part.write_bytes(b"x")  # both: the partial one is what was asked for
    assert torrent.finished_if_renamed(str(part)) == str(part)
    assert torrent.finished_if_renamed("https://x/a.mkv") == "https://x/a.mkv"
