"""castomatic doctor, discovery across every protocol, and the buffering additions."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

from castomatic import buffering, config, dlna, doctor, sources, targets, ytstream
from castomatic.backends import chromecast, roku
from castomatic.errors import UserError
from castomatic.sources import Input, MediaSource
from castomatic.targets import TV, AirPlayInfo, Target


@pytest.fixture(autouse=True)
def one_address(monkeypatch):
    monkeypatch.setattr(dlna, "local_addresses", lambda: ["127.0.0.1"])


# -- doctor ----------------------------------------------------------------------------------


def test_report_counts_and_prints(capsys):
    report = doctor.Report()
    report.section("Things")
    report.add(doctor.OK, "fine")
    report.add(doctor.WARN, "hmm", "detail", "first step\nsecond step")
    report.add(doctor.FAIL, "broken")
    out = capsys.readouterr().out
    assert "[  ok  ] fine" in out and "[ warn ] hmm: detail" in out and "fix: first step" in out
    assert "              second step" in out and "[ FAIL ] broken" in out
    assert (report.problems, report.warnings) == (1, 1)


def test_check_tools_reports_missing_ffmpeg(monkeypatch, capsys):
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: None)
    report = doctor.Report()
    doctor.check_tools(report)
    assert report.problems == 2 and "brew install ffmpeg" in capsys.readouterr().out


def test_check_tools_with_ffmpeg(monkeypatch, capsys):
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: f"/bin/{tool}")
    monkeypatch.setattr(doctor, "_first_line", lambda cmd: f"{cmd[0]} version 9")
    monkeypatch.setattr(doctor.stream, "video_encoder", lambda: "libx264")
    report = doctor.Report()
    doctor.check_tools(report)
    out = capsys.readouterr().out
    assert report.problems == 0 and "libx264 (software)" in out and "catt" in out


def test_helpers():
    assert doctor._package_version("definitely-not-a-package") == "missing"
    assert doctor._first_line(["/nonexistent/tool"]) is None
    assert doctor._first_line(["true"]) is None  # nothing printed


async def test_check_castor_both_ways(capsys):
    report = doctor.Report()
    await doctor.check_castor(report, version=lambda: _value("castor 2.5.0"))
    await doctor.check_castor(report, version=lambda: _value(None))
    out = capsys.readouterr().out
    assert "castor 2.5.0" in out and "not installed" in out and report.problems == 0


async def test_check_network(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "_firewall_state", lambda: "Firewall is enabled. (State = 1)")
    report = doctor.Report()
    assert await doctor.check_network(report) == ["127.0.0.1"]
    out = capsys.readouterr().out
    assert "serving video from 127.0.0.1: works" in out and "macOS firewall: on" in out
    monkeypatch.setattr(dlna, "local_addresses", lambda: ["127.0.0.1", "100.64.0.1"])
    monkeypatch.setattr(doctor, "_serves_on", lambda address: _value("TimeoutError"))
    monkeypatch.setattr(doctor, "_firewall_state", lambda: "Firewall is disabled. (State = 0)")
    report = doctor.Report()
    await doctor.check_network(report)
    out = capsys.readouterr().out
    assert "several network interfaces" in out and "could not fetch from itself" in out
    assert "macOS firewall: off" in out
    monkeypatch.setattr(dlna, "local_addresses", lambda: [])
    report = doctor.Report()
    assert await doctor.check_network(report) == [] and report.problems == 1


async def test_serves_on_an_address_that_cannot_be_used():
    assert await doctor._serves_on("203.0.113.1")  # not this machine's: binding fails


def test_firewall_state_off_a_mac(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    assert doctor._firewall_state() is None


async def test_check_tvs(monkeypatch, capsys):
    paired = AirPlayInfo("AA", "10.0.0.5", 7000, object())
    lonely = AirPlayInfo("BB", "10.0.0.6", 7000, None)
    tvs = [
        TV("Den TV", "10.0.0.5", chromecast=chromecast.ChromecastInfo("Den TV", "10.0.0.5"), airplay=AirPlayInfo("CC", "10.0.0.5", 7000, None)),
        TV("Apple TV", "10.0.0.6", airplay=lonely),
        TV("Bedroom", "10.0.0.7", roku=roku.RokuInfo("Bedroom", "10.0.0.7")),
    ]
    assert paired.credentials

    async def discover(credentials=None):
        targets.LAST_SCAN[:] = [targets.ScanResult("dlna", 0, 3.0), targets.ScanResult("roku", 1, 3.0, "OSError: x")]
        return tvs

    monkeypatch.setattr(targets, "discover_tvs", discover)
    monkeypatch.setattr(doctor, "_roku_access", lambda host: _value("HTTP 403"))
    config.save_tv({"name": "Den TV", "host": "10.0.0.5"})
    report = doctor.Report()
    await doctor.check_tvs(report)
    out = capsys.readouterr().out
    assert "DLNA search: 0 found in 3.0s" in out and "Roku search: 1 found in 3.0s; OSError: x" in out
    assert "<- your TV" in out
    assert "not paired (not needed" in out  # Den TV is reached over Chromecast anyway
    assert "atvremote --id BB" in out  # Apple TV has nothing else
    assert "Control by mobile apps" in out


async def test_check_tvs_none_found(monkeypatch, capsys):
    async def discover(credentials=None):
        targets.LAST_SCAN.clear()
        return []

    monkeypatch.setattr(targets, "discover_tvs", discover)
    report = doctor.Report()
    await doctor.check_tvs(report)
    out = capsys.readouterr().out
    assert report.problems == 1 and "Is the TV on" in out and "castomatic doctor" not in out


async def test_roku_access_unreachable():
    assert await doctor._roku_access("127.0.0.1")  # nothing listening on 8060 here


async def test_doctor_run_saves_the_report(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(doctor, "check_tools", lambda report: report.add(doctor.OK, "tools"))
    monkeypatch.setattr(doctor, "check_castor", lambda report: _value(None))
    monkeypatch.setattr(doctor, "check_network", lambda report: _value([]))
    outcome = {"status": doctor.OK}

    async def tvs(report, credentials=None):
        report.add(outcome["status"], "tvs")
        return []

    monkeypatch.setattr(doctor, "check_tvs", tvs)
    saved = tmp_path / "report.txt"
    args = doctor.build_parser().parse_args(["--save", str(saved)])
    assert await doctor.run(args) == 0
    assert "Everything looks good" in saved.read_text() and "Report saved" in capsys.readouterr().out
    outcome["status"] = doctor.WARN
    assert await doctor.run(doctor.build_parser().parse_args([])) == 0
    assert "warning(s) worth reading" in capsys.readouterr().out
    outcome["status"] = doctor.FAIL
    assert await doctor.run(doctor.build_parser().parse_args([])) == 1
    assert "problem(s) to fix" in capsys.readouterr().out


async def _value(v):
    return v


# -- discovery across every protocol ---------------------------------------------------------------


async def test_discover_merges_every_protocol_and_records_each_search(monkeypatch):
    async def renderers(timeout):
        return [dlna.Renderer("http://10.0.0.5:1/d", "Den TV", "u", "http://10.0.0.5:1/c")]

    async def airplay(credentials):
        raise_later = AirPlayInfo("AA", "10.0.0.7", 7000, object(), "Apple TV")
        return [raise_later]

    async def casts():
        return [chromecast.ChromecastInfo("Den TV (Cast)", "10.0.0.5"), chromecast.ChromecastInfo("Mine", "127.0.0.1")]

    async def rokus():
        raise OSError("no multicast")

    monkeypatch.setattr(dlna, "discover_renderers", renderers)
    monkeypatch.setattr(targets, "_scan_airplay", airplay)
    monkeypatch.setattr(targets, "_scan_chromecast", casts)
    monkeypatch.setattr(targets, "_scan_roku", rokus)
    found = await targets.discover_tvs()
    assert [(t.name, t.protocols) for t in found] == [("Apple TV", ["AirPlay"]), ("Den TV", ["Chromecast", "DLNA"])]
    by_protocol = {s.protocol: s for s in targets.LAST_SCAN}
    assert by_protocol["roku"].error == "OSError: no multicast" and by_protocol["chromecast"].found == 2


async def test_a_search_that_hangs_is_cut_off():
    targets.LAST_SCAN.clear()
    assert await targets._timed("roku", asyncio.sleep(5), 0.05) == []
    assert "no answer within" in targets.LAST_SCAN[0].error


async def test_scanners_call_the_backends(monkeypatch):
    async def found(timeout):
        return ["x"]

    monkeypatch.setattr(chromecast, "discover", found)
    monkeypatch.setattr(roku, "discover", found)
    assert await targets._scan_chromecast() == ["x"] and await targets._scan_roku() == ["x"]


def test_matches_cast_uuid_and_roku_serial():
    tv = TV("Den", "10.0.0.5", chromecast=chromecast.ChromecastInfo("Den", "10.0.0.5", uuid="ABC-1"), roku=roku.RokuInfo("Den", "10.0.0.5", serial="X9"))
    assert targets.matches(tv, "abc-1") and targets.matches(tv, "x9") and not targets.matches(tv, "zz")


def test_candidate_targets_order_and_unpaired_airplay():
    tv = TV("Den", "10.0.0.5", dlna.Renderer("http://10.0.0.5/d", "Den", "u", "c"),
            AirPlayInfo("AA", "10.0.0.5", 7000, None), roku=roku.RokuInfo("Den", "10.0.0.5"))  # fmt: skip
    assert [t.protocol for t in targets.candidate_targets(tv)] == ["dlna", "roku"]
    assert [t.protocol for t in targets.candidate_targets(tv, "roku")] == ["roku"]
    with pytest.raises(UserError, match="does not offer Chromecast"):
        targets.candidate_targets(tv, "chromecast")
    only_unpaired = TV("ATV", "10.0.0.6", airplay=AirPlayInfo("BB", "10.0.0.6", 7000, None))
    with pytest.raises(UserError, match="not paired") as caught:
        targets.candidate_targets(only_unpaired)
    assert "Nothing else answered" in caught.value.hint


async def test_saved_tv_reached_over_chromecast_and_roku_without_searching(monkeypatch):
    async def no_search(credentials=None):
        raise AssertionError("should not search")

    monkeypatch.setattr(targets, "discover_tvs", no_search)
    saved = {"name": "Den", "host": "10.0.0.5", "chromecast": True, "roku": True}
    found = await targets._saved_fast_targets(saved, "auto")
    assert [t.protocol for t in found] == ["chromecast", "roku"]
    assert await targets._saved_fast_targets(saved, "airplay") == []
    assert await targets._saved_fast_targets({"name": "x", "host": "10.0.0.5", "airplay_id": "A"}, "auto") == []
    config.save_tv(saved)
    assert (await targets.resolve_target()).protocol == "chromecast"


def test_remember_records_every_protocol(capsys):
    tv = TV("Den", "10.0.0.5", chromecast=chromecast.ChromecastInfo("Den", "10.0.0.5"))
    targets._remember(tv)
    assert config.saved_tv()["chromecast"] is True and config.saved_tv()["roku"] is False


def test_no_tv_error_mentions_local_network_on_a_mac(monkeypatch):
    monkeypatch.setattr(targets.sys, "platform", "darwin")
    assert "Local Network" in targets.no_tv_error([], None).hint
    monkeypatch.setattr(targets.sys, "platform", "linux")
    assert "Local Network" not in targets.no_tv_error([], None).hint


# -- network shares ------------------------------------------------------------------------------


def test_filesystem_type_picks_the_deepest_mount(monkeypatch):
    table = [("/", "apfs"), ("/Volumes/share", "smbfs"), ("/Volumes/share/deep", "nfs")]
    monkeypatch.setattr(buffering, "_mount_table", lambda: table)
    assert buffering.filesystem_type(Path("/Volumes/share/movie.mkv")) == "smbfs"
    assert buffering.filesystem_type(Path("/Volumes/share/deep/x")) == "nfs"
    assert buffering.filesystem_type(Path("/Users/me/x")) == "apfs"
    assert buffering.is_network_path(Path("/Volumes/share/movie.mkv"))
    assert not buffering.is_network_path(Path("/Users/me/x"))
    monkeypatch.setattr(buffering, "_mount_table", lambda: [("/mnt", "fuse.sshfs")])
    assert buffering.is_network_path(Path("/mnt/a"))
    monkeypatch.setattr(buffering, "_mount_table", lambda: [])
    assert buffering.filesystem_type(Path("/x")) is None


def test_mount_table_reads_this_machine():
    table = buffering._mount_table()
    assert any(point == "/" for point, _ in table)


def test_mount_table_parses_mount_output(monkeypatch):
    def no_proc(*args, **kwargs):
        raise OSError

    out = "//u@nas/share on /Volumes/my share (smbfs, nodev, nosuid)\nnonsense\n"
    monkeypatch.setattr("builtins.open", no_proc)
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=out))
    assert buffering._mount_table() == [("/Volumes/my share", "smbfs")]

    def broken(*a, **k):
        raise OSError("no mount")

    monkeypatch.setattr(subprocess, "run", broken)
    assert buffering._mount_table() == []


async def test_network_share_is_held_to_a_higher_standard(monkeypatch, tmp_path):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"x" * 1000)
    notes = []
    monkeypatch.setattr(buffering, "is_network_path", lambda path: True)
    # 8x faster than the video needs: enough for a local disk (6x), not for a share (12x)
    monkeypatch.setattr(buffering, "measure_rate", lambda path: _value(1000 / 10 * 8))
    media = await buffering.choose(video, 10.0, root=tmp_path / "c", on_note=notes.append)
    assert media is not None
    await media.close()
    assert "network share" in notes[0] and "will not stop part way" in notes[0]
    monkeypatch.setattr(buffering, "measure_rate", lambda path: _value(1000 / 10 * 20))  # 20x
    other = tmp_path / "other.mkv"
    other.write_bytes(b"y" * 1000)
    assert await buffering.choose(other, 10.0, root=tmp_path / "c2") is None


# -- remote addresses -----------------------------------------------------------------------------


async def _server(handler):
    app = web.Application()
    app.router.add_get("/v", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v"


async def test_measure_url_rate():
    async def slow(request):
        response = web.StreamResponse()
        await response.prepare(request)
        for _ in range(20):
            await response.write(b"x" * 10_000)
            await asyncio.sleep(0.02)
        return response

    runner, url = await _server(slow)
    try:
        rate = await sources.measure_url_rate(Input(url), seconds=0.2)
        assert 10_000 < rate < 2_000_000
        short = await sources.measure_url_rate(Input(url), seconds=10, limit=50_000)
        assert short > 0
    finally:
        await runner.cleanup()

    async def tiny(request):
        return web.Response(body=b"abc")

    runner, url = await _server(tiny)
    try:
        assert await sources.measure_url_rate(Input(url), seconds=5) > 0
    finally:
        await runner.cleanup()

    async def missing(request):
        return web.Response(status=404)

    runner, url = await _server(missing)
    try:
        assert await sources.measure_url_rate(Input(url)) is None
    finally:
        await runner.cleanup()

    async def empty(request):
        return web.Response(body=b"")

    runner, url = await _server(empty)
    try:
        assert await sources.measure_url_rate(Input(url)) is None
    finally:
        await runner.cleanup()
    assert await sources.measure_url_rate(Input("http://127.0.0.1:1/v")) is None


def test_remote_buffer_copies_with_ffmpeg(tmp_path):
    src = MediaSource("Clip", [Input("https://cdn/v.mp4", {"Referer": "https://site/"})])
    cmd = ytstream.remote_command(src)
    assert cmd[0] == "ffmpeg" and "-reconnect" in cmd and "https://cdn/v.mp4" in cmd
    assert cmd[cmd.index("-c") + 1] == "copy" and cmd[-3:] == ["-f", "mpegts", "pipe:1"]
    assert any("Referer: https://site/" in part for part in cmd)
    buffer = ytstream.RemoteBuffer(src, tmp_path)
    assert buffer.command() == cmd
    buffer.error = "Connection reset"
    assert "could not keep downloading 'Clip': Connection reset" in buffer._failure().message
    yt = ytstream.YtdlpBuffer(MediaSource("x", [Input("u")], page_url="https://y/x"), tmp_path)
    assert "yt_dlp" in " ".join(yt.command())


def test_check_tools_reports_subtitle_drawing(monkeypatch, capsys):
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: f"/bin/{tool}")
    monkeypatch.setattr(doctor, "_first_line", lambda cmd: "v")
    monkeypatch.setattr(doctor.stream, "video_encoder", lambda: "h264_videotoolbox")
    for available, words in [(True, "available"), (False, "built without libass")]:
        monkeypatch.setattr(doctor.stream, "can_burn_subtitles", lambda a=available: a)
        doctor.check_tools(doctor.Report())
        assert words in capsys.readouterr().out
