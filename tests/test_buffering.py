"""Deciding a file is slow, when to start, and serving a copy still being filled."""

import asyncio
import shutil

import aiohttp
import pytest

from castomatic import buffering, cache as cache_mod, dlna, hls, mp4index, subtitles
from castomatic.cache import BlockCache, CacheError
from castomatic.errors import UserError
from tests.test_mp4index import box, moov, video_trak

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
SIZE = 900_000


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "Movie.mp4"
    path.write_bytes(bytes(i % 251 for i in range(SIZE)))
    return path


def _mp4(path):
    data = box(b"ftyp", b"isom") + moov(video_trak()) + box(b"mdat", b"x" * 700_000)
    path.write_bytes(data)
    return path


# -- is this file slow? --------------------------------------------------------------------


def test_spots_cover_the_file():
    assert buffering.measure_spots(1000) == [0]
    size = 2_000_000_000
    spots = buffering.measure_spots(size)
    assert len(spots) == buffering.MEASURE_SPOTS and all(s % 4096 == 0 for s in spots)
    for i, spot in enumerate(spots):
        assert i * size // buffering.MEASURE_SPOTS <= spot + 4096
        assert spot < (i + 1) * size // buffering.MEASURE_SPOTS


async def test_measure_rate(source, monkeypatch):
    assert await buffering.measure_rate(source) > 0
    source.chmod(0)
    assert await buffering.measure_rate(source) is None
    source.chmod(0o600)
    assert await buffering.measure_rate(source.parent / "absent") is None

    monkeypatch.setattr(buffering, "MEASURE_TIMEOUT", 0.01)

    async def hang(*args):
        await asyncio.sleep(1)

    monkeypatch.setattr(buffering, "_in_daemon_thread", hang)
    assert await buffering.measure_rate(source) == 0.0


def test_measuring_stops_at_its_time_limit_and_at_the_end(source, monkeypatch):
    calls = []
    real = buffering.os.pread

    def pread(fd, length, offset):
        calls.append(offset)
        return real(fd, length, offset)

    monkeypatch.setattr(buffering.os, "pread", pread)
    assert buffering._measure(source, [SIZE - 10]) > 0  # ran off the end
    assert calls == [SIZE - 10, SIZE]
    monkeypatch.setattr(buffering, "MEASURE_SECONDS", 0)
    calls.clear()
    buffering._measure(source, [0, 500_000])
    assert 500_000 not in calls


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_reads_ask_to_skip_the_os_cache(platform, monkeypatch, source):
    import fcntl

    calls = []
    monkeypatch.setattr(buffering.sys, "platform", platform)
    monkeypatch.setattr(fcntl, "F_NOCACHE", 48, raising=False)
    monkeypatch.setattr(fcntl, "fcntl", lambda *a: calls.append("nocache"))
    monkeypatch.setattr(buffering.os, "posix_fadvise", lambda *a: calls.append("fadvise"), raising=False)
    monkeypatch.setattr(buffering.os, "POSIX_FADV_DONTNEED", 4, raising=False)
    fd = buffering.os.open(source, buffering.os.O_RDONLY)
    buffering._skip_os_cache(fd)
    assert calls == ["nocache" if platform == "darwin" else "fadvise"]

    def refuse(*a):
        raise OSError("not on this filesystem")

    monkeypatch.setattr(fcntl, "fcntl", refuse)
    monkeypatch.setattr(buffering.os, "posix_fadvise", refuse, raising=False)
    buffering._skip_os_cache(fd)
    buffering.os.close(fd)


@pytest.mark.parametrize(
    "remaining,seconds,rate,bitrate,expected",
    [
        (1000, 100, None, 10.0, 1000),  # no speed yet: wait for everything
        (1000, 100, 100.0, 10.0, 200),  # drive far faster: only the startup cushion
        (100, 10, 100.0, 10.0, 100),  # the cushion never exceeds what is left
        (1000, 100, 5.0, 10.0, 600),  # (10 - 4) a second short for 100 seconds
        (1000, 100, 0.0, 10.0, 1000),  # drive stalled: all of it
        (1000, 100, 11.0, 10.0, 200),  # just faster, but not by the safety margin: cushion wins
    ],
)
def test_needed_bytes(remaining, seconds, rate, bitrate, expected):
    assert buffering.needed_bytes(remaining, seconds, rate, bitrate) == expected


def test_needed_bytes_for_the_real_case():
    size, duration = 1_819_444_775, 1704.8
    need = buffering.needed_bytes(size, duration, 700_000, size / duration)
    assert 800_000_000 < need < 900_000_000


# -- the server ----------------------------------------------------------------------------


@pytest.fixture
async def served(source, tmp_path):
    media = await buffering.BufferedMedia.open(source, tmp_path / "root", workers=2)
    yield media
    await media.close()


async def test_ranges(served, source):
    whole = source.read_bytes()
    async with aiohttp.ClientSession() as session:
        got = await session.get(served.url)
        assert got.status == 200 and got.headers["Accept-Ranges"] == "bytes"
        assert got.content_type == "video/mp4" and await got.read() == whole

        got = await session.get(served.url, headers={"Range": "bytes=500000-500009"})
        assert got.status == 206 and got.headers["Content-Range"] == f"bytes 500000-500009/{SIZE}"
        assert await got.read() == whole[500000:500010]

        got = await session.get(served.url, headers={"Range": "bytes=899990-"})
        assert await got.read() == whole[899990:]

        got = await session.get(served.url, headers={"Range": "bytes=-10"})
        assert got.status == 206 and await got.read() == whole[-10:]

        got = await session.get(served.url, headers={"Range": f"bytes={SIZE}-"})
        assert got.status == 416 and got.headers["Content-Range"] == f"bytes */{SIZE}"

        got = await session.get(served.url, headers={"Range": "bytes=oops"})
        assert got.status == 416

        got = await session.head(served.url)
        assert got.status == 200 and int(got.headers["Content-Length"]) == SIZE


async def test_a_tv_can_fetch_a_file_still_arriving(served, source):
    server = dlna.DirectFileServer(source, "127.0.0.1", respond=served.respond)
    url = await server.start()
    async with aiohttp.ClientSession() as session:
        got = await session.get(url, headers={"Range": "bytes=700000-700099"})
        assert got.status == 206 and got.headers["contentFeatures.dlna.org"]
        assert await got.read() == source.read_bytes()[700000:700100]
    assert server.requests == 1
    await server.stop()


async def test_a_failing_drive_cuts_the_connection(source, tmp_path, caplog):
    cache = BlockCache.open(source, tmp_path / "r")
    server = buffering.CacheServer(cache)
    url = await server.start()
    cache.error = "the drive is gone"
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientError):
            await (await session.get(url)).read()
    assert "the drive is gone" in caplog.text
    await server.stop()
    await server.stop()
    cache.close()


async def test_a_reader_that_hangs_up_is_normal(served):
    host, port = served.url.split("/")[2].split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(b"GET /media.mp4 HTTP/1.1\r\nHost: x\r\n\r\n")
    await writer.drain()
    await reader.readuntil(b"\r\n\r\n")
    writer.transport.abort()
    await asyncio.sleep(0.05)
    async with aiohttp.ClientSession() as session:
        assert (await session.head(served.url)).status == 200  # still serving


async def test_a_failed_server_start_releases_the_copy(source, tmp_path, monkeypatch):
    async def fail(self):
        raise OSError("no ports")

    monkeypatch.setattr(buffering.CacheServer, "start", fail)
    with pytest.raises(OSError):
        await buffering.BufferedMedia.open(source, tmp_path)
    BlockCache.open(source, tmp_path).close()  # the lock was given back


# -- planning and starting -----------------------------------------------------------------


async def test_load_index_from_the_copy(tmp_path):
    media = await buffering.BufferedMedia.open(_mp4(tmp_path / "a.mp4"), tmp_path / "root")
    index = await media.load_index()
    assert len(index.frames) == 2 and await media.load_index() is index
    await media.close()


async def test_plan_uses_the_index_and_the_loopback_address(tmp_path, monkeypatch):
    calls = []

    async def plan(source, subtitle=None, index=None):
        calls.append((source, subtitle, index))
        return "the plan"

    monkeypatch.setattr(buffering.hls, "plan", plan)
    media = await buffering.BufferedMedia.open(_mp4(tmp_path / "a.mp4"), tmp_path / "root", workers=1)
    media.cache._read_source = _slowly(media.cache._read_source)
    assert await media.plan("sub") == "the plan"
    assert calls[0][0] == media.url and calls[0][1] == "sub" and calls[0][2] is media.index
    await media.cache.wait_complete()
    await media.plan()
    assert calls[1][0] == media.cache.path == media.local_path
    await media.close()


def _slowly(read):
    def slow(fd, block):
        __import__("time").sleep(0.002)
        return read(fd, block)

    return slow


async def test_plan_without_an_index_copies_everything_first(source, tmp_path, monkeypatch):
    seen = []

    async def plan(path, subtitle=None):
        assert media.cache.complete
        return path

    monkeypatch.setattr(buffering.hls, "plan", plan)
    media = await buffering.BufferedMedia.open(source, tmp_path / "root", workers=2)
    assert await media.plan(None, lambda *a: seen.append(a)) == media.cache.path
    await media.close()


async def test_hls_plan_reads_keyframes_from_the_index(monkeypatch, tmp_path):
    async def probe(source):
        return {
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p", "width": 640, "height": 360}
            ],
            "format": {"duration": "20"},
        }

    async def no_scan(source):
        raise AssertionError("ffprobe must not read every packet")

    monkeypatch.setattr(hls, "probe", probe)
    monkeypatch.setattr(hls, "keyframes", no_scan)
    index = mp4index.Mp4Index([(0.0, -0.1, 0), (10.0, 9.9, 100)])
    media = await hls.plan("http://127.0.0.1:1/media.mp4", index=index)
    assert media.ts_shift == pytest.approx(0.1) and media.segments


class Ready:
    """A stand-in BlockCache whose progress a test drives."""

    def __init__(self, rate=None, have=0, size=1000, complete=False):
        self.size, self.rate, self.have, self.complete = size, rate, have, complete
        self.error = None
        self.hinted = None

    def hint(self, offset):
        self.hinted = offset

    def contiguous_from(self, offset):
        return self.have


def media_for(cache, index=None):
    media = buffering.BufferedMedia(cache, None, "url")
    media.index = index
    media._indexed = True
    return media


async def test_wait_ready_holds_until_the_shortfall_is_covered(monkeypatch):
    monkeypatch.setattr(buffering.asyncio, "sleep", _instant)
    monkeypatch.setattr(buffering, "HEAD_BYTES", 0)  # only the shortfall here
    cache = Ready(rate=5.0)  # 10 bytes/s video: 6 short each second for 100 seconds
    seen = []

    def status(have, need, rate):
        seen.append((have, need, rate))
        cache.have += 200

    await media_for(cache).wait_ready(0.0, 100.0, status)
    assert seen[0] == (0, 600, 5.0) and cache.have == 600 and cache.hinted == 0


_real_sleep = asyncio.sleep


async def _instant(seconds):
    await _real_sleep(0)


async def test_wait_ready_from_a_later_start(monkeypatch):
    index = mp4index.Mp4Index([(0.0, 0.0, 0), (50.0, 50.0, 480)])
    cache = Ready(rate=1000.0, have=10_000)
    await media_for(cache, index).wait_ready(60.0, 100.0)
    assert cache.hinted == 480
    no_index = Ready(rate=1000.0, have=10_000)
    await media_for(no_index).wait_ready(60.0, 100.0)
    assert no_index.hinted == 600  # even bitrate assumed


def test_running_dry():
    cache = Ready(size=1000, have=50)
    media = media_for(cache)
    assert media.running_dry(0, 100.0)  # 5 seconds ahead
    cache.have = 100
    assert not media.running_dry(0, 100.0)  # 10 seconds ahead
    cache.have = 20
    assert not media.running_dry(980, 100.0)  # all that is left of the file
    cache.have = 0
    cache.complete = True
    assert not media.running_dry(0, 100.0)


def test_offset_at():
    index = mp4index.Mp4Index([(0.0, 0.0, 0), (50.0, 50.0, 480)])
    assert media_for(Ready(), index).offset_at(60.0, 100.0) == 480
    assert media_for(Ready()).offset_at(60.0, 100.0) == 600
    assert media_for(Ready()).offset_at(500.0, 100.0) == 1000


async def test_wait_ready_ends_early_or_reports_failure(monkeypatch):
    monkeypatch.setattr(buffering.asyncio, "sleep", _instant)
    done = Ready(complete=True)
    await media_for(done).wait_ready(0.0, 100.0)

    cache = Ready(rate=None)

    def complete(*args):
        cache.complete = True

    await media_for(cache).wait_ready(0.0, 100.0, complete)
    broken = Ready()
    broken.error = "no more drive"
    with pytest.raises(CacheError, match="no more drive"):
        await media_for(broken).wait_ready(0.0, 100.0)


async def test_finish_and_resume(source, tmp_path):
    media = await buffering.BufferedMedia.open(source, tmp_path / "root")
    assert await media.finish() == media.cache.path
    await media.close()
    assert media.cache.path.read_bytes() == source.read_bytes()


# -- deciding ------------------------------------------------------------------------------


def _rate(monkeypatch, value):
    async def measure(path):
        return value

    monkeypatch.setattr(buffering, "measure_rate", measure)


async def test_nothing_is_read_without_a_length(tmp_path):
    assert await buffering.choose(tmp_path / "absent", None, tmp_path) is None


async def test_a_fast_drive_is_left_alone(source, tmp_path, monkeypatch):
    _rate(monkeypatch, 1e9)
    assert await buffering.choose(source, 10.0, tmp_path / "r") is None
    assert not (tmp_path / "r").exists()
    _rate(monkeypatch, None)  # could not tell
    assert await buffering.choose(source, 10.0, tmp_path / "r") is None


async def test_a_slow_file_is_buffered_wherever_it_is(source, tmp_path, monkeypatch):
    _rate(monkeypatch, 100_000)
    notes = []
    media = await buffering.choose(source, 10.0, tmp_path / "r", on_note=notes.append)
    assert media and "reads at 0.1 MB/s and the video needs 0.1 MB/s" in notes[0]
    await media.close()


async def test_just_fast_enough_is_not_fast_enough(source, tmp_path, monkeypatch):
    bitrate = SIZE / 10.0
    _rate(monkeypatch, bitrate * 5)  # would play, with no room for a peak in the bitrate
    media = await buffering.choose(source, 10.0, tmp_path / "r")
    assert media
    await media.close()


async def test_a_source_that_does_not_answer_is_buffered(source, tmp_path, monkeypatch):
    _rate(monkeypatch, 0.0)
    media = await buffering.choose(source, 10.0, tmp_path / "r")
    assert media
    await media.close()


async def test_the_system_temporary_folder_is_the_default(source, tmp_path, monkeypatch):
    _rate(monkeypatch, 0.0)
    monkeypatch.setattr(cache_mod, "default_cache_root", lambda: tmp_path / "system")
    media = await buffering.choose(source, 10.0)
    assert media.cache.directory.parent == tmp_path / "system"
    await media.close()


async def test_an_earlier_copy_is_used_without_timing_the_drive(source, tmp_path, monkeypatch):
    async def never(path):
        raise AssertionError("no need to time the drive")

    monkeypatch.setattr(buffering, "measure_rate", never)
    notes = []
    root = tmp_path / "r"
    first = BlockCache.open(source, root, block_size=cache_mod.BLOCK_SIZE)
    first.close()
    media = await buffering.choose(source, 10.0, root, on_note=notes.append)
    assert "carrying on" in notes[0]
    await media.finish()
    await media.close()
    media = await buffering.choose(source, 10.0, root, on_note=notes.append)
    assert "verified local copy" in notes[1] and media.cache.complete
    await media.close()


async def test_unknown_length_plays_directly(source, tmp_path):
    assert await buffering.choose(source, None, tmp_path / "r") is None


async def test_no_room_or_a_locked_copy_falls_back_quietly(source, tmp_path, monkeypatch):
    _rate(monkeypatch, 1.0)
    held = BlockCache.open(source, tmp_path)
    notes = []
    assert await buffering.choose(source, 10.0, tmp_path, on_note=notes.append) is None
    held.close()

    def full(*args):
        raise UserError("not enough free space")

    monkeypatch.setattr(buffering.cache_mod, "require_cache_space", full)
    assert await buffering.choose(source, 10.0, tmp_path, on_note=notes.append) is None
    assert all("playing straight from the file" in note for note in notes[-1:])


@needs_ffmpeg
async def test_segments_cut_through_the_copy_match_the_original(tmp_path):
    """The real thing: ffmpeg reads a slow copy by HTTP while it fills, moov at either end."""
    from tests.test_hls import _make_video

    path = tmp_path / "Movie.mp4"
    _make_video(path, "libx264", "aac")
    media = await buffering.BufferedMedia.open(path, tmp_path / "root", workers=1)
    real = media.cache._read_source
    media.cache._read_source = _slowly(real)
    plan = await media.plan()
    assert media.index.frames and (plan.source == media.url or media.cache.complete)
    server = hls.JitHlsServer(plan, "127.0.0.1")
    url = await server.start()
    async with aiohttp.ClientSession() as session:
        playlist = await (await session.get(url)).text()
        names = [line for line in playlist.splitlines() if line.endswith(".ts")]
        last = await session.get(url.rsplit("/", 1)[0] + "/" + names[-1])
        assert last.status == 200 and len(await last.read()) > 1000
    await server.stop()
    await media.close()


# -- slowing down part way through ---------------------------------------------------------


def _readings(monkeypatch, values):
    left = iter(values)

    async def measure(path):
        return next(left)

    monkeypatch.setattr(buffering, "measure_rate", measure)
    monkeypatch.setattr(buffering, "WATCH_SECONDS", 0)


async def test_one_low_reading_is_forgiven_two_are_not(source, monkeypatch):
    bitrate = SIZE / 10.0
    readings = [bitrate * 9, bitrate, bitrate * 9, None, bitrate, bitrate, "unused"]
    _readings(monkeypatch, readings)
    await buffering.Fallback(source, 10.0).slowed()


async def test_a_stall_needs_no_second_opinion(source, monkeypatch):
    _readings(monkeypatch, [])

    async def stalled():
        return True

    await buffering.Fallback(source, 10.0).slowed(stalled)


async def test_a_stall_check_that_finds_nothing_carries_on(source, monkeypatch):
    _readings(monkeypatch, [0.0, 0.0])

    async def stalled():
        return False

    await buffering.Fallback(source, 10.0).slowed(stalled)


async def test_a_fallback_opens_a_copy_and_says_why(source, tmp_path):
    notes = []
    fallback = buffering.Fallback(source, 10.0, tmp_path / "r", on_note=notes.append)
    media = await fallback.open()
    assert media is fallback.media and "stopped keeping up" in notes[0]
    await media.close()


async def test_a_fallback_uses_the_system_temporary_folder(source, tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "default_cache_root", lambda: tmp_path / "system")
    media = await buffering.Fallback(source, 10.0).open()
    assert media.cache.directory.parent == tmp_path / "system"
    await media.close()


async def test_a_drive_gone_too_far_to_copy_from(tmp_path):
    notes = []
    fallback = buffering.Fallback(tmp_path / "gone.mp4", 10.0, tmp_path, on_note=notes.append)
    assert await fallback.open() is None and fallback.media is None
    assert "playing straight from the file" in notes[-1]


async def test_an_embedded_subtitle_waits_for_the_finished_copy(tmp_path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"x" * 4096)
    media = await buffering.BufferedMedia.open(source, tmp_path / "cache")
    try:
        inside = subtitles.Subtitle("en", "English", video=source)
        beside = subtitles.Subtitle("en", "English", sidecar=tmp_path / "movie.srt")
        original = type(media.cache).complete
        type(media.cache).complete = property(lambda self: False)
        try:
            assert media.subtitle(None) is None
            assert media.subtitle(beside) is beside
            assert media.subtitle(inside) is None
        finally:
            type(media.cache).complete = original
        await media.cache.wait_complete()
        assert media.subtitle(inside) is inside and inside.video == media.local_path
    finally:
        await media.close()


async def test_a_matroska_index_is_found(monkeypatch, tmp_path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"x" * 4096)
    media = await buffering.BufferedMedia.open(source, tmp_path / "cache")
    try:
        found = mp4index.Mp4Index([(0.0, 0.0, 0)])
        monkeypatch.setattr(buffering.mkvindex, "load", lambda read_at, size: found)
        assert await media.load_index() is found
    finally:
        await media.close()


async def test_a_busy_opening_is_waited_for(tmp_path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"x" * 1000)
    media = await buffering.BufferedMedia.open(source, tmp_path / "cache")
    try:
        # 100 s of video; the first 10 s take 600 of the 1000 bytes
        media.index = mp4index.Mp4Index([(0.0, 0.0, 0), (10.0, 10.0, 600), (20.0, 20.0, 650)])
        rate = 10.0 / buffering.SAFETY  # 10 bytes a second usable: the average
        assert media.lead_needed(0, rate, 100.0) == 500  # 600 needed by 10 s, 100 of it arrives
        assert media.lead_needed(600, rate, 100.0) == 0  # from there on it keeps up
        assert media.lead_needed(0, 0.0, 100.0) == 1000
        media.index = None
        assert media.lead_needed(0, rate, 100.0) == 0
    finally:
        await media.close()


async def test_the_gate_waits_for_the_busy_opening(tmp_path, monkeypatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"x" * 1000)
    media = await buffering.BufferedMedia.open(source, tmp_path / "cache")
    try:
        media.index = mp4index.Mp4Index([(0.0, 0.0, 0), (10.0, 10.0, 600)])
        seen = []
        monkeypatch.setattr(type(media.cache), "rate", property(lambda self: 1e9))
        await media.fill_ahead(0, 100.0, lambda *a: seen.append(a))
    finally:
        await media.close()


async def test_the_start_of_the_file_comes_first(tmp_path, monkeypatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"x" * 3000)
    monkeypatch.setattr(buffering, "HEAD_BYTES", 1000)
    media = await buffering.BufferedMedia.open(source, tmp_path / "cache")
    try:
        hints, seen = [], []
        heads = iter([0, 0, 500, 500, 1000])
        monkeypatch.setattr(media.cache, "hint", hints.append)
        monkeypatch.setattr(media.cache, "contiguous_from", lambda offset: next(heads))
        monkeypatch.setattr(type(media.cache), "complete", property(lambda self: False))
        monkeypatch.setattr(buffering.asyncio, "sleep", _no_sleep)
        await media.fill_head(lambda *a: seen.append(a))
        assert hints == [0] and [s[:2] for s in seen] == [(0, 1000), (500, 1000)]
    finally:
        monkeypatch.undo()
        await media.close()


async def _no_sleep(seconds):
    return None


async def test_a_failed_copy_stops_the_wait_for_the_start(tmp_path, monkeypatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"x" * 3000)
    media = await buffering.BufferedMedia.open(source, tmp_path / "cache")
    try:
        monkeypatch.setattr(type(media.cache), "complete", property(lambda self: False))
        monkeypatch.setattr(media.cache, "contiguous_from", lambda offset: 0)
        media.cache.error = "the drive went away"
        with pytest.raises(cache_mod.CacheError):
            await media.fill_head()
    finally:
        monkeypatch.undo()
        media.cache.error = None
        await media.close()


async def test_the_opening_is_read_up_to_the_next_keyframe(tmp_path, monkeypatch):
    monkeypatch.setattr(buffering, "HEAD_BYTES", 100)
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"x" * 10_000)
    media = await buffering.BufferedMedia.open(source, tmp_path / "cache")
    try:
        assert media.head_bytes() == 100
        assert media.head_bytes(10.0, 100.0) == 100 + 1000  # no index: an even bitrate
        media.index = mp4index.Mp4Index([(0.0, 0.0, 0), (8.0, 8.0, 3000), (16.0, 16.0, 4000)])
        assert media.head_bytes(10.0, 100.0) == 4000  # the group running past 10 s
        assert media.head_bytes(20.0, 100.0) == 10_000
    finally:
        await media.close()


async def test_a_restart_part_way_waits_for_the_opening(monkeypatch):
    monkeypatch.setattr(buffering.asyncio, "sleep", _instant)
    cache = Ready(rate=1e9, have=1000)
    asked = []
    media = media_for(cache)

    async def fill_head(on_status=None, seconds=0.0, duration=1.0):
        asked.append(seconds)

    monkeypatch.setattr(media, "fill_head", fill_head)
    await media.wait_ready(60.0, 100.0)
    await media.wait_ready(60.0, 100.0, opening=True)
    assert asked == [0.0, buffering.OPENING_SECONDS]
