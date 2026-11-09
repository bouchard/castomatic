"""Just-in-time HLS: segment planning, ffmpeg commands and the HTTP server."""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import aiohttp
import pytest

from castomatic import hls

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def test_plan_segments_cuts_on_keyframes():
    keys = [(0.0, -0.08), (2.0, 1.9), (6.5, 6.4), (9.0, 8.9), (13.0, 12.9)]
    segments = hls.plan_segments(keys, duration=15.0, target=6.0)
    assert [s.start for s in segments] == [0.0, 6.5, 13.0]
    assert [round(s.duration, 3) for s in segments] == [6.5, 6.5, 2.0]
    # Cut stops just before the next keyframe's decode time
    assert segments[0].cut == pytest.approx(6.4 - 0.001)
    assert segments[1].cut == pytest.approx(12.9 - 6.5 - 0.001)
    assert segments[2].cut == pytest.approx(15.0 - 13.0 - 0.001)


def test_plan_segments_without_keyframes_is_one_segment():
    (segment,) = hls.plan_segments([], duration=30.0)
    assert (segment.start, segment.duration) == (0.0, 30.0)


def _plan(**kwargs):
    defaults = dict(
        source=Path("movie.mkv"), duration=15.0, copy_video=True, copy_audio=True,
        has_audio=True, audio_index=0, ts_shift=0.083,
    )  # fmt: skip
    defaults.update(kwargs)
    media = hls.MediaPlan(**defaults)
    media.segments = hls.plan_segments([(0.0, 0.0), (7.0, 6.9)], 15.0)
    return media


def test_playlist_is_vod():
    text = _plan().playlist().splitlines()
    assert text[:2] == ["#EXTM3U", "#EXT-X-VERSION:3"]
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in text and text[-1] == "#EXT-X-ENDLIST"
    assert text.count("seg00000.ts") == 1 and "seg00001.ts" in text
    assert "#EXT-X-TARGETDURATION:8" in text


def test_copy_command_only_remuxes():
    cmd = hls.segment_command(_plan(), 1)
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert cmd[cmd.index("-c:a") + 1] == "copy"
    assert cmd[cmd.index("-ss") + 1] == "7.000000"
    assert cmd[cmd.index("-output_ts_offset") + 1] == "7.083000"


def test_transcode_command_only_where_required():
    cmd = hls.segment_command(_plan(copy_video=False, copy_audio=False), 0)
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    only_audio = hls.segment_command(_plan(copy_audio=False), 0)
    assert only_audio[only_audio.index("-c:v") + 1] == "copy"
    assert only_audio[only_audio.index("-c:a") + 1] == "aac"


def test_no_audio_stream_is_not_mapped():
    cmd = hls.segment_command(_plan(has_audio=False, copy_audio=False), 0)
    assert "-c:a" not in cmd and "0:a:0" not in cmd


def _make_video(path: Path, vcodec: str, acodec: str) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=24:duration=14",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=14",
         "-c:v", vcodec, "-g", "48", "-pix_fmt", "yuv420p", "-c:a", acodec, str(path)],
        check=True,
    )  # fmt: skip


@needs_ffmpeg
@pytest.mark.parametrize(
    "vcodec,acodec,copy_video,copy_audio",
    [("libx264", "aac", True, True), ("mpeg4", "libmp3lame", False, False)],
)
async def test_server_converts_just_in_time(tmp_path, vcodec, acodec, copy_video, copy_audio):
    source = tmp_path / "in.mkv"
    _make_video(source, vcodec, acodec)

    media = await hls.plan(source)
    assert (media.copy_video, media.copy_audio) == (copy_video, copy_audio)
    assert len(media.segments) >= 2

    server = hls.JitHlsServer(media, "127.0.0.1")
    url = await server.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url) as resp:
                assert resp.status == 200
                assert "#EXT-X-ENDLIST" in await resp.text()
            segment = url.replace("index.m3u8", "seg00001.ts")
            async with http.get(segment) as resp:
                assert resp.status == 200
                data = await resp.read()
            async with http.get(url.replace("index.m3u8", "seg99999.ts")) as resp:
                assert resp.status == 404
    finally:
        await server.stop()

    out = tmp_path / "seg.ts"
    out.write_bytes(data)
    info = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    assert sorted(set(info)) == ["aac", "h264"]
    # Nothing was written next to the source: no intermediate files
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.mkv", "seg.ts"]


@needs_ffmpeg
async def test_convert_to_directory_matches_jit(tmp_path):
    source = tmp_path / "in.mkv"
    _make_video(source, "libx264", "aac")
    media = await hls.plan(source)

    playlist = await hls.convert_to_directory(media, tmp_path / "out")

    assert playlist.read_text() == media.playlist()
    assert sorted(p.name for p in (tmp_path / "out").glob("seg*.ts")) == [
        f"seg{i:05d}.ts" for i in range(len(media.segments))
    ]


def test_pick_audio_prefers_english():
    streams = [
        {"codec_type": "video"},
        {"codec_type": "audio", "tags": {"language": "fra"}},
        {"codec_type": "audio", "tags": {"language": "eng"}},
    ]
    audio, index = hls._pick_audio(streams)
    assert (audio["tags"]["language"], index) == ("eng", 1)
    assert hls._pick_audio(streams[:2])[1] == 0  # no English: first track
    assert hls._pick_audio(streams[:1]) == (None, 0)


async def test_keyframes_ignores_non_keyframes_and_missing_times(monkeypatch):
    async def fake_run(*args):
        return b"0.0,-0.08,K__\n1.0,0.9,___\nN/A,N/A,K__\nbroken\n2.0,1.9,K_\n"

    monkeypatch.setattr(hls, "_run", fake_run)
    assert await hls.keyframes(Path("x")) == [(0.0, -0.08), (2.0, 1.9)]


async def test_run_reports_failures():
    with pytest.raises(RuntimeError, match="ffprobe failed"):
        await hls.probe(Path("/no/such/file.mkv"))


@needs_ffmpeg
async def test_plan_rejects_audio_only(tmp_path):
    source = tmp_path / "a.mka"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
         "sine=duration=2", str(source)],
        check=True,
    )  # fmt: skip
    with pytest.raises(ValueError, match="no video stream"):
        await hls.plan(source)


def _server(tmp_path, monkeypatch, run):
    monkeypatch.setattr(hls, "_run", run)
    media = _plan()
    return hls.JitHlsServer(media, "127.0.0.1")


async def test_segment_cache_dedupes_and_evicts(tmp_path, monkeypatch):
    calls = []

    async def fake_run(*args):
        calls.append(args[args.index("-ss") + 1])
        await asyncio.sleep(0.01)
        return b"ts" + calls[-1].encode()

    monkeypatch.setattr(hls, "CACHE_SEGMENTS", 1)
    server = _server(tmp_path, monkeypatch, fake_run)

    first, again = await asyncio.gather(server._segment_bytes(0), server._segment_bytes(0))
    assert first == again and len(calls) == 1  # concurrent requests share one ffmpeg
    await server._segment_bytes(0)  # cache hit
    assert len(calls) == 1
    await server._segment_bytes(1)  # evicts segment 0
    await server._segment_bytes(0)
    assert len(calls) == 3
    await server.stop()  # never started: must be a no-op
    assert not server._cache


async def test_failed_conversion_is_a_500(tmp_path, monkeypatch):
    async def failing_run(*args):
        raise RuntimeError("ffmpeg failed: boom")

    server = _server(tmp_path, monkeypatch, failing_run)
    url = await server.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url.replace("index.m3u8", "seg00000.ts")) as resp:
                assert resp.status == 500
            async with http.get(url) as resp:  # the playlist needs no ffmpeg
                assert resp.status == 200
    finally:
        await server.stop()


async def test_static_server_serves_directory(tmp_path):
    (tmp_path / "index.m3u8").write_text("#EXTM3U\n")
    (tmp_path / "seg0.ts").write_bytes(b"data")
    server = hls.StaticHlsServer(tmp_path / "index.m3u8", "127.0.0.1")
    url = await server.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url.replace("index.m3u8", "seg0.ts")) as resp:
                assert await resp.read() == b"data"
                assert resp.headers["Content-Type"] == "video/mp2t"
                assert resp.headers["Cache-Control"] == "no-cache"
            async with http.get(url) as resp:
                assert resp.headers["Content-Type"].startswith("application/vnd.apple.mpegurl")
            async with http.get(url.replace("index.m3u8", "missing.ts")) as resp:
                assert resp.status == 404
            async with http.get(url.replace("index.m3u8", "%2e%2e/secret")) as resp:
                assert resp.status == 404
    finally:
        await server.stop()
    await server.stop()  # second stop is harmless

    (tmp_path / "other.bin").write_bytes(b"x")
    plain = hls.StaticHlsServer(tmp_path / "index.m3u8", "127.0.0.1")
    other = (await plain.start()).replace("index.m3u8", "other.bin")
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(other) as resp:
                assert "Cache-Control" not in resp.headers
    finally:
        await plain.stop()


def test_a_jit_server_can_change_what_it_reads():
    media = hls.MediaPlan("/a.mp4", 10.0, True, True, True, 0)
    server = hls.JitHlsServer(media, "127.0.0.1")
    server.use_source("http://127.0.0.1:1/media.mp4")
    assert media.source == "http://127.0.0.1:1/media.mp4"


async def test_a_cancelled_run_stops_its_process(tmp_path):
    marker = tmp_path / "ran"
    task = asyncio.ensure_future(
        hls._run("sh", "-c", f"echo $$ > {marker}; exec sleep 30")  # noqa: SLF001
    )
    for _ in range(100):
        await asyncio.sleep(0.02)
        if marker.exists() and marker.read_text().strip():
            break
    pid = int(marker.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        await asyncio.sleep(0.02)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
    pytest.fail("the process was left running")


async def test_changing_source_restarts_the_segment_being_cut(monkeypatch):
    media = hls.MediaPlan("old", 10.0, True, True, True, 0, segments=[hls.Segment(0.0, 10.0, 10.0)])
    server = hls.JitHlsServer(media, "127.0.0.1")
    started = asyncio.Event()
    reads = []

    async def run(*args):
        source = args[args.index("-i") + 1]
        reads.append(source)
        if source == "old":
            started.set()
            await asyncio.sleep(60)
        return b"from " + source.encode()

    monkeypatch.setattr(hls, "_run", run)
    waiting = asyncio.ensure_future(server._segment_bytes(0))  # noqa: SLF001
    await started.wait()
    server.use_source("new")
    assert await asyncio.wait_for(waiting, 2) == b"from new"
    assert reads == ["old", "new"]


async def test_stopping_a_segment_is_not_a_restart(monkeypatch):
    media = hls.MediaPlan("old", 10.0, True, True, True, 0, segments=[hls.Segment(0.0, 10.0, 10.0)])
    server = hls.JitHlsServer(media, "127.0.0.1")
    started = asyncio.Event()

    async def run(*args):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(hls, "_run", run)
    task = asyncio.ensure_future(server._convert(0))  # noqa: SLF001
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_server_knows_which_segment_was_asked_for_last(monkeypatch):
    segments = [hls.Segment(0.0, 6.0, 6.0), hls.Segment(6.0, 6.0, 6.0)]
    media = hls.MediaPlan("src", 12.0, True, True, True, 0, segments=segments)
    server = hls.JitHlsServer(media, "127.0.0.1")

    async def run(*args):
        return b"ts"

    monkeypatch.setattr(hls, "_run", run)
    url = await server.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url.replace("index.m3u8", "seg00001.ts")) as resp:
                assert resp.status == 200
    finally:
        await server.stop()
    assert server.reached == 6.0


def test_a_start_on_a_segment_edge_moves_into_it():
    segs = [hls.Segment(0.0, 6.0, 6.0), hls.Segment(6.0, 6.0, 6.0), hls.Segment(12.0, 0.4, 0.4)]
    media = hls.MediaPlan("src", 12.4, True, True, True, 0, segments=segs)
    assert media.playable_start(0.0) == 0.0
    assert media.playable_start(6.0) == 6.5 and media.playable_start(5.8) == 6.5
    assert media.playable_start(9.0) == 9.0
    assert media.playable_start(12.0) == 12.2
    server = hls.JitHlsServer(media, "127.0.0.1")
    assert server.media is media
