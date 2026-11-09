"""Trying the next way when one fails, the new protocols end to end, and the new commands."""

import argparse
import asyncio
import io
import logging
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from castomatic import banner, cli, config, planner, sources, stream, targets
from castomatic.backends import chromecast, roku
from castomatic.backends.base import CastError
from castomatic.engines import castor
from castomatic.errors import UserError
from castomatic.sources import Input, MediaSource
from castomatic.targets import TV, Target
from tests.test_backends import FakeCattDevice, FakeEcp, status
from tests.test_cli import FakeDlnaPlayer, parse, summary
from tests.test_hls import _make_video

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def every_way_tv(host="127.0.0.1"):
    return TV(
        "Den TV", host, None, None,
        chromecast.ChromecastInfo("Den TV", host), roku.RokuInfo("Den TV", host),
    )  # fmt: skip


# -- failover ------------------------------------------------------------------------------


@pytest.fixture
def scripted(monkeypatch):
    """Replace each protocol's player with one that follows a script of outcomes."""
    log = []
    script = {}

    def player(protocol):
        async def play(args, source, summ, target, workdir, buffered=None, fallback=None):
            log.append((protocol, args.mode, args.failover))
            outcome = script.get((protocol, args.mode))
            if outcome:
                raise outcome

        return play

    for name in targets.PROTOCOLS:
        monkeypatch.setattr(cli, f"_play_{name}", player(name))
    return SimpleNamespace(log=log, script=script)


async def test_untouched_video_the_tv_fetched_but_never_played_is_retried_converted(scripted, capsys):
    tv = every_way_tv()
    scripted.script[("chromecast", "auto")] = cli.StartFailed("Den TV fetched it but never played it.", fetched=True, untouched=True)
    await cli._play_with_failover(parse("a"), [Target(tv, "chromecast"), Target(tv, "roku")], None, None, None, None, None)
    assert scripted.log == [("chromecast", "auto", True), ("chromecast", "convert", True)]
    assert "Trying again, converting" in capsys.readouterr().out


async def test_a_tv_that_never_fetched_moves_on_to_the_next_protocol(scripted, capsys):
    tv = every_way_tv()
    scripted.script[("chromecast", "auto")] = cli.StartFailed("never asked for the video.")
    await cli._play_with_failover(parse("a"), [Target(tv, "chromecast"), Target(tv, "roku")], None, None, None, None, None)
    assert scripted.log == [("chromecast", "auto", True), ("roku", "auto", True)]
    assert "Trying Den TV over Roku instead" in capsys.readouterr().out


async def test_when_nothing_is_left_the_last_reason_is_the_error(scripted):
    tv = every_way_tv()
    scripted.script[("roku", "convert")] = cli.StartFailed("still nothing.", "Check the TV.")
    with pytest.raises(UserError, match="still nothing") as caught:
        await cli._play_with_failover(parse("a", "--mode", "convert"), [Target(tv, "roku")], None, None, None, None, None)
    assert caught.value.hint == "Check the TV."
    assert scripted.log == [("roku", "convert", False)]  # nothing after it: no failover


async def test_watchdog_hands_back_the_reason_when_another_way_is_left(monkeypatch, capsys):
    monkeypatch.setattr(cli, "START_WATCH_SECONDS", 0.05)
    monkeypatch.setattr(cli, "WATCH_INTERVAL", 0.01)
    monkeypatch.setattr(FakeDlnaPlayer, "states", ["TRANSITIONING"])
    failed = await cli._watch_start(FakeDlnaPlayer(None), SimpleNamespace(requests=0), "Den", failover=True)
    assert "never asked this computer" in failed.reason and not failed.fetched
    assert capsys.readouterr().out == ""  # nothing printed: the caller decides
    failed = await cli._watch_start(FakeDlnaPlayer(None), SimpleNamespace(requests=3), "Den", failover=True)
    assert failed.fetched


async def test_first_verdict_reads_a_receiver_that_gave_up():
    player = SimpleNamespace(error="unsupported codec", ended=asyncio.Event())
    player.wait_until_ended = player.ended.wait
    player.ended.set()
    watcher = asyncio.ensure_future(asyncio.sleep(10))
    failed = await cli._first_verdict(player, watcher, "Den")
    watcher.cancel()
    assert "could not play it: unsupported codec" in failed.reason and failed.fetched
    player.error = None
    watcher = asyncio.ensure_future(asyncio.sleep(10))
    assert await cli._first_verdict(player, watcher, "Den") is None  # simply finished
    watcher.cancel()


async def test_wait_raises_start_failed_with_failover(monkeypatch):
    monkeypatch.setattr(cli, "START_WATCH_SECONDS", 0.05)
    monkeypatch.setattr(cli, "WATCH_INTERVAL", 0.01)
    monkeypatch.setattr(FakeDlnaPlayer, "states", ["TRANSITIONING"])
    args = parse("a")
    args.failover = True
    with pytest.raises(cli.StartFailed):
        await cli._wait(FakeDlnaPlayer(None), SimpleNamespace(requests=0), "Den", MediaSource("a", [Input("x")]), summary(), args)


class CapsPlayer(FakeDlnaPlayer):
    """A fake player that states its capabilities, like Chromecast and Roku do."""

    caps = chromecast.CAPABILITIES

    async def capabilities(self):
        return self.caps


@pytest.fixture
def caps_player(monkeypatch):
    monkeypatch.setattr(CapsPlayer, "states", ["PLAYING"])
    monkeypatch.setattr(CapsPlayer, "connect_error", None)
    monkeypatch.setattr(CapsPlayer, "play_error", None)
    monkeypatch.setattr(CapsPlayer, "caps", chromecast.CAPABILITIES)
    monkeypatch.setattr(cli, "START_WATCH_SECONDS", 0.2)
    monkeypatch.setattr(cli, "WATCH_INTERVAL", 0.01)
    return CapsPlayer


async def test_connect_and_play_failures_become_start_failed_with_failover(caps_player):
    args = parse("http://h/a.mp4")
    args.failover = True
    target = Target(every_way_tv(), "chromecast")
    src = MediaSource("a", [Input("http://h/a.mp4")])
    caps_player.connect_error = OSError("unreachable")
    with pytest.raises(cli.StartFailed, match="cannot reach"):
        await cli._play_renderer(args, src, summary(), target, None, None, None, caps_player(None))
    caps_player.connect_error = None
    caps_player.play_error = CastError("LOAD_FAILED")
    with pytest.raises(cli.StartFailed, match="refused") as caught:
        await cli._play_renderer(args, src, summary(), target, None, None, None, caps_player(None))
    assert caught.value.untouched  # it was being sent as it is
    args.failover = False
    with pytest.raises(UserError, match="refused"):
        await cli._play_renderer(args, src, summary(), target, None, None, None, caps_player(None))
    caps_player.connect_error = chromecast.ChromecastError("no answer")
    with pytest.raises(UserError, match="cannot reach"):
        await cli._play_renderer(args, src, summary(), target, None, None, None, caps_player(None))


async def test_https_address_goes_straight_to_a_tv_that_fetches_it(caps_player, capsys):
    src = MediaSource("a", [Input("https://h/a.mp4")])
    await cli._play_renderer(parse("x"), src, summary(), Target(every_way_tv(), "chromecast"), None, None, None, caps_player(None))
    assert caps_player.last.played["url"] == "https://h/a.mp4" and caps_player.last.mime == "video/mp4"
    assert "no conversion needed" in capsys.readouterr().out


@needs_ffmpeg
async def test_subtitles_left_out_with_a_note_where_the_tv_cannot_take_them(caps_player, tmp_path, capsys):
    caps_player.caps = roku.CAPABILITIES
    video = tmp_path / "Movie.mp4"
    _make_video(video, "libx264", "aac")
    (tmp_path / "Movie.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    src = MediaSource("Movie", [Input(str(video))], video)
    summ = stream.summarize([await stream.probe_input(src.inputs[0])])
    await cli._play_renderer(parse(str(video)), src, summ, Target(every_way_tv(), "roku"), None, None, None, caps_player(None), seeks=False)
    assert "takes no separate subtitle file" in capsys.readouterr().out
    assert caps_player.last.played["subtitle_url"] is None


@needs_ffmpeg
async def test_chromecast_gets_webvtt_subtitles_with_an_untouched_file(caps_player, tmp_path):
    video = tmp_path / "Movie.mp4"
    _make_video(video, "libx264", "aac")
    (tmp_path / "Movie.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    src = MediaSource("Movie", [Input(str(video))], video)
    summ = stream.summarize([await stream.probe_input(src.inputs[0])])
    await cli._play_renderer(parse(str(video)), src, summ, Target(every_way_tv(), "chromecast"), None, None, None, caps_player(None))
    assert caps_player.last.played["subtitle_url"].endswith("/subs.vtt")


class FakeLive:
    def __init__(self, plan, directory, start=0.0):
        self.plan, self.directory, self.start_at = plan, directory, start
        self.failed = asyncio.Event()
        self.error = None
        self.stopped = False

    async def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        playlist = self.directory / "index.m3u8"
        playlist.write_text("#EXTM3U\n")
        return playlist

    async def stop(self):
        self.stopped = True


async def test_live_hls_conversion_for_a_web_video(caps_player, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "LiveHls", FakeLive)
    src = MediaSource("Clip", [Input("https://cdn/v.mp4")], via_ytdlp=True)
    args = parse("https://youtu.be/x", "--start", "20")
    await cli._play_renderer(args, src, summary(audio="ac3"), Target(every_way_tv(), "chromecast"), tmp_path, None, None, caps_player(None))
    played = caps_player.last.played
    assert played["url"].endswith("/index.m3u8") and played["start"] == 0.0
    assert caps_player.last.mime == cli.HLS_MIME
    out = capsys.readouterr().out
    assert "Converting live to HLS" in out and "converting audio to AAC" in out


async def test_live_server_reports_ffmpeg_failures(tmp_path):
    live = FakeLive(None, tmp_path / "live")
    server = cli._LiveServer(live, "127.0.0.1")
    assert server.requests is None and server.last_error is None
    await server.start()
    assert server.requests == 0
    live.error = "Connection reset"
    live.failed.set()
    assert server.last_error == "Connection reset"
    await server.stop()
    assert live.stopped


# -- end to end: real players against fake TVs --------------------------------------------------


@needs_ffmpeg
async def test_roku_end_to_end_sends_a_playable_file_untouched(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(roku, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(cli, "START_WATCH_SECONDS", 2.0)
    monkeypatch.setattr(cli, "WATCH_INTERVAL", 0.01)
    ecp = FakeEcp()
    await ecp.start()
    video = tmp_path / "Movie.mkv"
    _make_video(video, "libx264", "ac3")
    src = MediaSource("Movie", [Input(str(video))], video)
    summ = stream.summarize([await stream.probe_input(src.inputs[0])])
    tv = TV("Bedroom", "127.0.0.1", roku=roku.RokuInfo("Bedroom", "127.0.0.1", ecp.port))
    try:
        await cli._play_roku(parse(str(video)), src, summ, Target(tv, "roku"), tmp_path)
    finally:
        await ecp.runner.cleanup()
    assert ecp.inputs[0]["videoFormat"] == "mkv" and ecp.inputs[0]["u"].endswith("/media.mkv")
    assert ecp.fetched > 0
    assert "Sending the video as is" in capsys.readouterr().out


async def test_chromecast_end_to_end_through_catt(monkeypatch, capsys):
    monkeypatch.setattr(chromecast, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(FakeCattDevice, "connect_error", None)
    monkeypatch.setattr(FakeCattDevice, "load_error", None)
    monkeypatch.setattr(FakeCattDevice, "last", None)  # not one left over from another test
    monkeypatch.setattr(cli, "ChromecastPlayer", lambda info: chromecast.ChromecastPlayer(info, device_factory=FakeCattDevice))
    monkeypatch.setattr(cli, "START_WATCH_SECONDS", 5.0)
    monkeypatch.setattr(cli, "WATCH_INTERVAL", 0.01)
    src = MediaSource("Clip", [Input("https://cdn.example/clip.mp4")])
    tv = every_way_tv()
    args = parse("https://cdn.example/clip.mp4")
    args.failover = True
    task = asyncio.ensure_future(cli._play_chromecast(args, src, summary(duration=1.0), Target(tv, "chromecast"), None))
    while FakeCattDevice.last is None or FakeCattDevice.last._controller.loaded is None:
        await asyncio.sleep(0.01)
    media = FakeCattDevice.last._cast.media_controller
    media.status = status("PLAYING", 0.5)
    await asyncio.sleep(0.05)
    media.status = status("IDLE", 1.0, "FINISHED")
    await asyncio.wait_for(task, 3)
    url, kwargs = FakeCattDevice.last._controller.loaded
    assert url == "https://cdn.example/clip.mp4" and kwargs["content_type"] == "video/mp4"
    assert "Playing on Den TV" in capsys.readouterr().out


# -- run(): the pieces around playing -------------------------------------------------------------


def test_may_convert_live_and_player_lookup():
    local_src = MediaSource("a", [Input("/m/a.mkv")], Path("/m/a.mkv"))
    web = MediaSource("a", [Input("https://h/a")], via_ytdlp=True)
    assert not cli._may_convert_live("dlna", web, parse("x"))
    assert cli._may_convert_live("chromecast", web, parse("x"))
    assert not cli._may_convert_live("roku", local_src, parse("x"))
    assert cli._may_convert_live("roku", local_src, parse("x", "--start", "9"))
    assert cli._player_for("roku") is cli._play_roku


async def test_too_slow_to_stream_buffers_first(monkeypatch, capsys):
    rates = {"value": 100_000.0}

    async def measure(item):
        return rates["value"]

    monkeypatch.setattr(cli.sources, "measure_url_rate", measure)
    src = MediaSource("a", [Input("http://h/a.mp4")])
    info = [{"format": {"bit_rate": "8000000"}}]  # 1 MB/s
    assert await cli._too_slow_to_stream(parse("x"), src, info)
    assert "buffered here first" in capsys.readouterr().out
    rates["value"] = 5_000_000.0
    assert not await cli._too_slow_to_stream(parse("x"), src, info)
    rates["value"] = None
    assert not await cli._too_slow_to_stream(parse("x"), src, info)
    assert not await cli._too_slow_to_stream(parse("x"), src, [{"format": {"bit_rate": "n/a"}}])
    assert not await cli._too_slow_to_stream(parse("x"), src, [{"format": {}}])
    assert not await cli._too_slow_to_stream(parse("x", "--download-first"), src, info)
    hls_src = MediaSource("a", [Input("http://h/a.m3u8")])
    assert not await cli._too_slow_to_stream(parse("x"), hls_src, info)


@pytest.fixture
def castor_env(monkeypatch):
    env = SimpleNamespace(cast=[], found=[Target(every_way_tv(), "chromecast")])

    async def resolve_targets(spec, protocol, choose, credentials):
        return env.found

    async def cast(what, protocol, host, name, start=0.0, debug=False):
        env.cast.append((what, protocol, host, name, start))

    monkeypatch.setattr(cli.targets, "resolve_targets", resolve_targets)
    monkeypatch.setattr(castor, "cast", cast)
    monkeypatch.setattr(castor, "find", lambda: "/usr/local/bin/castor")
    return env


async def test_engine_castor_hands_over_the_page(castor_env, monkeypatch, capsys):
    monkeypatch.setattr(cli.stream, "ensure_ffmpeg", lambda: None)

    async def resolve(text, fmt, cookies, verbose):
        return MediaSource("Page", [Input("https://cdn/v.m3u8")], via_ytdlp=True, page_url=text)

    monkeypatch.setattr(cli.sources, "resolve", resolve)
    await cli.run(parse("https://site/watch", "--engine", "castor", "--start", "60"))
    assert castor_env.cast == [("https://site/watch", "chromecast", "127.0.0.1", "Den TV", 60.0)]
    assert "via castor" in capsys.readouterr().out


async def test_castor_stream_that_cannot_be_probed_is_played_by_castor(castor_env, monkeypatch, capsys):
    monkeypatch.setattr(cli.stream, "ensure_ffmpeg", lambda: None)

    async def resolve(text, fmt, cookies, verbose):
        return MediaSource("Page", [Input("https://cdn/v.m3u8")], page_url=text, via_castor=True)

    async def probe(item):
        raise stream.ProbeError("no", "hint", "403 Forbidden")

    monkeypatch.setattr(cli.sources, "resolve", resolve)
    monkeypatch.setattr(cli.stream, "probe_input", probe)
    await cli.run(parse("https://site/watch"))
    assert castor_env.cast[0][0] == "https://site/watch"
    out = capsys.readouterr().out
    assert "castor found the video" in out and "letting castor play it" in out


async def test_castor_cannot_reach_an_airplay_only_tv(castor_env, monkeypatch):
    tv = TV("Apple TV", "10.0.0.3", airplay=targets.AirPlayInfo("id", "10.0.0.3", 7000, object()))
    castor_env.found = [Target(tv, "airplay")]
    with pytest.raises(UserError, match="castor cannot reach Apple TV"):
        await cli._cast_with_castor(parse("x"), MediaSource("a", [Input("https://h/a")]))
    monkeypatch.setattr(castor, "find", lambda: None)
    with pytest.raises(UserError, match="needs castor"):
        await cli._cast_with_castor(parse("x"), MediaSource("a", [Input("https://h/a")]))


async def test_run_tries_ways_in_the_planned_order(monkeypatch, scripted, capsys):
    monkeypatch.setattr(cli.stream, "ensure_ffmpeg", lambda: None)
    path = Path("/m/a.mkv")

    async def resolve(text, fmt, cookies, verbose):
        return MediaSource("a", [Input(str(path))], path)

    async def probe(item):
        return {"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"},
                            {"codec_type": "audio", "codec_name": "ac3"}], "format": {"duration": "60"}}  # fmt: skip

    async def resolve_targets(spec, protocol, choose, credentials):
        tv = every_way_tv()
        return [Target(tv, "chromecast"), Target(tv, "roku")]

    async def no_buffer(path, duration, on_note=None):
        return None

    monkeypatch.setattr(cli.sources, "resolve", resolve)
    monkeypatch.setattr(cli.stream, "probe_input", probe)
    monkeypatch.setattr(cli.targets, "resolve_targets", resolve_targets)
    monkeypatch.setattr(cli.buffering, "choose", no_buffer)
    monkeypatch.setattr(cli.buffering, "Fallback", lambda *a, **k: SimpleNamespace(media=None))
    await cli.run(parse(str(path), "-v"))
    # MKV with AC3: Roku plays it untouched, so it goes first
    assert scripted.log[0][0] == "roku"
    assert "a  ->  Den TV (ROKU)" in capsys.readouterr().out


# -- explain -------------------------------------------------------------------------------------


@pytest.fixture
def explain_env(monkeypatch):
    monkeypatch.setattr(cli.stream, "ensure_ffmpeg", lambda: None)
    env = SimpleNamespace(source=MediaSource("Movie", [Input("/m/a.mkv")], Path("/m/a.mkv")), targets=None)

    async def resolve(text, fmt, cookies, verbose):
        return env.source

    async def probe(item):
        return {"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"},
                            {"codec_type": "audio", "codec_name": "dts"}],
                "format": {"duration": "60", "bit_rate": "8000000"}}  # fmt: skip

    async def resolve_targets(spec, protocol, choose, credentials, interactive=None):
        if env.targets is None:
            raise UserError("no TV was found on the network.")
        return env.targets

    async def rate(path):
        return 50_000_000.0

    monkeypatch.setattr(cli.sources, "resolve", resolve)
    monkeypatch.setattr(cli.stream, "probe_input", probe)
    monkeypatch.setattr(cli.targets, "resolve_targets", resolve_targets)
    monkeypatch.setattr(cli.buffering, "measure_rate", rate)
    monkeypatch.setattr(cli.buffering, "is_network_path", lambda path: False)
    monkeypatch.setattr(Path, "stat", lambda self, **kw: SimpleNamespace(st_size=60_000_000, st_mode=0o100644))
    return env


async def test_explain_without_a_tv_shows_every_kind(explain_env, capsys):
    await cli.explain(parse("/m/a.mkv"))
    out = capsys.readouterr().out
    assert "Streams:   video h264 (yuv420p), audio dts, 60s" in out
    assert "Buffering: not needed" in out
    assert "TV:        none" in out and "each kind of TV" in out
    assert "1. Chromecast" in out and "Roku" in out and "AirPlay" in out
    assert "What each protocol plays as it is" in out


async def test_explain_with_a_tv(explain_env, monkeypatch, capsys):
    tv = every_way_tv()
    explain_env.targets = [Target(tv, "chromecast"), Target(tv, "roku")]
    monkeypatch.setattr(cli.buffering, "measure_rate", lambda path: _value(1_000_000.0))
    await cli.explain(parse("/m/a.mkv", "--start", "30"))
    out = capsys.readouterr().out
    assert "TV:        Den TV (127.0.0.1)" in out
    assert "Buffering: yes" in out
    assert "cannot be told to start part way" in out


async def test_explain_web_and_remote_sources(explain_env, monkeypatch, capsys):
    explain_env.source = MediaSource("Web", [Input("https://cdn/v")], via_ytdlp=True)
    await cli.explain(parse("https://youtu.be/x"))
    assert "web videos are buffered" in capsys.readouterr().out
    explain_env.source = MediaSource("Remote", [Input("http://h/a.mp4")])
    for value, words in [(5_000_000.0, "not needed"), (100_000.0, "Buffering: yes"), (None, "unknown")]:
        monkeypatch.setattr(cli.sources, "measure_url_rate", lambda item, v=value: _value(v))
        await cli.explain(parse("http://h/a.mp4"))
        assert words in capsys.readouterr().out
    monkeypatch.setattr(cli.buffering, "measure_rate", lambda path: _value(None))
    explain_env.source = MediaSource("Movie", [Input("/m/a.mkv")], Path("/m/a.mkv"))
    await cli.explain(parse("/m/a.mkv"))
    assert "could not be measured" in capsys.readouterr().out


async def test_explain_when_ffmpeg_cannot_read_it(explain_env, monkeypatch, capsys):
    async def probe(item):
        raise stream.ProbeError("no", "hint", "403 Forbidden")

    monkeypatch.setattr(cli.stream, "probe_input", probe)
    explain_env.source = MediaSource("Web", [Input("https://cdn/v")], via_castor=True, page_url="https://site/x")
    await cli.explain(parse("https://site/x"))
    out = capsys.readouterr().out
    assert "found by castor" in out and "cannot be read directly" in out
    with pytest.raises(UserError, match="nothing to explain"):
        await cli.explain(parse())


async def _value(v):
    return v


# -- logging, banner, entry points ----------------------------------------------------------------


def test_setup_logging_levels(tmp_path):
    cli.setup_logging(0)
    assert logging.getLogger().level == logging.WARNING
    cli.setup_logging(1)
    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger("pychromecast").level == logging.WARNING
    log = tmp_path / "castomatic.log"
    cli.setup_logging(0, str(log))
    logging.getLogger("castomatic.test").debug("into the file")
    assert "into the file" in log.read_text()
    cli.setup_logging(2)
    assert logging.getLogger().level == logging.DEBUG
    with pytest.raises(UserError, match="cannot write the log"):
        cli.setup_logging(0, str(tmp_path / "missing" / "dir" / "x.log"))
    cli.setup_logging(0)


def test_main_dispatches_doctor_and_explain(monkeypatch):
    seen = []

    async def doctor_run(args):
        seen.append(("doctor", args.save))
        return 1

    async def explain(args):
        seen.append(("explain", args.source))

    from castomatic import doctor

    monkeypatch.setattr(doctor, "run", doctor_run)
    monkeypatch.setattr(cli, "explain", explain)
    assert cli.main(["doctor", "--save", "r.txt"]) == 1
    assert cli.main(["explain", "movie.mkv"]) == 0
    assert seen == [("doctor", "r.txt"), ("explain", "movie.mkv")]


def test_help_on_a_terminal_shows_the_starburst(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "cast anything, anywhere" in out and "castomatic doctor" in out


def test_banner_only_on_terminals(monkeypatch):
    piped = io.StringIO()
    assert banner.show(piped) is False and piped.getvalue() == ""
    assert banner.show(piped, force=True) is True and "castomatic" in piped.getvalue()
    tty = io.StringIO()
    tty.isatty = lambda: True
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert banner.use_color(tty)
    colored = banner.render(True)
    assert "\033[33m" in colored and "cast anything" in colored
    monkeypatch.setenv("NO_COLOR", "1")
    assert not banner.use_color(tty)
    out = io.StringIO()
    banner.welcome(out)
    assert "Welcome!" in out.getvalue() and "castomatic doctor" in out.getvalue()


async def test_first_run_shows_the_welcome(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def stop():
        raise UserError("stop here")

    monkeypatch.setattr(cli.stream, "ensure_ffmpeg", stop)
    with pytest.raises(UserError, match="stop here"):
        await cli.run(parse("movie.mkv"))
    assert "Welcome!" in capsys.readouterr().out
    config.save_tv({"name": "TV", "host": "10.0.0.2"})
    with pytest.raises(UserError):
        await cli.run(parse("movie.mkv"))
    assert "Welcome!" not in capsys.readouterr().out
    config.forget_tv()


# -- --burn-subtitles ------------------------------------------------------------------------------


def test_burn_command_escapes_and_re_encodes():
    s = summary(audio="aac")
    plan = stream.make_plan([Input("/m/a.mkv")], s)
    plan.burn = Path("/t/it's:x[1].srt")
    cmd = stream.stream_command(plan)
    assert cmd[cmd.index("-vf") + 1] == "subtitles=filename=/t/it\\\\\\'s\\\\:x\\[1\\].srt"
    assert "copy" != cmd[cmd.index("-c:v") + 1]
    assert "subtitles drawn on" in plan.description


def test_can_burn_subtitles_reads_the_filter_list(monkeypatch):
    import subprocess

    stream.can_burn_subtitles.cache_clear()
    listing = " ... subtitles         V->V       Render text subtitles onto input video using the libass library.\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=listing))
    assert stream.can_burn_subtitles()
    stream.can_burn_subtitles.cache_clear()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=" T.. scale  V->V\n\n"))
    assert not stream.can_burn_subtitles()
    stream.can_burn_subtitles.cache_clear()

    def broken(*a, **k):
        raise OSError

    monkeypatch.setattr(subprocess, "run", broken)
    assert not stream.can_burn_subtitles()
    stream.can_burn_subtitles.cache_clear()


class FakeSub:
    sidecar = None

    def __init__(self, fail=False):
        self.fail = fail

    async def srt(self, offset=0.0):
        if self.fail:
            raise RuntimeError("bad file")
        return b"1\n00:00:01,000 --> 00:00:02,000\nHi\n"


async def test_burn_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(stream, "can_burn_subtitles", lambda: True)
    assert await cli._burn_file(FakeSub(), 0, None) is None
    path = await cli._burn_file(FakeSub(), 5, tmp_path)
    assert path == tmp_path / "burn.srt" and b"Hi" in path.read_bytes()
    assert await cli._burn_file(FakeSub(fail=True), 0, tmp_path) is None
    assert "could not convert them" in capsys.readouterr().out
    monkeypatch.setattr(stream, "can_burn_subtitles", lambda: False)
    assert await cli._burn_file(FakeSub(), 0, tmp_path) is None
    assert "built without libass" in capsys.readouterr().out


async def test_burning_turns_a_roku_file_into_a_live_conversion(caps_player, monkeypatch, tmp_path, capsys):
    caps_player.caps = roku.CAPABILITIES
    monkeypatch.setattr(cli, "LiveHls", FakeLive)
    monkeypatch.setattr(stream, "can_burn_subtitles", lambda: True)

    async def subtitle(args, source, buffered):
        return FakeSub()

    monkeypatch.setattr(cli, "_subtitle_for", subtitle)
    src = MediaSource("Movie", [Input("/m/a.mp4")], Path("/m/a.mp4"))
    await cli._play_renderer(parse("/m/a.mp4", "--burn-subtitles"), src, summary(), Target(every_way_tv(), "roku"), tmp_path, None, None, caps_player(None), seeks=False)
    assert caps_player.last.played["url"].endswith("/index.m3u8")
    out = capsys.readouterr().out
    assert "drawing the subtitles onto the picture" in out and "subtitles drawn on" in out
    assert "takes no separate subtitle file" not in out


async def test_burning_over_dlna_uses_the_mpegts_stream(caps_player, monkeypatch, tmp_path, capsys):
    caps_player.caps = planner.dlna_capabilities({"video/mp4", "video/vnd.dlna.mpeg-tts"}, False)
    monkeypatch.setattr(stream, "can_burn_subtitles", lambda: True)

    async def subtitle(args, source, buffered):
        return FakeSub()

    monkeypatch.setattr(cli, "_subtitle_for", subtitle)
    src = MediaSource("Movie", [Input("/m/a.mp4")], Path("/m/a.mp4"))
    await cli._play_renderer(parse("/m/a.mp4", "--burn-subtitles"), src, summary(), Target(every_way_tv(), "dlna"), tmp_path, None, None, caps_player(None))
    assert caps_player.last.played["url"].endswith("/stream.ts")
    assert "subtitles drawn on" in capsys.readouterr().out
