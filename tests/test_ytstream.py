"""Streaming a yt-dlp video through a local buffer and re-streaming it."""

import asyncio
import shutil
import subprocess
import sys
import textwrap

import aiohttp
import pytest

from castomatic import stream, ytstream
from castomatic.errors import UserError
from castomatic.sources import MediaSource

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def web_source(**kw):
    defaults = dict(title="Clip", inputs=[], via_ytdlp=True, page_url="https://site/watch?v=1")
    return MediaSource(**{**defaults, **kw})


@pytest.fixture
def ts_file(tmp_path):
    path = tmp_path / "in.ts"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=24:duration=14",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=14",
         "-c:v", "libx264", "-g", "48", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mpegts", str(path)],
        check=True,
    )  # fmt: skip
    return path


def fake_ytdlp(monkeypatch, script: str):
    """Replace the yt-dlp command with a Python program that plays its part."""
    monkeypatch.setattr(ytstream, "ytdlp_command", lambda source: [sys.executable, "-c", textwrap.dedent(script)])


def copying(path, pause=0.0, then="", fail=None):
    """A stand-in for yt-dlp: writes ``path`` to stdout in pieces, then ``fail`` or exits."""
    return f"""
        import sys, time
        data = open({str(path)!r}, "rb").read()
        step = max(len(data) // 10, 1)
        for i in range(0, len(data), step):
            sys.stdout.buffer.write(data[i:i + step]); sys.stdout.buffer.flush(); time.sleep({pause})
        {then}
        {"sys.stderr.write(%r); sys.exit(1)" % fail if fail else ""}
    """


# -- how much to hold back -----------------------------------------------------------------


def test_a_live_stream_always_gets_the_same_cushion():
    assert ytstream.lead_needed(None, 0.0) == ytstream.LIVE_LEAD
    assert ytstream.lead_needed(None, 50.0) == ytstream.LIVE_LEAD


@pytest.mark.parametrize(
    "duration,rate,expected",
    [
        (3600, 5.0, ytstream.MIN_LEAD),  # far faster than playing: only the cushion
        (3600, 1.0, 3600 * (1 - ytstream.TRUST)),  # just keeps up: a fifth of it must be here
        (3600, 0.5, 3600 * (1 - ytstream.TRUST * 0.5)),
        (3600, 0.0, 3600),  # no idea of the speed: all of it
        (5, 0.1, 5),  # never more than the video
    ],
)
def test_a_finished_video_needs_what_the_connection_will_not_deliver(duration, rate, expected):
    assert ytstream.lead_needed(duration, rate) == pytest.approx(expected)


def test_command_asks_yt_dlp_for_the_format_on_stdout():
    cmd = ytstream.ytdlp_command(web_source(ytdlp_format="best", cookies_from_browser="firefox"))
    assert cmd[:3] == [sys.executable, "-m", "yt_dlp"] and cmd[cmd.index("-f") + 1] == "best"
    assert cmd[cmd.index("-o") + 1] == "-" and cmd[-1] == "https://site/watch?v=1"
    assert cmd[cmd.index("--cookies-from-browser") + 1] == "firefox"
    assert cmd[cmd.index("--downloader-args") + 1] == "ffmpeg_i:-copyts"  # keeps audio and video in step
    assert "--cookies-from-browser" not in ytstream.ytdlp_command(web_source())


# -- the buffer ----------------------------------------------------------------------------


@needs_ffmpeg
async def test_buffer_collects_what_yt_dlp_writes_and_knows_how_much_video_it_is(monkeypatch, tmp_path, ts_file):
    fake_ytdlp(monkeypatch, copying(ts_file))
    buf = ytstream.YtdlpBuffer(web_source(), tmp_path / "work")
    await buf.start()
    await asyncio.wait_for(buf.finished.wait(), 10)
    assert buf.path.read_bytes() == ts_file.read_bytes() and buf.size == ts_file.stat().st_size
    assert buf.error is None
    assert 13 < await buf.seconds_buffered() < 15
    await buf.stop()


async def test_a_buffer_too_small_to_measure_has_no_length_yet(tmp_path):
    buf = ytstream.YtdlpBuffer(web_source(), tmp_path)
    assert await buf.seconds_buffered() is None
    buf.size = 10 * ytstream.CHUNK  # bytes that are not video
    buf.path.write_bytes(b"x" * buf.size)
    assert await buf.seconds_buffered() is None


@needs_ffmpeg
async def test_a_live_stream_is_ready_once_the_cushion_is_there(monkeypatch, tmp_path, ts_file):
    monkeypatch.setattr(ytstream, "LIVE_LEAD", 6.0)
    fake_ytdlp(monkeypatch, copying(ts_file, pause=0.3, then="time.sleep(30)"))  # stays live
    buf = ytstream.YtdlpBuffer(web_source(is_live=True), tmp_path)
    await buf.start()
    seen = []
    await asyncio.wait_for(buf.wait_ready(0.0, lambda *a: seen.append(a), poll=0.1), 15)
    have, want, speed = seen[-1]
    assert want == 6.0 and have >= 6.0 and speed > 0 and not buf.finished.is_set()
    await buf.stop()
    assert buf.finished.is_set()


@needs_ffmpeg
async def test_waiting_to_start_part_way_adds_the_start_to_the_cushion(monkeypatch, tmp_path, ts_file):
    monkeypatch.setattr(ytstream, "MIN_LEAD", 2.0)
    fake_ytdlp(monkeypatch, copying(ts_file))
    buf = ytstream.YtdlpBuffer(web_source(duration=14.0), tmp_path)
    await buf.start()
    seen = []
    await asyncio.wait_for(buf.wait_ready(5.0, lambda *a: seen.append(a), poll=0.1), 15)
    assert all(want >= 7.0 for _, want, _ in seen)
    await buf.stop()


@needs_ffmpeg
async def test_a_short_video_that_is_all_here_is_ready_even_if_the_cushion_is_longer(monkeypatch, tmp_path, ts_file):
    fake_ytdlp(monkeypatch, copying(ts_file))
    buf = ytstream.YtdlpBuffer(web_source(is_live=True), tmp_path)  # wants 20 s; the clip is 14
    await buf.start()
    await asyncio.wait_for(buf.wait_ready(poll=0.1), 15)
    assert buf.finished.is_set()
    await buf.stop()


async def test_yt_dlp_failing_is_reported_with_its_reason(monkeypatch, tmp_path):
    fake_ytdlp(monkeypatch, "import sys; sys.stderr.write('ERROR: Sign in to confirm your age\\n'); sys.exit(1)")
    buf = ytstream.YtdlpBuffer(web_source(), tmp_path)
    await buf.start()
    with pytest.raises(UserError, match="could not stream 'Clip': Sign in to confirm your age") as info:
        await buf.wait_ready(poll=0.05)
    assert "--cookies-from-browser" in info.value.hint
    await buf.stop()


async def test_yt_dlp_failing_without_a_word_still_gets_an_error(monkeypatch, tmp_path):
    fake_ytdlp(monkeypatch, "import sys; sys.exit(3)")
    buf = ytstream.YtdlpBuffer(web_source(), tmp_path)
    await buf.start()
    with pytest.raises(UserError, match="yt-dlp exited with 3"):
        await buf.wait_ready(poll=0.05)
    await buf.stop()


async def test_yt_dlp_ending_with_nothing_is_an_error(monkeypatch, tmp_path):
    fake_ytdlp(monkeypatch, "pass")
    buf = ytstream.YtdlpBuffer(web_source(), tmp_path)
    await buf.start()
    with pytest.raises(UserError, match="no video came out"):
        await buf.wait_ready(poll=0.05)
    await buf.stop()


async def test_silence_from_yt_dlp_times_out_and_stops_it(monkeypatch, tmp_path):
    fake_ytdlp(monkeypatch, "import time; time.sleep(60)")
    buf = ytstream.YtdlpBuffer(web_source(), tmp_path)
    await buf.start()
    with pytest.raises(UserError, match="did not deliver any video within 0 seconds"):
        await buf.wait_ready(timeout=0.2, poll=0.05)
    assert buf.finished.is_set() and buf._proc.returncode is not None
    await buf.stop()


async def test_a_buffer_that_cannot_be_written_stops_yt_dlp(monkeypatch, tmp_path):
    fake_ytdlp(monkeypatch, "import time; print('x' * 100); time.sleep(60)")
    buf = ytstream.YtdlpBuffer(web_source(), tmp_path)
    await buf.start()
    real = ytstream.Path.open

    def failing(self, *a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ytstream.Path, "open", failing)
    await asyncio.wait_for(buf.finished.wait(), 5)
    monkeypatch.setattr(ytstream.Path, "open", real)
    assert "No space left on device" in buf.error
    await buf.stop()


async def test_stopping_twice_is_harmless(monkeypatch, tmp_path):
    fake_ytdlp(monkeypatch, "import time; time.sleep(60)")
    buf = ytstream.YtdlpBuffer(web_source(), tmp_path)
    await buf.start()
    await buf.stop()
    await buf.stop()
    await ytstream.YtdlpBuffer(web_source(), tmp_path).stop()  # never started


# -- sending it on -------------------------------------------------------------------------


async def fetch(url, method="GET"):
    async with aiohttp.ClientSession() as http:
        async with http.request(method, url) as resp:
            return resp.status, dict(resp.headers), await resp.read()


@needs_ffmpeg
async def test_server_sends_the_whole_buffer_as_it_grows_and_stops_when_yt_dlp_does(monkeypatch, tmp_path, ts_file):
    fake_ytdlp(monkeypatch, copying(ts_file, pause=0.1))
    buf = ytstream.YtdlpBuffer(web_source(), tmp_path)
    server = ytstream.BufferServer(buf, "127.0.0.1")
    await buf.start()
    url = await server.start()
    status, headers, body = await asyncio.wait_for(fetch(url), 15)  # asked for while yt-dlp is still writing
    assert status == 200 and body == ts_file.read_bytes()
    assert headers["Content-Type"] == stream.TS_MIMES[0] and "transferMode.dlna.org" in headers
    assert headers["contentFeatures.dlna.org"] == stream.STREAM_FLAGS
    # Anyone asking again gets it from the start
    assert (await fetch(url))[2] == body and server.requests == 2 and server.last_error is None
    await server.stop()
    await server.stop()
    await buf.stop()


async def test_server_answers_head_without_a_body(monkeypatch, tmp_path):
    fake_ytdlp(monkeypatch, "import time; time.sleep(60)")
    buf = ytstream.YtdlpBuffer(web_source(), tmp_path)
    server = ytstream.BufferServer(buf, "127.0.0.1")
    await buf.start()
    status, headers, body = await asyncio.wait_for(fetch(await server.start(), "HEAD"), 5)
    assert status == 200 and body == b"" and server.requests == 1
    await server.stop()
    await buf.stop()


async def test_server_remembers_why_yt_dlp_ended_badly(monkeypatch, tmp_path):
    fake_ytdlp(monkeypatch, "import sys; sys.stdout.buffer.write(b'x' * 1000); sys.stdout.buffer.flush(); sys.stderr.write('ERROR: 403'); sys.exit(1)")
    buf = ytstream.YtdlpBuffer(web_source(), tmp_path)
    server = ytstream.BufferServer(buf, "127.0.0.1")
    await buf.start()
    status, _, body = await asyncio.wait_for(fetch(await server.start()), 5)
    assert body == b"x" * 1000 and server.last_error == "403"
    await server.stop()
    await buf.stop()


async def test_server_lets_go_when_the_tv_hangs_up(monkeypatch, tmp_path):
    fake_ytdlp(monkeypatch, "import sys, time\nwhile True:\n    sys.stdout.buffer.write(b'x' * 65536); sys.stdout.buffer.flush(); time.sleep(0.01)")
    buf = ytstream.YtdlpBuffer(web_source(), tmp_path)
    server = ytstream.BufferServer(buf, "127.0.0.1")
    await buf.start()
    url = await server.start()
    async with aiohttp.ClientSession() as http:
        async with http.get(url) as resp:
            await resp.content.readexactly(100000)  # then leave without finishing
    await asyncio.sleep(0.3)
    assert await asyncio.wait_for(fetch(url, "HEAD"), 5)  # still serving
    await server.stop()
    await buf.stop()
