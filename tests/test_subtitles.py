"""Subtitle discovery, conversion and how they reach HLS and DLNA."""

import shutil
import subprocess
from pathlib import Path

import aiohttp
import pytest

from castomatic import dlna, hls, subtitles
from tests.test_hls import _make_video

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)

SRT = "1\n00:00:01,000 --> 00:00:03,500\nHello there\n\n2\n00:00:05,000 --> 00:00:06,000\nCafé\n\n"


def _touch(directory: Path, *names: str) -> Path:
    for name in names:
        (directory / name).write_text(SRT)
    return directory / names[0]


def test_sidecars_match_the_video_name(tmp_path):
    video = _touch(tmp_path, "Movie.mkv")
    _touch(tmp_path, "Movie.srt", "Movie.en.srt", "Movie.fr.vtt", "Other.srt", "Movie.txt")
    found = subtitles.sidecars(video)
    assert [(s.sidecar.name, s.language) for s in found] == [
        ("Movie.en.srt", "en"), ("Movie.fr.vtt", "fr"), ("Movie.srt", ""),
    ]  # fmt: skip
    assert found[0].name == "English" and found[1].name == "Fr" and found[2].name == "Subtitles"


def test_embedded_skips_bitmap_tracks():
    info = {"streams": [
        {"codec_type": "video"},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng"}},
        {"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "fra"}},
        {"codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "eng", "title": "Full"}},
        {"codec_type": "subtitle", "codec_name": "mov_text"},
    ]}  # fmt: skip
    found = subtitles.embedded(Path("m.mkv"), info)
    assert [(s.language, s.name, s.stream_index) for s in found] == [
        ("fra", "Fra", 1), ("eng", "Full", 2), ("", "Subtitles", 3),
    ]  # fmt: skip
    assert subtitles._prefer_english(found).name == "Full"
    assert subtitles._prefer_english(found[:1]).name == "Fra"
    assert subtitles._prefer_english([]) is None


def test_decode_handles_bom_and_legacy_encodings():
    assert subtitles._decode("﻿Café".encode("utf-8")) == "Café"
    assert subtitles._decode("Café".encode("cp1252")) == "Café"


async def test_find_prefers_explicit_then_sidecar(tmp_path, monkeypatch):
    video = _touch(tmp_path, "Movie.mkv")
    other = _touch(tmp_path, "custom.srt")
    assert (await subtitles.find(video, other)).sidecar == other
    with pytest.raises(FileNotFoundError):
        await subtitles.find(video, tmp_path / "missing.srt")

    _touch(tmp_path, "Movie.fr.srt", "Movie.en.srt")

    async def no_probe(path):
        raise AssertionError("a sidecar makes probing unnecessary")

    monkeypatch.setattr(hls, "probe", no_probe)
    assert (await subtitles.find(video)).sidecar.name == "Movie.en.srt"


async def test_find_falls_back_to_embedded_then_none(tmp_path, monkeypatch):
    video = _touch(tmp_path, "Movie.mkv")
    info = {"streams": [{"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}}]}

    async def probe(path):
        return info

    monkeypatch.setattr(hls, "probe", probe)
    assert (await subtitles.find(video)).video == video
    info["streams"] = []
    assert await subtitles.find(video) is None


@needs_ffmpeg
async def test_sidecar_converts_to_webvtt_and_srt(tmp_path):
    srt = tmp_path / "Movie.srt"
    srt.write_text(SRT.replace("Café", "Cafe"))
    sub = subtitles.Subtitle("en", "English", sidecar=srt)
    vtt = (await sub.webvtt()).decode()
    assert vtt.startswith("WEBVTT") and "00:01.000 --> 00:03.500" in vtt
    assert "00:00:01,000 --> 00:00:03,500" in (await sub.srt()).decode()
    assert await sub.webvtt() is await sub.webvtt()  # converted once


@needs_ffmpeg
async def test_legacy_encoded_sidecar_is_converted(tmp_path):
    srt = tmp_path / "Movie.srt"
    srt.write_bytes(SRT.encode("cp1252"))
    assert "Café" in (await subtitles.Subtitle("", "", sidecar=srt).webvtt()).decode()


@needs_ffmpeg
async def test_embedded_track_is_extracted(tmp_path):
    (tmp_path / "in.srt").write_text(SRT.replace("Café", "Cafe"))
    video = tmp_path / "in.mkv"
    _make_video(tmp_path / "plain.mkv", "libx264", "aac")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(tmp_path / "plain.mkv"),
         "-i", str(tmp_path / "in.srt"), "-map", "0", "-map", "1", "-c", "copy", "-c:s", "srt",
         "-metadata:s:s:0", "language=eng", str(video)],
        check=True,
    )  # fmt: skip
    (sub,) = subtitles.embedded(video, await hls.probe(video))
    assert "Hello there" in (await sub.webvtt()).decode()


def _plan(**kwargs):
    from tests.test_hls import _plan as plan

    media = plan(**kwargs)
    media.subtitle = subtitles.Subtitle("en", "English", sidecar=Path("x.srt"))
    media.resolution, media.codecs, media.bandwidth = "1920x800", "avc1.640029,mp4a.40.2", 4_000_000
    return media


def test_master_playlist_offers_the_subtitle_track():
    media = _plan()
    master = media.master()
    assert media.entry_name == "master.m3u8"
    assert 'TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",LANGUAGE="en",DEFAULT=YES' in master
    assert 'URI="subs.m3u8"' in master
    assert 'BANDWIDTH=4000000,RESOLUTION=1920x800,CODECS="avc1.640029,mp4a.40.2",SUBTITLES="subs"' in master
    assert master.rstrip().endswith("index.m3u8")
    media.subtitle.language, media.codecs = "", None
    assert "LANGUAGE" not in media.master() and "CODECS" not in media.master()


def test_subtitle_playlist_and_timestamp_map():
    media = _plan(duration=95.2)
    lines = media.subtitle_playlist().splitlines()
    assert "#EXT-X-TARGETDURATION:96" in lines and "subs.vtt" in lines
    assert lines[-1] == "#EXT-X-ENDLIST"
    shifted = media.webvtt(b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n")
    assert shifted.startswith(b"WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:7470,LOCAL:00:00:00.000\n\n00:00:01")


def test_media_without_subtitles_keeps_the_plain_playlist():
    from tests.test_hls import _plan as plan

    assert plan().entry_name == "index.m3u8"


def test_h264_codec_string():
    assert hls.h264_codec_string("High", 41) == "avc1.640029"
    assert hls.h264_codec_string("Constrained Baseline", 30) == "avc1.42001E"
    assert hls.h264_codec_string("Main", 31) == "avc1.4D001F"
    assert hls.h264_codec_string(None, None) == "avc1.640028"


@needs_ffmpeg
async def test_server_serves_the_subtitle_track(tmp_path):
    source = tmp_path / "Movie.mkv"
    _make_video(source, "libx264", "aac")
    (tmp_path / "Movie.srt").write_text(SRT.replace("Café", "Cafe"))
    sub = await subtitles.find(source)
    media = await hls.plan(source, sub)
    assert media.codecs.startswith("avc1.") and media.codecs.endswith("mp4a.40.2")

    server = hls.JitHlsServer(media, "127.0.0.1")
    url = await server.start()
    try:
        assert url.endswith("/master.m3u8")
        async with aiohttp.ClientSession() as http:
            texts = {}
            for name in ("master.m3u8", "index.m3u8", "subs.m3u8", "subs.vtt"):
                async with http.get(url.replace("master.m3u8", name)) as resp:
                    assert resp.status == 200
                    texts[name] = await resp.text()
    finally:
        await server.stop()
    assert "SUBTITLES" in texts["master.m3u8"] and "#EXT-X-ENDLIST" in texts["subs.m3u8"]
    assert "X-TIMESTAMP-MAP=MPEGTS:" in texts["subs.vtt"] and "Hello there" in texts["subs.vtt"]


@needs_ffmpeg
async def test_no_subtitles_means_no_subtitle_routes(tmp_path):
    source = tmp_path / "in.mkv"
    _make_video(source, "libx264", "aac")
    server = hls.JitHlsServer(await hls.plan(source), "127.0.0.1")
    url = await server.start()
    try:
        async with aiohttp.ClientSession() as http:
            for name in ("master.m3u8", "subs.vtt"):
                async with http.get(url.replace("index.m3u8", name)) as resp:
                    assert resp.status == 404
    finally:
        await server.stop()


@needs_ffmpeg
async def test_convert_to_directory_writes_the_subtitle_files(tmp_path):
    source = tmp_path / "Movie.mkv"
    _make_video(source, "libx264", "aac")
    (tmp_path / "Movie.srt").write_text(SRT.replace("Café", "Cafe"))
    media = await hls.plan(source, await subtitles.find(source))
    entry = await hls.convert_to_directory(media, tmp_path / "out")
    assert entry.name == "master.m3u8"
    assert sorted(p.name for p in (tmp_path / "out").glob("*") if not p.name.startswith("seg")) == [
        "index.m3u8", "master.m3u8", "subs.m3u8", "subs.vtt",
    ]  # fmt: skip


def test_didl_announces_the_subtitle_to_samsung_and_others():
    text = dlna.didl("http://h/a.mp4", "T", "video/mp4", "http://h/subs.srt")
    assert 'xmlns:sec="http://www.sec.co.kr/"' in text
    assert '<sec:CaptionInfoEx sec:type="srt">http://h/subs.srt</sec:CaptionInfoEx>' in text
    assert 'protocolInfo="http-get:*:text/srt:*">http://h/subs.srt</res>' in text
    assert "CaptionInfoEx" not in dlna.didl("http://h/a.mp4", "T", "video/mp4")


@needs_ffmpeg
async def test_direct_server_serves_srt_and_announces_it(tmp_path):
    media = tmp_path / "movie.mp4"
    media.write_bytes(b"x" * 10)
    srt = tmp_path / "movie.srt"
    srt.write_text(SRT.replace("Café", "Cafe"))
    server = dlna.DirectFileServer(media, "127.0.0.1", subtitle=subtitles.Subtitle("", "", sidecar=srt))
    url = await server.start()
    try:
        assert server.subtitle_url == url.replace("media.mp4", "subs.srt")
        async with aiohttp.ClientSession() as http:
            async with http.get(url) as resp:
                assert resp.headers["CaptionInfo.sec"] == server.subtitle_url
            async with http.get(server.subtitle_url) as resp:
                assert resp.content_type == "text/srt" and "Hello there" in await resp.text()
    finally:
        await server.stop()


async def test_direct_server_without_subtitle_has_no_caption_header(tmp_path):
    media = tmp_path / "movie.mp4"
    media.write_bytes(b"x")
    server = dlna.DirectFileServer(media, "127.0.0.1")
    url = await server.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url) as resp:
                assert "CaptionInfo.sec" not in resp.headers and server.subtitle_url is None
            async with http.get(url.replace("media.mp4", "subs.srt")) as resp:
                assert resp.status == 404
    finally:
        await server.stop()


@needs_ffmpeg
async def test_srt_can_skip_the_first_seconds(tmp_path):
    srt = tmp_path / "Movie.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nEarly\n\n2\n00:00:12,000 --> 00:00:13,000\nLater\n\n")
    sub = subtitles.Subtitle("", "", sidecar=srt)
    shifted = (await sub.srt(10.0)).decode()
    assert "Later" in shifted and "00:00:02,000 --> 00:00:03,000" in shifted
    assert "Early" not in shifted


def test_shift_srt_moves_clamps_and_drops_cues():
    data = (
        "1\n00:00:01,000 --> 00:00:03,000\nGone\n\n"
        "2\n00:00:08,000 --> 00:00:12,500\nPartly gone\nsecond line\n\n"
        "3\n00:01:10,250 --> 00:01:11,000\nKept\n\n"
    ).encode()
    shifted = subtitles.shift_srt(data, 10.0).decode()
    assert shifted == (
        "1\n00:00:00,000 --> 00:00:02,500\nPartly gone\nsecond line\n\n"
        "2\n00:01:00,250 --> 00:01:01,000\nKept\n\n"
    )
    assert subtitles.shift_srt(data, 10000) == b""
    assert subtitles.shift_srt(b"WEBVTT garbage with no cues", 1) == b""


async def test_unreadable_file_just_means_no_subtitles(tmp_path, monkeypatch):
    video = _touch(tmp_path, "Movie.mkv")

    async def fails(path):
        raise RuntimeError("ffprobe failed: Invalid data")

    monkeypatch.setattr(hls, "probe", fails)
    assert await subtitles.find(video) is None
