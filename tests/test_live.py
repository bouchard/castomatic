"""Live HLS: ffmpeg writes growing HLS while a remote video is still being fetched."""

import asyncio
import shutil

import pytest
from aiohttp import web

from castomatic import live, stream
from castomatic.errors import UserError
from castomatic.sources import Input
from tests.test_hls import _make_video

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

PLAYLIST = "#EXTM3U\n#EXTINF:6.0,\nseg00000.ts\n#EXTINF:6.0,\nseg00001.ts\n"


def test_live_command_writes_an_event_playlist(tmp_path):
    summary = stream.Summary(0, "h264", "yuv420p", 0, 0, "aac", 60.0)
    plan = stream.make_plan([Input("/m/a.mkv")], summary)
    cmd = stream.live_hls_command(plan, tmp_path, start=12)
    assert cmd[cmd.index("-ss") + 1] == "12.000" and cmd[cmd.index("-c:v") + 1] == "copy"
    assert cmd[cmd.index("-f") + 1] == "hls" and cmd[cmd.index("-hls_playlist_type") + 1] == "event"
    assert cmd[cmd.index("-hls_list_size") + 1] == "0"
    assert cmd[cmd.index("-hls_segment_filename") + 1] == str(tmp_path / "seg%05d.ts")
    assert cmd[-1] == str(tmp_path / "index.m3u8") and "pipe:1" not in cmd


def test_missing_playlist_counts_as_nothing_yet(tmp_path):
    lh = live.LiveHls(None, tmp_path)
    assert lh.segments() == 0 and not lh.finished() and lh.playlist == tmp_path / "index.m3u8"
    (tmp_path / "index.m3u8").write_text(PLAYLIST)
    assert lh.segments() == 2 and not lh.finished()
    (tmp_path / "index.m3u8").write_text(PLAYLIST + "#EXT-X-ENDLIST\n")
    assert lh.finished()


async def _plan(tmp_path):
    video = tmp_path / "in.mkv"
    _make_video(video, "libx264", "aac")
    source = Input(str(video))
    summary = stream.summarize([await stream.probe_input(source)])
    return stream.make_plan([source], summary)


@needs_ffmpeg
async def test_starts_once_segments_exist_and_finishes_the_playlist(tmp_path):
    lh = live.LiveHls(await _plan(tmp_path), tmp_path / "out")
    playlist = await lh.start()
    try:
        assert playlist.name == "index.m3u8" and (lh.segments() >= live.READY_SEGMENTS or lh.finished())
        for _ in range(100):  # the 14 s test video converts almost at once
            if lh.finished():
                break
            await asyncio.sleep(0.1)
        assert lh.finished() and not lh.failed.is_set()
    finally:
        await lh.stop()
    await lh.stop()  # idempotent


@needs_ffmpeg
async def test_an_unreadable_source_is_reported_with_ffmpegs_reason(tmp_path):
    plan = await _plan(tmp_path)
    plan.inputs[0].location = str(tmp_path / "vanished.mkv")
    lh = live.LiveHls(plan, tmp_path / "out")
    with pytest.raises(UserError, match="could not prepare the video: .*vanished.mkv") as info:
        await lh.start()
    assert "--download-first" in info.value.hint
    await lh.stop()


@needs_ffmpeg
async def test_a_source_that_never_delivers_times_out(tmp_path):
    async def silent(request):
        await asyncio.sleep(30)

    app = web.Application()
    app.router.add_get("/v.mp4", silent)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.1)  # do not wait out the handler
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    plan = await _plan(tmp_path)
    plan.inputs[0] = Input(f"http://127.0.0.1:{port}/v.mp4")
    lh = live.LiveHls(plan, tmp_path / "out")
    try:
        with pytest.raises(UserError, match="did not start flowing within 1 seconds") as info:
            await lh.start(timeout=1.0)
        assert "--download-first" in info.value.hint
    finally:
        await lh.stop()
        await runner.cleanup()


class FakeProc:
    """Stands in for ffmpeg: exits with ``code`` after a moment, having printed ``err``."""

    def __init__(self, code, err=b""):
        self.returncode = None
        self._code = code
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(err)
        self.stderr.feed_eof()

    async def wait(self):
        await asyncio.sleep(0.05)
        self.returncode = self._code
        return self._code

    def kill(self):
        self.returncode = -9


async def _with_fake_ffmpeg(monkeypatch, tmp_path, proc):
    (tmp_path / "index.m3u8").write_text(PLAYLIST)  # as if ffmpeg had already written two segments

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(live.asyncio, "create_subprocess_exec", fake_exec)
    lh = live.LiveHls(None, tmp_path)
    monkeypatch.setattr(live.stream, "live_hls_command", lambda *a: ["ffmpeg"])
    await lh.start()
    return lh


async def test_a_failure_part_way_is_flagged(monkeypatch, tmp_path):
    lh = await _with_fake_ffmpeg(monkeypatch, tmp_path, FakeProc(1, b"x\nConnection reset by peer\n"))
    await asyncio.wait_for(lh.failed.wait(), 2)
    assert "Connection reset by peer" in lh.error
    await lh.stop()


async def test_exits_we_caused_or_that_completed_are_not_failures(monkeypatch, tmp_path):
    lh = await _with_fake_ffmpeg(monkeypatch, tmp_path, FakeProc(0))
    await asyncio.sleep(0.2)
    assert not lh.failed.is_set()
    await lh.stop()
    (tmp_path / "index.m3u8").write_text(PLAYLIST + "#EXT-X-ENDLIST\n")
    lh = await _with_fake_ffmpeg(monkeypatch, tmp_path, FakeProc(1, b"late error"))
    (tmp_path / "index.m3u8").write_text(PLAYLIST + "#EXT-X-ENDLIST\n")
    await asyncio.sleep(0.2)
    assert not lh.failed.is_set()  # it had already finished the whole source
    await lh.stop()
