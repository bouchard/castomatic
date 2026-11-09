"""castor as an optional engine, driven through a fake ``castor`` program on the PATH."""

import os
import stat
import sys

import pytest

from castomatic import sources
from castomatic.engines import castor
from castomatic.errors import UserError

FAKE = """#!{python}
import os, sys, time
args = sys.argv[1:]
log = os.environ.get("FAKE_CASTOR_LOG")
if log:
    with open(log, "a") as f:
        f.write(" ".join(args) + " | " + os.environ.get("CASTOR_DEVICE__TYPE", "") + " "
                + os.environ.get("CASTOR_DEVICE__HOST", "") + " " + os.environ.get("CASTOR_DEVICE__NAME", "") + "\\n")
mode = os.environ.get("FAKE_CASTOR_MODE", "ok")
if args == ["--version"]:
    print("castor version 2.5.0") if mode != "silent" else None
    sys.exit(0 if mode != "broken" else 3)
if "--dry-run" in args:
    if mode == "none":
        sys.exit(0)
    if mode == "fail":
        print("no player found", file=sys.stderr)
        sys.exit(1)
    if mode == "slow":
        time.sleep(5)
    print("unknown\\thttps://cdn.example/fallback.m3u8\\tlast resort")
    print("4000000\\thttps://cdn.example/best.m3u8")
    print("garbage line")
    sys.exit(0)
if args[:1] == ["cast"]:
    print("Casting...")
    print("Playing")
    sys.exit(0 if mode != "fail" else 2)
sys.exit(9)
"""


@pytest.fixture
def fake_castor(tmp_path, monkeypatch):
    path = tmp_path / "bin" / "castor"
    path.parent.mkdir()
    path.write_text(FAKE.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{path.parent}{os.pathsep}{os.environ['PATH']}")
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_CASTOR_LOG", str(log))
    monkeypatch.setenv("FAKE_CASTOR_MODE", "ok")
    return log


@pytest.fixture
def no_castor(monkeypatch):
    monkeypatch.setattr(castor.shutil, "which", lambda name: None)


def test_parse_streams_reads_the_dry_run_rows():
    rows = castor.parse_streams("123\thttps://a/x.m3u8\nunknown\thttp://b/y.mp4\tlast resort\nnope\n\t\n")
    assert [(s.url, s.bitrate, s.last_resort) for s in rows] == [
        ("https://a/x.m3u8", 123, False), ("http://b/y.mp4", None, True),
    ]  # fmt: skip


def test_clock_and_commands():
    assert castor.clock(0) == "0s" and castor.clock(65) == "1m5s" and castor.clock(4320) == "1h12m0s"
    assert castor.cast_command("castor", "https://site/watch?v=1") == ["castor", "cast", "player", "https://site/watch?v=1"]
    assert castor.cast_command("castor", "https://cdn/a.m3u8?sig=1", 90) == ["castor", "cast", "url", "https://cdn/a.m3u8?sig=1", "--at", "1m30s"]


def test_device_env_and_airplay_refusal():
    assert castor.device_env("roku", "10.0.0.4", "") == {
        "CASTOR_DEVICE__TYPE": "roku", "CASTOR_DEVICE__HOST": "10.0.0.4", "CASTOR_DEVICE__NAME": "10.0.0.4",
    }  # fmt: skip
    with pytest.raises(UserError, match="cannot play over airplay"):
        castor.device_env("airplay", "10.0.0.4", "TV")


async def test_resolve_page_puts_last_resorts_last(fake_castor):
    streams = await castor.resolve_page("https://site/watch")
    assert [s.url for s in streams] == ["https://cdn.example/best.m3u8", "https://cdn.example/fallback.m3u8"]
    assert "cast player https://site/watch --dry-run" in fake_castor.read_text()


@pytest.mark.parametrize("mode", ["none", "fail"])
async def test_resolve_page_finding_nothing(fake_castor, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CASTOR_MODE", mode)
    assert await castor.resolve_page("https://site/watch") == []


async def test_resolve_page_gives_up_after_its_time_limit(fake_castor, monkeypatch):
    monkeypatch.setenv("FAKE_CASTOR_MODE", "slow")
    assert await castor.resolve_page("https://site/watch", timeout=0.5) == []


async def test_resolve_page_when_castor_cannot_run(fake_castor, monkeypatch):
    monkeypatch.setattr(castor, "find", lambda: "/nonexistent/castor")
    assert await castor.resolve_page("https://site/watch") == []


async def test_version(fake_castor, monkeypatch):
    assert await castor.version() == "castor version 2.5.0"
    monkeypatch.setenv("FAKE_CASTOR_MODE", "broken")
    assert await castor.version() == "installed (version unknown)"
    monkeypatch.setattr(castor, "find", lambda: "/nonexistent/castor")
    assert await castor.version() is None


async def test_missing_castor(no_castor):
    assert castor.find() is None
    assert await castor.version() is None
    assert await castor.resolve_page("https://x") == []
    with pytest.raises(UserError, match="not installed"):
        await castor.cast("https://x", "dlna", "10.0.0.2", "TV")


async def test_cast_passes_the_tv_and_shows_castor_output(fake_castor):
    lines = []
    await castor.cast("https://site/watch", "chromecast", "10.0.0.8", "Den TV", 75, debug=True, on_line=lines.append)
    assert lines == ["  castor: Casting...", "  castor: Playing"]
    call = fake_castor.read_text().strip()
    assert call == "cast player https://site/watch --at 1m15s --debug | chromecast 10.0.0.8 Den TV"


async def test_cast_failure_is_reported(fake_castor, monkeypatch):
    monkeypatch.setenv("FAKE_CASTOR_MODE", "fail")
    with pytest.raises(UserError, match="exit status 2"):
        await castor.cast("https://cdn/a.mp4", "dlna", "10.0.0.2", "TV", on_line=lambda line: None)


async def test_cast_interrupted_stops_castor(fake_castor, monkeypatch):
    def interrupt(line):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await castor.cast("https://site/watch", "dlna", "10.0.0.2", "TV", on_line=interrupt)


# -- as a fallback when yt-dlp cannot read a page ----------------------------------------------


@pytest.fixture
def ytdlp_fails(monkeypatch):
    async def not_media(url):
        return False

    async def fails(url, fmt, cookies, verbose):
        raise UserError("yt-dlp could not get a video from it: Unsupported URL", "Try upgrading.")

    monkeypatch.setattr(sources, "is_direct_media", not_media)
    monkeypatch.setattr(sources, "from_ytdlp", fails)


async def test_castor_finds_what_yt_dlp_cannot(fake_castor, ytdlp_fails, capsys):
    source = await sources.resolve("https://site.example/watch/42")
    assert source.via_castor and source.location == "https://cdn.example/best.m3u8"
    assert source.page_url == "https://site.example/watch/42"
    assert "asking castor" in capsys.readouterr().out


async def test_without_castor_the_error_says_how_to_get_it(no_castor, ytdlp_fails):
    with pytest.raises(UserError) as caught:
        await sources.resolve("https://site.example/watch/42")
    assert "Unsupported URL" in caught.value.message
    assert "Try upgrading." in caught.value.hint and "brew install --cask stupside/tap/castor" in caught.value.hint


async def test_when_castor_finds_nothing_either_the_error_says_so(fake_castor, ytdlp_fails, monkeypatch):
    monkeypatch.setenv("FAKE_CASTOR_MODE", "none")
    with pytest.raises(UserError) as caught:
        await sources.resolve("https://site.example/watch/42")
    assert "castor was tried too" in caught.value.hint


async def test_hintless_ytdlp_error_still_gets_the_castor_note(no_castor, monkeypatch):
    async def not_media(url):
        return False

    async def fails(url, fmt, cookies, verbose):
        raise UserError("nope")

    monkeypatch.setattr(sources, "is_direct_media", not_media)
    monkeypatch.setattr(sources, "from_ytdlp", fails)
    with pytest.raises(UserError) as caught:
        await sources.resolve("https://site.example/x")
    assert caught.value.hint.startswith("castor can find videos")
