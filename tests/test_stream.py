"""Conversion decisions, the ffmpeg command and the live MPEG-TS stream server."""

import asyncio
import shutil
import subprocess
from pathlib import Path

import aiohttp
import pytest

from castomatic import stream
from castomatic.errors import UserError
from castomatic.sources import Input
from tests.test_hls import _make_video

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

SINK = {"video/mp4", "video/x-mkv", "video/vnd.dlna.mpeg-tts"}


def _info(video="h264", pix="yuv420p", audio=("aac",), duration="60.0", extra_streams=()):
    streams = [{"codec_type": "video", "codec_name": video, "pix_fmt": pix}]
    for codec in audio:
        streams.append({"codec_type": "audio", "codec_name": codec})
    return {"streams": streams + list(extra_streams), "format": {"duration": duration}}


def _summary(**kw):
    return stream.summarize([_info(**kw)])


def test_summarize_picks_english_audio_and_ignores_cover_art():
    info = _info(extra_streams=[])
    info["streams"] = [
        {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
        {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"},
        {"codec_type": "audio", "codec_name": "ac3", "tags": {"language": "fra"}},
        {"codec_type": "audio", "codec_name": "aac", "tags": {"language": "eng"}},
    ]
    summary = stream.summarize([info])
    assert (summary.video_codec, summary.audio_codec, summary.audio_index) == ("h264", "aac", 1)
    assert summary.duration == 60.0


def test_summarize_separate_video_and_audio_inputs():
    video_only = {"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"}], "format": {}}
    audio_only = {"streams": [{"codec_type": "audio", "codec_name": "opus"}], "format": {"duration": "10"}}
    summary = stream.summarize([video_only, audio_only])
    assert (summary.video_input, summary.audio_input, summary.audio_codec) == (0, 1, "opus")
    assert summary.duration == 10.0
    assert stream.summarize([video_only]).audio_input is None


def test_summarize_needs_a_video_stream():
    with pytest.raises(UserError, match="no video stream"):
        stream.summarize([{"streams": [{"codec_type": "audio", "codec_name": "aac"}], "format": {}}])


@pytest.mark.parametrize(
    "location,kwargs,expected",
    [
        ("/m/a.mp4", {}, []),
        ("/m/a.mkv", {"audio": ()}, []),
        ("/m/a.mp4", {"video": "hevc", "pix": "yuv420p10le"}, []),
        ("/m/a.avi", {}, ["the TV does not list .avi files"]),
        ("/m/a.mp4", {"audio": ("dts",)}, ["audio is dts"]),
        ("/m/a.mkv", {"video": "av1", "pix": "yuv420p", "audio": ("flac",)}, ["video is av1", "audio is flac"]),
        ("/m/a.mp4", {"pix": "yuv420p10le"}, ["video is yuv420p10le h264"]),
        ("https://h/a", {}, ["the TV does not list this container files"]),
    ],
)  # fmt: skip
def test_conversion_reasons(location, kwargs, expected):
    assert stream.conversion_reasons(_summary(**kwargs), location, SINK) == expected


def test_unknown_renderer_capabilities_fall_back_to_common_types():
    assert stream.conversion_reasons(_summary(), "/m/a.mp4", set()) == []
    assert stream.conversion_reasons(_summary(), "/m/a.webm", set())


def test_matching_mime_uses_the_renderers_spelling():
    assert stream.matching_mime("/m/a.mkv", {"video/x-matroska"}) == "video/x-matroska"
    assert stream.matching_mime("/m/a.mkv", SINK) == "video/x-mkv"
    assert stream.matching_mime("/m/a.mkv", set()) == "video/x-mkv"
    assert stream.matching_mime("/m/a.unknown", SINK) == "video/mp4"


@pytest.mark.parametrize(
    "kwargs,force,video,audio,text",
    [
        ({}, False, True, True, "copying video, copying audio"),
        ({"audio": ("flac",)}, False, True, False, "copying video, converting audio to AAC"),
        ({"video": "vp9"}, False, False, True, "converting video to H.264, copying audio"),
        ({}, True, False, False, "converting video to H.264, converting audio to AAC"),
        ({"audio": ()}, False, True, False, "copying video"),
    ],
)  # fmt: skip
def test_plan_copies_what_it_can(kwargs, force, video, audio, text):
    plan = stream.make_plan([Input("/m/a.mkv")], _summary(**kwargs), force)
    assert (plan.copy_video, plan.copy_audio, plan.description) == (video, audio, text)


def test_command_copies_and_skips_ahead(monkeypatch):
    plan = stream.make_plan([Input("/m/a.mkv")], _summary(), False)
    cmd = stream.stream_command(plan, start=90)
    assert cmd[cmd.index("-ss") + 1] == "90.000" and cmd[cmd.index("-i") + 1] == "/m/a.mkv"
    assert cmd[cmd.index("-c:v") + 1] == "copy" and cmd[cmd.index("-c:a") + 1] == "copy"
    assert "h264_mp4toannexb" in cmd and cmd[-3:] == ["-f", "mpegts", "pipe:1"]
    assert "-reconnect" not in cmd and "-ss" not in stream.stream_command(plan)


def test_command_for_remote_inputs_with_headers_and_two_inputs():
    summary = stream.summarize([
        {"streams": [{"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv420p"}], "format": {}},
        {"streams": [{"codec_type": "audio", "codec_name": "opus"}], "format": {}},
    ])  # fmt: skip
    inputs = [Input("https://cdn/v", {"User-Agent": "ua"}), Input("https://cdn/a")]
    cmd = stream.stream_command(stream.make_plan(inputs, summary), start=5)
    assert cmd.count("-reconnect") == 2 and cmd.count("-ss") == 2
    assert cmd[cmd.index("-headers") + 1] == "User-Agent: ua\r\n"
    assert cmd[cmd.index("-map") + 1] == "0:v:0" and "1:a:0" in cmd
    assert "hevc_mp4toannexb" in cmd and cmd[cmd.index("-c:a") + 1] == "aac"


@pytest.mark.parametrize("encoder", ["libx264", "h264_videotoolbox"])
def test_command_transcodes_with_the_best_encoder(monkeypatch, encoder):
    monkeypatch.setattr(stream, "video_encoder", lambda: encoder)
    plan = stream.make_plan([Input("/m/a.webm")], _summary(video="vp9"), False)
    cmd = stream.stream_command(plan)
    assert cmd[cmd.index("-c:v") + 1] == encoder and "-pix_fmt" in cmd


def test_video_encoder_prefers_hardware_on_a_mac(monkeypatch):
    stream.video_encoder.cache_clear()
    monkeypatch.setattr(stream.sys, "platform", "darwin")
    monkeypatch.setattr(
        stream.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=" V....D h264_videotoolbox ...", stderr=""),
    )  # fmt: skip
    assert stream.video_encoder() == "h264_videotoolbox"
    stream.video_encoder.cache_clear()
    monkeypatch.setattr(stream.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert stream.video_encoder() == "libx264"
    stream.video_encoder.cache_clear()
    monkeypatch.setattr(stream.sys, "platform", "linux")
    assert stream.video_encoder() == "libx264"
    stream.video_encoder.cache_clear()


def test_ensure_ffmpeg_names_whats_missing(monkeypatch):
    monkeypatch.setattr(stream.shutil, "which", lambda tool: None if tool == "ffprobe" else "/bin/x")
    with pytest.raises(UserError, match="ffprobe not found") as info:
        stream.ensure_ffmpeg()
    assert "brew install ffmpeg" in info.value.hint
    monkeypatch.setattr(stream.shutil, "which", lambda tool: "/bin/x")
    stream.ensure_ffmpeg()


@needs_ffmpeg
async def test_probe_input_reads_a_real_file(tmp_path):
    video = tmp_path / "in.mkv"
    _make_video(video, "libx264", "aac")
    info = await stream.probe_input(Input(str(video)))
    assert stream.summarize([info]).video_codec == "h264"


@needs_ffmpeg
@pytest.mark.parametrize("remote,words", [(False, "damaged"), (True, "expire")])
async def test_probe_failure_says_what_to_check(tmp_path, remote, words):
    junk = tmp_path / "junk.mkv"
    junk.write_text("not a video")
    location = "http://127.0.0.1:9/gone.mp4" if remote else str(junk)
    with pytest.raises(UserError, match="ffprobe could not read") as info:
        await stream.probe_input(Input(location))
    assert words in info.value.hint


async def _stream_server(tmp_path, **kwargs):
    video = tmp_path / "in.mkv"
    _make_video(video, "libx264", "aac")
    source = Input(str(video))
    summary = stream.summarize([await stream.probe_input(source)])
    server = stream.StreamServer(stream.make_plan([source], summary), "127.0.0.1", **kwargs)
    return server, await server.start()


@needs_ffmpeg
async def test_stream_server_converts_while_downloading(tmp_path):
    server, url = await _stream_server(tmp_path)
    try:
        async with aiohttp.ClientSession() as http:
            async with http.head(url) as resp:
                assert resp.status == 200 and resp.headers["Content-Type"] == "video/vnd.dlna.mpeg-tts"
                assert "DLNA.ORG_OP=00" in resp.headers["contentFeatures.dlna.org"]
            async with http.get(url) as resp:
                data = await resp.read()
        assert server.requests == 2 and server.last_error is None
    finally:
        await server.stop()
    out = tmp_path / "out.ts"
    out.write_bytes(data)
    codecs = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    assert sorted(set(codecs)) == ["aac", "h264"]


@needs_ffmpeg
async def test_stream_server_stops_ffmpeg_when_the_tv_disconnects(tmp_path):
    server, url = await _stream_server(tmp_path)
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url) as resp:
                await resp.content.read(1000)  # then drop the connection
        await asyncio.sleep(0.3)
        assert server.last_error is None
    finally:
        await server.stop()


@needs_ffmpeg
async def test_stream_server_reports_ffmpeg_failures(tmp_path):
    server, url = await _stream_server(tmp_path)
    server._plan.inputs[0].location = str(tmp_path / "vanished.mkv")
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url) as resp:
                await resp.read()
        assert "vanished.mkv" in server.last_error
    finally:
        await server.stop()


@needs_ffmpeg
async def test_stream_server_serves_a_subtitle_shifted_to_the_start(tmp_path):
    from castomatic import subtitles

    srt = tmp_path / "in.srt"
    srt.write_text("1\n00:00:02,000 --> 00:00:03,000\nEarly\n\n2\n00:00:12,000 --> 00:00:13,000\nLate\n\n")
    sub = subtitles.Subtitle("", "", sidecar=srt)
    server, url = await _stream_server(tmp_path, start=10.0, subtitle=sub)
    try:
        assert server.subtitle_url == url.replace("stream.ts", "subs.srt")
        async with aiohttp.ClientSession() as http:
            async with http.get(url, headers={"Range": "bytes=0-9"}) as resp:
                assert resp.headers["CaptionInfo.sec"] == server.subtitle_url
                await resp.content.read(10)
            async with http.get(server.subtitle_url) as resp:
                text = await resp.text()
        assert "Late" in text and "Early" not in text
    finally:
        await server.stop()
    await server.stop()


def test_describe_hides_signed_addresses():
    long_url = "https://rr2---sn-x.googlevideo.com/videoplayback?expire=1&sig=" + "a" * 500
    assert stream.describe(Input(long_url)) == "https://rr2---sn-x.googlevideo.com/…/videoplayback"
    assert stream.describe(Input("https://host.example")) == "https://host.example"
    assert stream.describe(Input("/Users/me/a.mkv")) == "/Users/me/a.mkv"


@needs_ffmpeg
async def test_probe_errors_carry_a_short_reason_and_no_address(monkeypatch):
    async def forbidden(*args, **kwargs):
        class Proc:
            returncode = 1

            async def communicate(self):
                return b"", b"https://cdn.example/v?sig=" + b"z" * 300 + b": Server returned 403 Forbidden (access denied)\n"

        return Proc()

    monkeypatch.setattr(stream.asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(stream.ProbeError) as info:
        await stream.probe_input(Input("https://cdn.example/v?sig=" + "z" * 300))
    assert info.value.reason == "Server returned 403 Forbidden (access denied)"
    assert "zzz" not in info.value.message and "refuses direct requests" in info.value.hint
