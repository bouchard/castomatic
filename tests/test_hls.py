"""Just-in-time HLS: segment planning, ffmpeg commands and the HTTP server."""

import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

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


def test_copied_video_drops_what_comes_before_the_cut():
    cmd = hls.segment_command(_plan(video_codec="hevc"), 1)
    assert cmd[cmd.index("-copypriorss") + 1] == "0"
    assert cmd[cmd.index("-bsf:v") + 1] == "hevc_mp4toannexb"
    assert "-copypriorss" not in hls.segment_command(_plan(copy_video=False), 1)


def _video_packets(path: Path, stream: str = "v:0") -> list:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", "packet=pts_time",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return sorted(float(line.strip(",")) for line in out.split())


@needs_ffmpeg
@pytest.mark.parametrize("vcodec,codec", [("libx264", "h264"), ("libx265", "hevc")])
async def test_copied_segments_do_not_repeat_the_one_before(tmp_path, vcodec, codec):
    # B-frames make ffmpeg seek just before -ss; from a cut on a keyframe that is the
    # keyframe before, and a copied segment used to carry the whole previous one too
    source = tmp_path / "in.mkv"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=24:duration=14",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=14",
         "-c:v", vcodec, "-g", "48", "-bf", "2", "-pix_fmt", "yuv420p",
         *(["-x265-params", "log-level=error"] if codec == "hevc" else []),
         "-c:a", "ac3", str(source)],
        check=True,
    )  # fmt: skip
    media = await hls.plan(source, video_ok={codec}, audio_ok=set())
    assert (media.copy_video, media.copy_audio, media.video_codec) == (True, False, codec)
    seg = media.segments[1]
    out = tmp_path / "seg.ts"
    proc = await asyncio.create_subprocess_exec(*hls.segment_command(media, 1), stdout=asyncio.subprocess.PIPE)
    out.write_bytes((await proc.communicate())[0])
    pts = _video_packets(out)
    assert len(pts) == pytest.approx(seg.duration * 24, abs=2)
    assert pts[0] == pytest.approx(seg.start + media.time_offset, abs=0.1)
    assert media.fmp4 is (codec == "hevc")  # HLS carries HEVC only in fMP4
    # the audio becomes AAC, so the master playlist can say what the TV is getting;
    # without CODECS a Chromecast assumes H.264 and fails on HEVC
    video_string, audio_string = media.codecs.split(",")
    assert video_string.startswith("avc1.64" if codec == "h264" else "hvc1.1.6.L")
    assert audio_string == "mp4a.40.2"


@needs_ffmpeg
async def test_plan_copies_only_what_the_tv_decodes(tmp_path):
    source = tmp_path / "in.mkv"
    _make_video(source, "libx264", "aac")
    assert (await hls.plan(source, video_ok=set(), audio_ok=set())).copy_video is False
    default = await hls.plan(source)
    assert (default.copy_video, default.copy_audio, default.codecs is not None) == (True, True, True)


def test_fmp4_playlist_names_the_init_segment():
    media = _plan(fmp4=True, video_codec="hevc", copy_audio=False)
    text = media.playlist().splitlines()
    assert "#EXT-X-VERSION:7" in text and '#EXT-X-MAP:URI="init.mp4"' in text
    assert "seg00001.m4s" in text and not any(line.endswith(".ts") for line in text)
    assert media.time_offset == pytest.approx(0.083 + hls.FMP4_PAD)
    cmd = hls.run_command(media, 1)
    assert cmd[cmd.index("-tag:v") + 1] == "hvc1" and "-copyts" in cmd
    # one run from the segment's keyframe to the end: no -to, audio from the same input
    assert cmd[cmd.index("-ss") + 1] == f"{media.segments[1].start:.6f}" and "-to" not in cmd
    assert cmd[cmd.index("-map", cmd.index("0:v:0")) + 1] == "0:a:0"
    assert cmd[cmd.index("-output_ts_offset") + 1] == f"{media.time_offset:.6f}"
    assert "frag_discont" in cmd[cmd.index("-movflags") + 1]
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    copied = hls.run_command(_plan(fmp4=True, video_codec="h264", copy_audio=True), 0)
    assert copied[copied.index("-tag:v") + 1] == "avc1" and copied[copied.index("-c:a") + 1] == "copy"


def _box(kind: bytes, body: bytes = b"") -> bytes:
    return (8 + len(body)).to_bytes(4, "big") + kind + body


def _traf(track: int, decode: int, trun: bytes, wide: bool = False) -> bytes:
    tfhd = _box(b"tfhd", b"\x00\x02\x00\x00" + track.to_bytes(4, "big"))
    when = (b"\x01\x00\x00\x00" + decode.to_bytes(8, "big")) if wide else (b"\x00\x00\x00\x00" + decode.to_bytes(4, "big"))
    return _box(b"traf", tfhd + _box(b"tfdt", when) + trun)


def _trun(offsets, version=1, first_flags=True):
    flags = 0x1 | 0x100 | 0x200 | 0x800 | (0x4 if first_flags else 0)
    body = bytes([version]) + flags.to_bytes(3, "big") + len(offsets).to_bytes(4, "big") + (0).to_bytes(4, "big")
    if first_flags:
        body += (0x2000000).to_bytes(4, "big")
    for cto in offsets:
        body += (3000).to_bytes(4, "big") + (100).to_bytes(4, "big") + cto.to_bytes(4, "big", signed=True)
    return _box(b"trun", body)


@pytest.mark.parametrize(
    "moof,expected",
    [
        # video (track 1): decode time plus the first sample's composition offset, at 90 kHz
        (_box(b"moof", _box(b"mfhd", b"\0" * 8) + _traf(1, 90000, _trun([9000, 0]))), 1.1),
        (_box(b"moof", _traf(1, 90000, _trun([-4500], first_flags=False), wide=True)), 0.95),
        # audio is ignored, and a run with no samples has no offset
        (_box(b"moof", _traf(2, 48000, _trun([0])) + _traf(1, 180000, _trun([]))), 2.0),
        (_box(b"moof", _traf(2, 48000, _trun([0]))), None),
        (_box(b"moof", (4).to_bytes(4, "big") + b"bad!"), None),
        (_box(b"moof", _box(b"traf", (4).to_bytes(4, "big") + b"bad!")), None),
    ],
)
def test_fragment_start(moof, expected):
    assert hls.fragment_start(moof) == (None if expected is None else pytest.approx(expected))


def _tfdt(data: bytes) -> list:
    found, i = [], data.find(b"tfdt")
    while i != -1:
        version = data[i + 4]
        found.append(int.from_bytes(data[i + 8 : i + (16 if version else 12)], "big"))
        i = data.find(b"tfdt", i + 4)
    return found


@needs_ffmpeg
async def test_fmp4_segments_carry_their_real_start_times(tmp_path):
    source = tmp_path / "in.mkv"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=24:duration=14",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=14",
         "-c:v", "libx265", "-x265-params", "keyint=48:min-keyint=48:scenecut=0:log-level=error",
         "-pix_fmt", "yuv420p", "-c:a", "ac3", str(source)],
        check=True,
    )  # fmt: skip
    media = await hls.plan(source, video_ok={"hevc"}, audio_ok={"aac"})
    assert media.fmp4 and len(media.segments) >= 2
    server = hls.JitHlsServer(media, "127.0.0.1")
    url = await server.start()
    base = url.rsplit("/", 1)[0]
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url) as resp:
                assert '#EXT-X-MAP:URI="init.mp4"' in await resp.text()
            async with http.get(f"{base}/init.mp4") as resp:
                init = await resp.read()
            segments = []
            for i in (0, 1):
                async with http.get(f"{base}/seg{i:05d}.m4s") as resp:
                    assert resp.content_type == "video/iso.segment"
                    segments.append(await resp.read())
            async with http.get(f"{base}/seg00001.ts") as resp:
                assert resp.status == 404
    finally:
        await server.stop()
    assert init[4:8] == b"ftyp" and b"moov" in init and b"moof" not in init
    assert all(seg[4:8] == b"moof" for seg in segments)
    # Each track's first fragment starts at the segment's place on the timeline:
    # video in a 90 kHz timescale, audio at its 48 kHz sample rate
    video0, audio0 = _tfdt(segments[0])[:2]
    video1, audio1 = _tfdt(segments[1])[:2]
    start = media.segments[1].start
    assert video0 >= 0 and audio0 >= 0
    assert video1 / 90000 == pytest.approx(start + media.time_offset, abs=0.1)
    # the audio runs on from segment 0, so it starts with the keyframe's decode time, not its own
    assert audio1 / 48000 == pytest.approx(start + media.time_offset, abs=0.1)
    # The whole thing plays back as one file
    whole = tmp_path / "whole.mp4"
    whole.write_bytes(init + segments[0] + segments[1])
    probe = subprocess.run(["ffmpeg", "-v", "error", "-i", str(whole), "-f", "null", "-"], capture_output=True, text=True)
    assert probe.returncode == 0 and not probe.stderr.strip()


@needs_ffmpeg
async def test_convert_to_directory_writes_fmp4(tmp_path):
    source = tmp_path / "in.mkv"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
         "testsrc=size=160x120:rate=24:duration=6", "-c:v", "libx265", "-x265-params",
         "keyint=48:log-level=error", "-pix_fmt", "yuv420p", str(source)],
        check=True,
    )  # fmt: skip
    media = await hls.plan(source, video_ok={"hevc"})
    playlist = await hls.convert_to_directory(media, tmp_path / "out")
    names = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert "init.mp4" in names and "seg00000.m4s" in names and playlist.name == "index.m3u8"


# What ffprobe reads from just after a seek to a keyframe of the SNL episode: the first
# two decode times are unknown and the keyframe at 100.100 s is decoded at 99.933 s.
_AFTER_SEEK = b"100.100000,N/A,K__\n100.033000,N/A,___\n100.000000,100.000000,___\n100.067000,100.033000,___\n"


@pytest.mark.parametrize(
    "output,at,expected",
    [
        (_AFTER_SEEK, 100.1, 0.166 + 0.033 / 2),  # its lead, plus half a frame
        # no B-frames: decoded when shown
        (b"4.000000,4.000000,K__\n4.040000,4.040000,___\n", 4.0, 0.0),
        # the seek landed on another keyframe, or not on one, or too little to count from
        (_AFTER_SEEK, 91.758, None),
        (b"100.100000,N/A,___\n100.033000,99.9,___\n100.0,99.93,___\n", 100.1, None),
        (b"100.100000,N/A,K__\n100.033000,N/A,___\n100.000000,100.000000,___\n", 100.1, None),
        (b"", 100.1, None),
    ],
)
async def test_keyframe_delay_counts_back_to_the_keyframe(monkeypatch, output, at, expected):
    async def fake_run(*args):
        return output

    monkeypatch.setattr(hls, "_run", fake_run)
    delay = await hls.keyframe_delay(Path("x"), at)
    assert delay == (None if expected is None else pytest.approx(expected, abs=0.001))


async def test_settle_cut_measures_each_keyframe(monkeypatch, caplog):
    # x265 varies how early each keyframe is decoded, so one guess cannot fit them all
    leads = {6.5: 0.15, 13.0: None}

    async def fake_delay(source, at):
        asked.append(at)
        return leads[at]

    asked: list = []
    monkeypatch.setattr(hls, "keyframe_delay", fake_delay)
    media = hls.MediaPlan(Path("x"), 15.0, True, True, True, 0, cuts_guessed=True)
    media.segments = hls.plan_segments([(0.0, -0.05), (6.5, 6.45), (13.0, 12.95)], 15.0, target=6.0)
    guessed = [s.cut for s in media.segments]
    caplog.set_level("DEBUG", logger="castomatic.hls")
    for index in (0, 0, 1, 2):
        await hls.settle_cut(media, index)
    assert asked == [6.5, 13.0]  # once each; the last segment ends with the file
    assert media.segments[0].cut == pytest.approx(6.5 - 0.15 - 0.001)
    assert media.segments[0].duration == 6.5  # the playlist is unchanged
    assert [s.cut for s in media.segments[1:]] == guessed[1:]  # unreadable: keep the guess
    assert media.settled == {0, 1}
    assert "segment 0: cut 6.349 s, not the guessed 6.449 s" in caplog.text
    exact = hls.MediaPlan(Path("x"), 15.0, True, True, True, 0, segments=list(media.segments))
    await hls.settle_cut(exact, 0)
    assert asked == [6.5, 13.0] and not exact.settled  # decode times known: nothing to do


@needs_ffmpeg
async def test_segments_cut_from_a_matroska_index_neither_repeat_nor_drop_frames(tmp_path):
    # Every frame once, and audio that never jumps: a Chromecast keeps time by the audio,
    # and audio converted segment by segment made the picture stutter at every join
    from castomatic import mkvindex

    source = tmp_path / "in.mkv"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=24:duration=20",
         "-f", "lavfi", "-i", "sine=frequency=1000:duration=20",
         "-c:v", "libx265", "-x265-params", "log-level=error:keyint=48:min-keyint=48:bframes=4:b-adapt=2",
         "-pix_fmt", "yuv420p", "-c:a", "ac3", str(source)],
        check=True,
    )  # fmt: skip
    with source.open("rb") as f:

        def read_at(offset, size):
            f.seek(offset)
            return f.read(size)

        index = mkvindex.load(read_at, source.stat().st_size)
    assert index is not None and all(pts == dts for pts, dts in index.keyframes)
    media = await hls.plan(source, index=index, video_ok={"hevc"}, audio_ok=set())
    assert media.fmp4 and len(media.segments) > 2
    await hls.convert_to_directory(media, tmp_path / "out")
    init = (tmp_path / "out" / "init.mp4").read_bytes()
    shown, heard = [], []
    for i in range(len(media.segments)):
        out = tmp_path / f"seg{i}.mp4"
        out.write_bytes(init + (tmp_path / "out" / media.segment_name(i)).read_bytes())
        shown += _video_packets(out)
        heard.append(_video_packets(out, "a:0"))
    assert len(shown) == len({round(t, 3) for t in shown}) == 20 * 24
    # the audio flows on from one segment to the next: no gap and no overlap at any join
    for before, after in zip(heard, heard[1:]):
        frame = before[-1] - before[-2]
        assert after[0] == pytest.approx(before[-1] + frame, abs=0.001)


def _short_gops(path: Path, seconds: int = 30) -> None:
    """HEVC with a keyframe every second and AAC-bound audio: several 6 s segments."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size=96x64:rate=24:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=500:sample_rate=48000:duration={seconds}",
         "-c:v", "libx265", "-x265-params", "log-level=error:keyint=24:min-keyint=24",
         "-pix_fmt", "yuv420p", "-c:a", "ac3", str(path)],
        check=True,
    )  # fmt: skip


@needs_ffmpeg
async def test_runs_follow_playback_and_start_again_where_the_tv_seeks(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="castomatic.hls")
    source = tmp_path / "in.mkv"
    _short_gops(source, seconds=60)
    media = await hls.plan(source, video_ok={"hevc"}, audio_ok=set())
    assert media.fmp4 and len(media.segments) >= 9
    server = hls.JitHlsServer(media, "127.0.0.1")
    base = (await server.start()).rsplit("/", 1)[0]
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{base}/init.mp4") as resp:  # before any segment: a run from the start
                init = await resp.read()
            first = server._run
            for i in (0, 1):
                async with http.get(f"{base}/{media.segment_name(i)}") as resp:
                    assert resp.status == 200
            assert server._run is first  # the next segment came from the same run
            await asyncio.sleep(0.5)
            assert first.next <= 1 + hls.FragmentRun.AHEAD + 1  # and it waits, not running away
            async with http.get(f"{base}/{media.segment_name(8)}") as resp:  # a seek
                seek = await resp.read()
            assert server._run is not first and server._run.first == 8
            async with http.get(f"{base}/init.mp4") as resp:
                assert await resp.read() == init  # every run writes the same one
            async with http.get(f"{base}/{media.segment_name(1)}") as resp:  # still cached
                assert resp.status == 200
    finally:
        await server.stop()
    assert "ffmpeg run from segment 8" in caplog.text
    whole = tmp_path / "seek.mp4"
    whole.write_bytes(init + seek)
    # where the TV sought to (x265's leading frames are shown a few frames before the keyframe)
    assert _video_packets(whole)[0] == pytest.approx(media.segments[8].start + media.time_offset, abs=0.2)


@needs_ffmpeg
async def test_a_new_source_takes_over_the_run(tmp_path):
    source = tmp_path / "in.mkv"
    _short_gops(source, seconds=20)
    copy = tmp_path / "copy.mkv"
    copy.write_bytes(source.read_bytes())
    media = await hls.plan(source, video_ok={"hevc"}, audio_ok=set())
    server = hls.JitHlsServer(media, "127.0.0.1")
    wanted = asyncio.ensure_future(server._segment_bytes(2))
    await asyncio.sleep(0)
    first = server._run
    server.use_source(copy)  # the segment still wanted comes from the new source
    data = await asyncio.wait_for(wanted, 30)
    assert server._run is not first and data[4:8] == b"moof" and media.source == copy
    server.use_source(source)  # nobody waiting: nothing started
    assert server._run is None
    await server.stop()


@needs_ffmpeg
async def test_a_failed_run_is_an_http_error(tmp_path, caplog):
    media = _plan(fmp4=True, video_codec="hevc", copy_audio=False)
    media.source = tmp_path / "missing.mkv"
    server = hls.JitHlsServer(media, "127.0.0.1")
    base = (await server.start()).rsplit("/", 1)[0]
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{base}/init.mp4") as resp:
                assert resp.status == 500
            async with http.get(f"{base}/{media.segment_name(1)}") as resp:
                assert resp.status == 500
    finally:
        await server.stop()
    assert "missing.mkv" in caplog.text
    with pytest.raises(RuntimeError, match="missing.mkv"):
        await hls.convert_to_directory(media, tmp_path / "out")


async def test_a_run_that_writes_no_fragment_fails(monkeypatch):
    class Proc:
        returncode = None

        def __init__(self, out):
            self._out = list(out)
            self.stdout = SimpleNamespace(read=self._read)
            self.stderr = SimpleNamespace(read=self._nothing)

        async def _read(self, n=-1):
            return self._out.pop(0) if self._out else b""

        async def _nothing(self):
            return b""

        def kill(self):
            self.returncode = -9

        async def wait(self):
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

    outputs = [[b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00"], [(4).to_bytes(4, "big") + b"bad!"]]

    async def spawn(*args, **kw):
        return Proc(outputs.pop(0))

    monkeypatch.setattr(hls.asyncio, "create_subprocess_exec", spawn)
    media = _plan(fmp4=True, video_codec="hevc")
    for words in ("no media fragment", "broken MP4 box"):
        failures = []
        run = hls.FragmentRun(media, 0, lambda i, d: None, failures.append)
        run.start(to_the_end=True)
        await run.wait()
        assert run.done and words in str(failures[0])
        with pytest.raises(RuntimeError):
            run.init.result()


async def test_a_run_that_breaks_part_way_fails_the_conversion(monkeypatch, tmp_path):
    moov = (16).to_bytes(4, "big") + b"ftypisom" + b"\0" * 4 + (8).to_bytes(4, "big") + b"moov"
    fragment = (8).to_bytes(4, "big") + b"moof" + (8).to_bytes(4, "big") + b"mdat"

    class Proc:
        returncode = None

        def __init__(self):
            self._out = [moov + fragment, (4).to_bytes(4, "big") + b"bad!"]
            self.stdout = SimpleNamespace(read=self._read)
            self.stderr = SimpleNamespace(read=self._nothing)

        async def _read(self, n=-1):
            return self._out.pop(0) if self._out else b""

        async def _nothing(self):
            return b""

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def spawn(*args, **kw):
        return Proc()

    monkeypatch.setattr(hls.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match="broken MP4 box"):
        await hls.convert_to_directory(_plan(fmp4=True, video_codec="hevc"), tmp_path / "out")
    assert (tmp_path / "out" / "init.mp4").read_bytes() == moov
