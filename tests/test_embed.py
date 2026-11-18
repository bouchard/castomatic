"""Putting the subtitles inside the video, for TVs that ignore a subtitle file."""

import asyncio
import json
import subprocess
from pathlib import Path

import aiohttp
import pytest

from castomatic import cli, config, dlna, embed
from castomatic.sources import Input, MediaSource
from tests.test_cli import (  # noqa: F401  (fake_dlna is a fixture)
    SINK_MKV, FakeDlnaPlayer, _local_source, fake_dlna, needs_ffmpeg, parse, tv_target,
)

SRT = b"1\n00:00:01,000 --> 00:00:03,000\nHello\n\n"


def test_container_prefers_matroska_then_mp4():
    assert embed.container_for(Path("a.mp4"), {"video/mp4", "video/x-matroska"}) == (".mkv", "video/x-matroska", "srt")
    assert embed.container_for(Path("a.MP4"), {"video/mp4"}) == (".mp4", "video/mp4", "mov_text")
    assert embed.container_for(Path("a.avi"), {"video/mp4"}) is None
    assert embed.container_for(Path("a.mp4"), {"video/mpeg"}) is None


def test_command_copies_everything_and_makes_the_subtitles_default():
    mkv = embed.command(Path("in.mp4"), Path("s.srt"), Path("out.mkv"), "srt", "en")
    assert mkv[mkv.index("-c") + 1] == "copy" and "-disposition:s:0" in mkv
    assert "language=en" in mkv and "+faststart" not in mkv and mkv[-1] == "out.mkv"
    mp4 = embed.command(Path("in.mp4"), Path("s.srt"), Path("out.mp4"), "mov_text")
    assert "+faststart" in mp4 and not any(a.startswith("language=") for a in mp4)


def _streams(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name:stream_disposition=default",
         "-of", "json", str(path)],
        capture_output=True, check=True, text=True,
    ).stdout  # fmt: skip
    return json.loads(out)["streams"]


@needs_ffmpeg
@pytest.mark.parametrize("sink,codec", [(SINK_MKV, "subrip"), ({"video/mp4"}, "mov_text")])
async def test_embed_adds_a_default_subtitle_track(tmp_path, sink, codec):
    src, summ = await _local_source(tmp_path, "Movie.mp4")
    seen = []
    made = await embed.embed(src.path, SRT, sink, summ.duration, "eng", seen.append, folder=str(tmp_path))
    streams = _streams(made.path)
    subs = [s for s in streams if s["codec_type"] == "subtitle"]
    assert [s["codec_name"] for s in subs] == [codec] and subs[0]["disposition"]["default"] == 1
    assert {s["codec_type"] for s in streams} == {"video", "audio", "subtitle"}
    assert seen and seen[-1] <= 1.0
    made.close()
    assert not made.path.exists()


@needs_ffmpeg
async def test_embed_failures_say_why_and_leave_nothing_behind(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(embed.EmbedError, match="neither Matroska"):
        await embed.embed(tmp_path / "a.avi", SRT, {"video/mp4"}, folder=str(work))
    with pytest.raises(embed.EmbedError, match="No such file"):
        await embed.embed(tmp_path / "gone.mp4", SRT, SINK_MKV, folder=str(work))
    assert list(work.iterdir()) == []


def test_tvs_that_ignore_subtitle_links_are_remembered():
    assert not config.ignores_subtitle_links("dlna:uuid:1")
    config.remember_cannot_show("dlna:uuid:1", "hevc")
    config.remember_ignores_subtitle_links("dlna:uuid:1")
    assert config.ignores_subtitle_links("dlna:uuid:1")
    assert config.cannot_show("dlna:uuid:1") == {"hevc"}  # the other memory is kept
    config._write({"devices": {"dlna:uuid:2": "junk"}})
    config.remember_ignores_subtitle_links("dlna:uuid:2")
    config._write({"devices": []})
    config.remember_ignores_subtitle_links("dlna:uuid:3")
    assert config.ignores_subtitle_links("dlna:uuid:3") and not config.ignores_subtitle_links("dlna:uuid:2")


def test_a_dlna_player_remembers_by_its_renderer():
    player = dlna.DlnaPlayer(dlna.Renderer("http://10.0.0.5/d", "TV", "uuid:9", "http://10.0.0.5/c"))
    assert player.device_id == "dlna:uuid:9" and not player.ignores_subtitle_links()
    player.remember_ignores_subtitle_links()
    assert player.ignores_subtitle_links()
    assert dlna.DlnaPlayer(dlna.Renderer("http://10.0.0.6/d", "TV", "", "")).device_id == "dlna:10.0.0.6"


async def test_the_file_server_notices_what_the_tv_fetches(tmp_path):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"0123456789")

    class Sub:
        async def srt(self, offset=0.0):
            return SRT

    server = dlna.DirectFileServer(video, "127.0.0.1", subtitle=Sub())
    url = await server.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url) as resp:
                await resp.read()
            assert server.media_fetched.is_set() and not server.subtitle_fetched.is_set()
            async with http.get(server.subtitle_url) as resp:
                assert await resp.read() == SRT
        assert server.subtitle_fetched.is_set()
    finally:
        await server.stop()


# -- while playing -------------------------------------------------------------------------


class LearningPlayer(FakeDlnaPlayer):
    """A DLNA TV that fetches the video, maybe the subtitles, and plays until reloaded."""

    fetches_subtitles = False
    remembered = False
    plays = 2  # the play() call that ends the video

    def __init__(self, renderer):
        super().__init__(renderer)
        self.loads = []

    def ignores_subtitle_links(self):
        return LearningPlayer.remembered

    def remember_ignores_subtitle_links(self):
        LearningPlayer.remembered = True

    async def play(self, url, title="", start=0.0, subtitle_url=None):
        self.loads.append({"url": url, "start": start, "subtitle_url": subtitle_url})
        async with aiohttp.ClientSession() as http:
            async with http.get(url, headers={"Range": "bytes=0-99"}) as resp:
                await resp.read()
            if subtitle_url and self.fetches_subtitles:
                async with http.get(subtitle_url) as resp:
                    await resp.read()
        if len(self.loads) >= self.plays:
            self.ended.set()


@pytest.fixture
def learning(fake_dlna, monkeypatch):
    monkeypatch.setattr(LearningPlayer, "fetches_subtitles", False)
    monkeypatch.setattr(LearningPlayer, "remembered", False)
    monkeypatch.setattr(LearningPlayer, "plays", 2)
    monkeypatch.setattr(LearningPlayer, "position_seconds", 4.0)
    monkeypatch.setattr(cli.dlna, "DlnaPlayer", LearningPlayer)
    monkeypatch.setattr(cli, "SUBTITLE_FETCH_WAIT", 0.2)
    return LearningPlayer


async def _movie_with_subtitles(tmp_path):
    src, summ = await _local_source(tmp_path, "Movie.mp4")
    (tmp_path / "Movie.srt").write_bytes(SRT)
    return src, summ


@needs_ffmpeg
async def test_a_tv_that_ignores_the_subtitle_file_gets_them_inside_the_video(learning, tmp_path, capsys):
    src, summ = await _movie_with_subtitles(tmp_path)
    await cli._play_dlna(parse(str(src.path)), src, summ, tv_target(), None)
    first, second = FakeDlnaPlayer.last.loads
    assert first["url"].endswith("/media.mp4") and first["subtitle_url"].endswith("/subs.srt")
    assert second == {"url": second["url"], "start": 4.0, "subtitle_url": None}
    assert second["url"].endswith("/media.mkv") and FakeDlnaPlayer.last.mime == "video/x-mkv"
    assert learning.remembered
    out = capsys.readouterr().out
    assert "did not fetch the subtitle file" in out and "Starting again where it was" in out


@needs_ffmpeg
async def test_a_remembered_tv_gets_them_inside_the_video_from_the_start(learning, tmp_path, capsys):
    learning.remembered, learning.plays = True, 1
    src, summ = await _movie_with_subtitles(tmp_path)
    await cli._play_dlna(parse(str(src.path), "--start", "2"), src, summ, tv_target(), None)
    (load,) = FakeDlnaPlayer.last.loads
    assert load == {"url": load["url"], "start": 2.0, "subtitle_url": None} and load["url"].endswith(".mkv")
    assert "Adding the subtitles to the video" in capsys.readouterr().out


@needs_ffmpeg
async def test_a_tv_that_fetches_the_subtitle_file_is_left_alone(learning, tmp_path, capsys, monkeypatch):
    learning.fetches_subtitles, learning.plays = True, 1
    ended = asyncio.Event()

    class Slow(LearningPlayer):
        async def wait_until_ended(self):  # plays on past the subtitle check
            await asyncio.sleep(0.4)
            ended.set()

    monkeypatch.setattr(cli.dlna, "DlnaPlayer", Slow)
    src, summ = await _movie_with_subtitles(tmp_path)
    await cli._play_dlna(parse(str(src.path)), src, summ, tv_target(), None)
    assert len(FakeDlnaPlayer.last.loads) == 1 and ended.is_set() and not learning.remembered
    assert "inside the video" not in capsys.readouterr().out


@needs_ffmpeg
async def test_a_copy_that_cannot_be_made_is_a_warning(learning, tmp_path, capsys, monkeypatch):
    async def broken(*args, **kwargs):
        raise embed.EmbedError("disk full")

    monkeypatch.setattr(embed, "embed", broken)
    learning.remembered, learning.plays = True, 1
    src, summ = await _movie_with_subtitles(tmp_path)
    await cli._play_dlna(parse(str(src.path)), src, summ, tv_target(), None)
    (load,) = FakeDlnaPlayer.last.loads
    assert load["url"].endswith("/media.mp4") and load["subtitle_url"].endswith("/subs.srt")
    assert "could not be put inside the video: disk full" in capsys.readouterr().out


@needs_ffmpeg
async def test_playback_ending_first_stops_the_copy(learning, tmp_path, monkeypatch):
    started = asyncio.Event()

    async def forever(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(cli, "_embed_if_ignored", forever)
    learning.plays = 1
    src, summ = await _movie_with_subtitles(tmp_path)
    await cli._play_dlna(parse(str(src.path)), src, summ, tv_target(), None)
    assert len(FakeDlnaPlayer.last.loads) == 1


def test_embedded_source_plays_the_copy(tmp_path):
    src = MediaSource("Movie", [Input("/m/Movie.mp4")], Path("/m/Movie.mp4"))
    made = embed.Embedded(tmp_path / "video.mkv", "video/x-mkv", None)
    copy = cli._embedded_source(src, made)
    assert copy.title == "Movie" and copy.path == made.path and copy.location == str(made.path)
