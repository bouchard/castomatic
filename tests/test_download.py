"""Downloading a whole video first: direct URLs, yt-dlp, separate subtitle retries."""

import asyncio
import json
import subprocess
import sys
import textwrap
from collections import namedtuple
from pathlib import Path

import pytest
import yt_dlp.utils
from aiohttp import web

from castomatic import sources
from castomatic.errors import UserError
from castomatic.sources import Input, MediaSource

Usage = namedtuple("Usage", "total used free")


def ytdlp_source(**kw):
    defaults = dict(title="Clip", inputs=[Input("https://cdn/x")], via_ytdlp=True, page_url="https://site/watch?v=1",
                    duration=30.0)  # fmt: skip
    defaults.update(kw)
    return MediaSource(**defaults)


async def _serve(handler):
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


# -- helpers -----------------------------------------------------------------------------


def test_size_formatting():
    assert sources.format_size(512) == "512 B" and sources.format_size(1536) == "1.5 KB"
    assert sources.format_size(5 * 1024**2) == "5.0 MB" and sources.format_size(3 * 1024**4) == "3072.0 GB"


def test_check_space(monkeypatch, tmp_path):
    monkeypatch.setattr(sources.shutil, "disk_usage", lambda path: Usage(0, 0, 100 * 1024**2))
    sources.check_space(tmp_path, None)  # unknown size: cannot check
    sources.check_space(tmp_path, 10 * 1024**2)
    with pytest.raises(UserError, match="not enough free space in .* about 500.0 MB and only 100.0 MB") as info:
        sources.check_space(tmp_path, 500 * 1024**2)
    assert "--workdir" in info.value.hint


async def test_daemon_thread_returns_results_and_errors():
    assert await sources._in_daemon_thread(lambda a, b: a + b, 2, 3) == 5
    with pytest.raises(ValueError, match="boom"):
        await sources._in_daemon_thread(lambda: (_ for _ in ()).throw(ValueError("boom")))


async def test_a_result_arriving_after_cancellation_is_ignored():
    release = asyncio.Event()
    started = asyncio.get_running_loop().create_future()

    def slow():
        import time

        started.get_loop().call_soon_threadsafe(started.set_result, None)
        time.sleep(0.2)
        return 1

    task = asyncio.ensure_future(sources._in_daemon_thread(slow))
    await started
    task.cancel()
    await asyncio.sleep(0.4)  # the thread finishes and tries to deliver; nothing must blow up
    assert task.cancelled() and not release.is_set()


def test_a_stuck_thread_cannot_keep_the_program_alive():
    script = textwrap.dedent(
        """
        import asyncio, time
        from castomatic import sources
        async def main():
            sources._in_daemon_thread(time.sleep, 60)   # stuck, as yt-dlp can be when rate limited
            await asyncio.sleep(0.2)
        asyncio.run(main())
        """
    )
    started = __import__("time").monotonic()
    subprocess.run([sys.executable, "-c", script], check=True, timeout=20)
    assert __import__("time").monotonic() - started < 15


# -- direct URLs ---------------------------------------------------------------------------


async def test_http_download_reports_progress_and_sends_headers(tmp_path):
    seen = {}

    async def handler(request):
        seen["ua"] = request.headers.get("User-Agent")
        return web.Response(body=b"x" * 600_000, content_type="video/mp4")

    runner, base = await _serve(handler)
    progress = []
    try:
        source = MediaSource("Film", [Input(f"{base}/files/film.mkv", {"User-Agent": "tester"})])
        local = await sources.download(source, tmp_path / "w", lambda *a: progress.append(a))
    finally:
        await runner.cleanup()
    assert local.is_local and local.path == tmp_path / "w" / "video.mkv" and local.title == "Film"
    assert local.path.read_bytes() == b"x" * 600_000 and seen["ua"] == "tester"
    assert progress[-1][:2] == (600_000, 600_000) and progress[0][0] < progress[-1][0]


async def test_http_download_without_a_known_extension_defaults_to_mp4(tmp_path):
    async def handler(request):
        return web.Response(body=b"abc")

    runner, base = await _serve(handler)
    try:
        local = await sources.download(MediaSource("t", [Input(f"{base}/stream")]), tmp_path)
    finally:
        await runner.cleanup()
    assert local.path.name == "video.mp4"


async def test_http_error_status_is_explained(tmp_path):
    async def handler(request):
        return web.Response(status=403)

    runner, base = await _serve(handler)
    try:
        with pytest.raises(UserError, match="downloading 'T' failed: the server answered HTTP 403") as info:
            await sources.download(MediaSource("T", [Input(f"{base}/v.mp4")]), tmp_path)
    finally:
        await runner.cleanup()
    assert "links can expire" in info.value.hint


async def test_unreachable_server_is_explained(tmp_path):
    with pytest.raises(UserError, match="downloading 'T' failed"):
        await sources.download(MediaSource("T", [Input("http://127.0.0.1:9/v.mp4")]), tmp_path)


async def test_download_refuses_when_the_disk_is_too_small(monkeypatch, tmp_path):
    monkeypatch.setattr(sources.shutil, "disk_usage", lambda path: Usage(0, 0, 1024))

    async def handler(request):
        return web.Response(body=b"x" * 5_000_000)

    runner, base = await _serve(handler)
    try:
        with pytest.raises(UserError, match="not enough free space"):
            await sources.download(MediaSource("T", [Input(f"{base}/v.mp4")]), tmp_path)
        with pytest.raises(UserError, match="not enough free space"):
            await sources.download(ytdlp_source(size_estimate=10**9), tmp_path)
    finally:
        await runner.cleanup()


# -- yt-dlp downloads and the separate subtitle fetch ---------------------------------------


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(sources, "SUBTITLE_RETRY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(sources, "SUBTITLE_GRACE", 2.0)


def _fake_video(monkeypatch, notes=None, fail=None):
    def fake(source, directory, on_progress, verbose, cancel):
        if fail:
            raise fail
        if on_progress:
            on_progress(10, 100, 5.0)
        path = directory / "video.mkv"
        path.write_bytes(b"v")
        return path

    monkeypatch.setattr(sources, "_ytdlp_download", fake)


def _fake_subs(monkeypatch, outcomes, directory_file="video.en.srt"):
    calls = []

    def fake(source, directory, verbose):
        calls.append(1)
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        if outcome:
            (directory / directory_file).write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n\n")

    monkeypatch.setattr(sources, "_ytdlp_subtitles", fake)
    return calls


RATE_LIMITED = yt_dlp.utils.DownloadError("ERROR: Unable to download video subtitles for 'en': HTTP Error 429")


async def test_ytdlp_download_returns_a_local_source_with_subtitles(monkeypatch, tmp_path, fast_retries):
    _fake_video(monkeypatch)
    _fake_subs(monkeypatch, [True])
    notes, progress = [], []
    local = await sources.download(ytdlp_source(), tmp_path, lambda *a: progress.append(a), on_note=notes.append)
    kept = sources.cache_dir(ytdlp_source())
    assert local.path == kept / "video.mkv" and local.title == "Clip" and local.duration == 30.0
    assert (kept / "video.en.srt").exists() and notes == [] and progress == [(10, 100, 5.0)]


async def test_subtitles_are_retried_separately_until_they_work(monkeypatch, tmp_path, fast_retries):
    _fake_video(monkeypatch)
    calls = _fake_subs(monkeypatch, [RATE_LIMITED, RATE_LIMITED, True])
    notes = []
    await sources.download(ytdlp_source(), tmp_path, on_note=notes.append)
    assert len(calls) == 3 and (sources.cache_dir(ytdlp_source()) / "video.en.srt").exists()
    assert len(notes) == 2 and all("HTTP Error 429" in n and "retrying in 0s" in n for n in notes)


async def test_subtitles_that_never_work_do_not_fail_the_video(monkeypatch, tmp_path, fast_retries):
    _fake_video(monkeypatch)
    calls = _fake_subs(monkeypatch, [RATE_LIMITED])
    notes = []
    local = await sources.download(ytdlp_source(), tmp_path, on_note=notes.append)
    assert local.is_local and len(calls) == 4
    assert "subtitles unavailable after 4 tries" in notes[-1] and "playing without them" in notes[-1]


async def test_a_video_with_no_english_subtitles_says_so(monkeypatch, tmp_path, fast_retries):
    _fake_video(monkeypatch)
    _fake_subs(monkeypatch, [False])
    notes = []
    await sources.download(ytdlp_source(), tmp_path, on_note=notes.append)
    assert notes == ["this video has no English subtitles"]


async def test_subtitles_can_be_switched_off(monkeypatch, tmp_path):
    _fake_video(monkeypatch)
    calls = _fake_subs(monkeypatch, [True])
    await sources.download(ytdlp_source(), tmp_path, want_subtitles=False)
    assert calls == []


async def test_slow_subtitles_are_given_up_on_once_the_video_is_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, "SUBTITLE_RETRY_DELAYS", (30.0,))
    monkeypatch.setattr(sources, "SUBTITLE_GRACE", 0.2)
    _fake_video(monkeypatch)
    _fake_subs(monkeypatch, [RATE_LIMITED])
    notes = []
    local = await asyncio.wait_for(sources.download(ytdlp_source(), tmp_path, on_note=notes.append), 5)
    assert local.is_local and notes[-1] == "still waiting on subtitles: playing without them"


async def test_a_failed_video_download_is_explained_and_stops_the_subtitle_fetch(monkeypatch, tmp_path, fast_retries):
    _fake_video(monkeypatch, fail=yt_dlp.utils.DownloadError("ERROR: Video unavailable"))
    _fake_subs(monkeypatch, [RATE_LIMITED])
    with pytest.raises(UserError, match="downloading 'Clip' failed: Video unavailable"):
        await sources.download(ytdlp_source(), tmp_path)


async def test_cancelling_a_download_stops_its_worker(monkeypatch, tmp_path):
    seen = {}

    def fake(source, directory, on_progress, verbose, cancel):
        for _ in range(100):
            if cancel.is_set():
                seen["stopped"] = True
                return directory / "never"
            __import__("time").sleep(0.02)
        return directory / "never"

    monkeypatch.setattr(sources, "_ytdlp_download", fake)
    task = asyncio.ensure_future(sources.download(ytdlp_source(), tmp_path, want_subtitles=False))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert seen == {"stopped": True}


# -- the yt-dlp calls themselves, against a fake YoutubeDL and a real local server ------------


class FakeYDL:
    created = []
    script = None  # called with (hooks) inside extract_info

    def __init__(self, options):
        self.options = options
        FakeYDL.created.append(options)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download):
        if FakeYDL.script:
            FakeYDL.script(self.options)
        return FakeYDL.info

    def prepare_filename(self, info):
        return "/tmp/prepared.mkv"


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYDL.created = []
    FakeYDL.script = None
    FakeYDL.info = {"requested_downloads": [{"filepath": "/tmp/merged.mkv"}]}
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


def test_ytdlp_download_combines_the_progress_of_video_and_audio(fake_ydl, tmp_path):
    import threading

    def script(options):
        hook = options["progress_hooks"][0]
        hook({"status": "downloading", "downloaded_bytes": 50, "speed": 2.0})
        hook({"status": "finished", "downloaded_bytes": 100})
        hook({"status": "downloading", "downloaded_bytes": 30, "speed": None})
        hook({"status": "other"})

    fake_ydl.script = script
    progress = []
    source = ytdlp_source(size_estimate=130, cookies_from_browser="firefox")
    path = sources._ytdlp_download(source, tmp_path, lambda *a: progress.append(a), False, threading.Event())
    assert path == Path("/tmp/merged.mkv") and progress == [(50, 130, 2.0), (130, 130, None)]
    options = fake_ydl.created[0]
    assert options["cookiesfrombrowser"] == ("firefox",) and options["merge_output_format"] == "mkv"
    assert options["outtmpl"] == str(tmp_path / "video.%(ext)s") and "writesubtitles" not in options


def test_ytdlp_download_stops_when_cancelled_and_falls_back_to_the_prepared_name(fake_ydl, tmp_path):
    import threading

    cancel = threading.Event()
    cancel.set()
    fake_ydl.script = lambda options: options["progress_hooks"][0]({"status": "downloading"})
    with pytest.raises(yt_dlp.utils.DownloadCancelled):
        sources._ytdlp_download(ytdlp_source(), tmp_path, None, False, cancel)
    fake_ydl.script = None
    fake_ydl.info = {}
    assert sources._ytdlp_download(ytdlp_source(), tmp_path, None, True, threading.Event()) == Path("/tmp/prepared.mkv")
    assert fake_ydl.created[-1]["quiet"] is False


def test_ytdlp_subtitle_options_are_light_and_skip_the_video(fake_ydl, tmp_path):
    sources._ytdlp_subtitles(ytdlp_source(), tmp_path, False)
    options = fake_ydl.created[0]
    assert options["skip_download"] and options["writesubtitles"] and options["writeautomaticsub"]
    assert options["subtitleslangs"] == ["en", "en-orig"] and options["retries"] == 1
    assert options["postprocessors"][0]["format"] == "srt"


async def test_real_ytdlp_downloads_a_direct_link_with_progress(tmp_path):
    payload = b"v" * 300_000

    async def handler(request):
        return web.Response(body=payload, content_type="video/mp4")

    runner, base = await _serve(handler)
    progress = []
    try:
        source = ytdlp_source(page_url=f"{base}/clip.mp4", size_estimate=len(payload))
        local = await sources.download(source, tmp_path, lambda *a: progress.append(a), want_subtitles=False)
    finally:
        await runner.cleanup()
    assert local.path.read_bytes() == payload
    assert progress and progress[-1][0] <= len(payload) + 1


async def test_a_finished_download_is_kept_and_reused(monkeypatch, tmp_path):
    _fake_video(monkeypatch)
    calls = _fake_subs(monkeypatch, [True])
    first = await sources.download(ytdlp_source(), tmp_path / "one")
    assert first.path.parent == sources.cache_dir(ytdlp_source()) and first.path.is_file()
    assert (first.path.parent / "video.en.srt").exists() and not (tmp_path / "one" / "video.mkv").exists()

    monkeypatch.setattr(sources, "_run_ytdlp_download", None)  # a second download would crash
    notes = []
    again = await sources.download(ytdlp_source(), tmp_path / "two", on_note=notes.append)
    assert again.path == first.path and len(calls) == 1
    assert notes[0].startswith("using the copy downloaded earlier")


async def test_a_cached_video_fetches_subtitles_it_is_missing(monkeypatch, tmp_path):
    _fake_video(monkeypatch)
    _fake_subs(monkeypatch, [False])
    first = await sources.download(ytdlp_source(), tmp_path)
    calls = _fake_subs(monkeypatch, [True])
    again = await sources.download(ytdlp_source(), tmp_path)
    assert len(calls) == 1 and (again.path.parent / "video.en.srt").exists()


def test_a_damaged_cache_entry_is_ignored(tmp_path):
    (tmp_path / sources.DONE_MARKER).write_text("not json")
    assert sources._cached_video(tmp_path) is None
    (tmp_path / sources.DONE_MARKER).write_text('{"video": "gone.mkv"}')
    assert sources._cached_video(tmp_path) is None


def test_keeping_a_download_falls_back_when_the_cache_is_unwritable(tmp_path):
    video = tmp_path / "video.mkv"
    video.write_bytes(b"x")
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder")
    assert sources._keep(video, blocker / "sub", True) == video


def test_a_cache_marker_may_only_name_a_file_in_its_own_folder(tmp_path):
    (tmp_path / "video.mkv").write_bytes(b"x")
    for name in ("../video.mkv", str(tmp_path / "video.mkv"), 7):
        (tmp_path / sources.DONE_MARKER).write_text(json.dumps({"video": name}))
        assert sources._cached_video(tmp_path) is None
    (tmp_path / sources.DONE_MARKER).write_text(json.dumps({"video": "video.mkv"}))
    assert sources._cached_video(tmp_path) == tmp_path / "video.mkv"


async def test_a_cache_hit_needs_no_free_space(monkeypatch, tmp_path):
    _fake_video(monkeypatch)
    _fake_subs(monkeypatch, [True])
    await sources.download(ytdlp_source(), tmp_path)

    def full(*args):
        raise UserError("not enough free space")

    monkeypatch.setattr(sources, "check_space", full)
    assert (await sources.download(ytdlp_source(), tmp_path)).is_local


async def test_unfinished_subtitles_are_not_cached(monkeypatch, tmp_path):
    _fake_video(monkeypatch)
    _fake_subs(monkeypatch, [RATE_LIMITED])
    monkeypatch.setattr(sources, "SUBTITLE_GRACE", 0.05)
    local = await sources.download(ytdlp_source(), tmp_path)
    assert not list(local.path.parent.glob("*.srt"))
