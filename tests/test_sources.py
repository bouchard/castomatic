"""Resolving what the user typed: files, media URLs and web pages (yt-dlp)."""

import pytest
from aiohttp import web

from castomatic import sources
from castomatic.errors import UserError
from castomatic.sources import Input, MediaSource


async def test_resolve_local_file(tmp_path):
    video = tmp_path / "My Movie.mkv"
    video.write_bytes(b"x")
    source = await sources.resolve(str(video))
    assert source.is_local and source.title == "My Movie" and source.path == video.resolve()
    assert source.location == str(video.resolve()) and not source.is_hls_playlist


async def test_resolve_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "a.mp4").write_bytes(b"x")
    assert (await sources.resolve("~/a.mp4")).title == "a"


@pytest.mark.parametrize(
    "text,message",
    [
        ("/tmp", "is a folder"),
        ("/no/such/file.mkv", "no such file"),
        ("ftp://host/a.mkv", "only http:// and https://"),
    ],
)
async def test_resolve_errors_explain_what_to_do(text, message):
    with pytest.raises(UserError, match=message) as info:
        await sources.resolve(text)
    assert info.value.hint


async def test_direct_media_url_is_used_as_is():
    source = await sources.resolve("http://example.invalid/films/Big%20Film.mp4")
    assert source.title == "Big Film" and not source.is_local and not source.via_ytdlp
    assert source.inputs == [Input("http://example.invalid/films/Big%20Film.mp4")]


async def test_playlist_url_is_recognised():
    source = await sources.resolve("https://example.invalid/live/index.m3u8")
    assert source.is_hls_playlist
    two = MediaSource("t", [Input("a.m3u8"), Input("b.m3u8")])
    assert not two.is_hls_playlist


async def _server(handler):
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


@pytest.mark.parametrize(
    "content_type,expected",
    [("video/mp4", True), ("audio/mpeg", True), ("application/vnd.apple.mpegurl", True),
     ("text/html; charset=utf-8", False), ("application/json", False)],
)  # fmt: skip
async def test_is_direct_media_checks_the_content_type(content_type, expected):
    async def handler(request):
        return web.Response(body=b"", content_type=content_type.split(";")[0])

    runner, base = await _server(handler)
    try:
        assert await sources.is_direct_media(f"{base}/watch?v=1") is expected
    finally:
        await runner.cleanup()


async def test_is_direct_media_is_false_when_unreachable():
    assert await sources.is_direct_media("http://127.0.0.1:9/watch") is False


def _info(**extra):
    base = {"title": "Clip", "duration": 61.5}
    base.update(extra)
    return base


async def test_web_page_goes_to_ytdlp(monkeypatch):
    async def not_media(url):
        return False

    seen = {}

    def extract(url, fmt, cookies, verbose):
        seen.update(url=url, fmt=fmt, cookies=cookies, verbose=verbose)
        return _info(requested_formats=[
            {"url": "https://cdn/v.mp4", "http_headers": {"User-Agent": "x"}},
            {"url": "https://cdn/a.m4a"},
        ])

    monkeypatch.setattr(sources, "is_direct_media", not_media)
    monkeypatch.setattr(sources, "_extract", extract)
    source = await sources.resolve("https://video.example/watch?v=1", "best", "chrome", True)
    assert source.via_ytdlp and source.title == "Clip" and source.duration == 61.5
    assert source.inputs == [Input("https://cdn/v.mp4", {"User-Agent": "x"}), Input("https://cdn/a.m4a")]
    assert seen == {"url": "https://video.example/watch?v=1", "fmt": "best", "cookies": "chrome", "verbose": True}


async def test_ytdlp_single_format_and_playlist(monkeypatch):
    monkeypatch.setattr(sources, "_extract", lambda *a: _info(url="https://cdn/one.mp4"))
    assert (await sources.from_ytdlp("https://x/y")).inputs == [Input("https://cdn/one.mp4")]

    playlist = {"_type": "playlist", "entries": [None, _info(url="https://cdn/first.mp4", title=None)]}
    monkeypatch.setattr(sources, "_extract", lambda *a: playlist)
    source = await sources.from_ytdlp("https://x/some/page")
    assert source.inputs[0].location == "https://cdn/first.mp4" and source.title == "page"


async def test_ytdlp_failure_is_explained(monkeypatch):
    import yt_dlp.utils

    def boom(*args):
        raise yt_dlp.utils.DownloadError("\x1b[0;31mERROR:\x1b[0m [youtube] abc: Sign in to confirm")

    monkeypatch.setattr(sources, "_extract", boom)
    with pytest.raises(UserError, match="Sign in to confirm") as info:
        await sources.from_ytdlp("https://youtu.be/abc")
    assert "--cookies-from-browser" in info.value.hint and "\x1b" not in info.value.message


@pytest.mark.parametrize("info", [{"_type": "playlist", "entries": []}, {"title": "no url"}])
async def test_ytdlp_returning_nothing_playable(monkeypatch, info):
    monkeypatch.setattr(sources, "_extract", lambda *a: info)
    with pytest.raises(UserError, match="nothing playable"):
        await sources.from_ytdlp("https://x/y")


def test_clean_strips_colour_and_prefix():
    assert sources._clean("\x1b[0;31mERROR:\x1b[0m something\nERROR: last line") == "last line"
    assert sources._clean("") == "no details"


def test_extract_passes_options_to_yt_dlp(monkeypatch):
    import yt_dlp

    captured = {}

    class FakeYDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            return {"url": url, "download": download}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    result = sources._extract("https://x", "best", "firefox", False)
    assert result == {"url": "https://x", "download": False}
    assert captured["format"] == "best" and captured["cookiesfrombrowser"] == ("firefox",)
    assert captured["quiet"] and captured["noplaylist"]
    captured.clear()
    sources._extract("https://x", "best", None, True)
    assert not captured["quiet"] and "cookiesfrombrowser" not in captured
