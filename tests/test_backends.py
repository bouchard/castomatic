"""Chromecast and Roku players and discovery, against fakes that behave like the real thing."""

import asyncio
import logging
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

from castomatic import dlna
from castomatic.backends import base, chromecast, roku
from castomatic.backends.base import Capabilities, allow_any_origin



# -- base ------------------------------------------------------------------------------------


def test_capabilities_describe_and_alias():
    caps = Capabilities(sink=frozenset({"video/mp4"}), video=frozenset({"h264"}), audio=frozenset({"aac"}), hls=True)
    assert caps.supports_hls is True
    text = caps.describe()
    assert "video/mp4" in text and "h264" in text and "HLS: yes" in text
    assert "not stated" in Capabilities().describe()


async def test_allow_any_origin_adds_the_header():
    app = web.Application()
    allow_any_origin(app)
    app.router.add_get("/", lambda request: web.Response(text="hi"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{port}/") as resp:
                assert resp.headers["Access-Control-Allow-Origin"] == "*"
    finally:
        await runner.cleanup()


def test_cast_error_keeps_its_code():
    assert base.CastError("x", 714).code == 714
    assert issubclass(dlna.DlnaError, base.CastError)


# -- Chromecast --------------------------------------------------------------------------------


class FakeMediaController:
    def __init__(self):
        self.status = None
        self.calls = []

    def pause(self):
        self.calls.append("pause")

    def play(self):
        self.calls.append("play")

    def stop(self):
        self.calls.append("stop")


class FakeCast:
    def __init__(self):
        self.media_controller = FakeMediaController()
        self.disconnected = False

    def disconnect(self):
        self.disconnected = True


class FakeController:
    def __init__(self, cast, fail=None):
        self.cast = cast
        self.fail = fail
        self.loaded = None
        self.subtitle_on = None

    def prep_app(self):
        pass

    def play_media_url(self, url, **kwargs):
        if self.fail:
            raise self.fail
        self.loaded = (url, kwargs)
        self.cast.media_controller.status = SimpleNamespace(player_state="BUFFERING", adjusted_current_time=0.0, idle_reason=None)

    def enable_subtitle(self, track):
        self.subtitle_on = track


class FakeCattDevice:
    connect_error = None
    load_error = None
    last = None

    def __init__(self, ip_addr="", lazy=False):
        self.ip_addr = ip_addr
        self._cast = FakeCast()
        self._controller = FakeController(self._cast, self.load_error)
        FakeCattDevice.last = self

    @property
    def controller(self):
        if self.connect_error:
            raise self.connect_error
        return self._controller


@pytest.fixture
def cast_player(monkeypatch):
    monkeypatch.setattr(chromecast, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(FakeCattDevice, "connect_error", None)
    monkeypatch.setattr(FakeCattDevice, "load_error", None)
    info = chromecast.ChromecastInfo("Den TV", "127.0.0.1")
    return chromecast.ChromecastPlayer(info, device_factory=FakeCattDevice)


def status(state, at=0.0, reason=None):
    return SimpleNamespace(player_state=state, adjusted_current_time=at, idle_reason=reason)


async def test_chromecast_loads_media_and_reports_progress(cast_player):
    await cast_player.connect()
    assert cast_player.local_ip == "127.0.0.1"
    assert await cast_player.capabilities() is chromecast.CAPABILITIES
    assert await cast_player.state() == base.STATE_NO_MEDIA  # nothing loaded yet
    assert await cast_player.position() is None
    cast_player.mime = "application/vnd.apple.mpegurl"
    await cast_player.play("http://127.0.0.1:1/index.m3u8", "Movie", 12.0, "http://127.0.0.1:1/subs.vtt")
    device = FakeCattDevice.last
    url, kwargs = device._controller.loaded
    assert url.endswith("index.m3u8") and kwargs["content_type"] == "application/vnd.apple.mpegurl"
    assert kwargs["current_time"] == 12.0 and kwargs["title"] == "Movie"
    assert kwargs["subtitles"].endswith("subs.vtt") and device._controller.subtitle_on == 1
    assert await cast_player.state() == base.STATE_LOADING
    device._cast.media_controller.status = status("PLAYING", 13.5)
    assert await cast_player.state() == base.STATE_PLAYING
    assert await cast_player.position() == 13.5
    await cast_player.pause()
    await cast_player.resume()
    device._cast.media_controller.status = status("IDLE", 99.0, "FINISHED")
    await asyncio.wait_for(cast_player.wait_until_ended(), 1)
    assert cast_player.error is None
    await cast_player.stop()
    assert device._cast.media_controller.calls == ["pause", "play", "stop"]
    await cast_player.close()
    assert device._cast.disconnected
    await cast_player.stop()  # after close: nothing to do


async def test_chromecast_error_ends_playback_with_a_reason(cast_player):
    await cast_player.connect()
    await cast_player.play("http://x/a.mp4")
    FakeCattDevice.last._cast.media_controller.status = status("IDLE", 0.0, "ERROR")
    await asyncio.wait_for(cast_player.wait_until_ended(), 1)
    assert "error" in cast_player.error
    await cast_player.close()


async def test_chromecast_says_why_playback_ended(cast_player, caplog):
    # A receiver that drops the session mid-way used to end playback without a word
    caplog.set_level(logging.DEBUG, logger="castomatic.backends.chromecast")
    await cast_player.connect()
    await cast_player.play("http://x/a.mp4")
    controller = FakeCattDevice.last._cast.media_controller
    controller.status = status("PLAYING", 60.0)
    await asyncio.sleep(0.05)
    controller.status = status("IDLE", 67.0, "INTERRUPTED")
    await asyncio.wait_for(cast_player.wait_until_ended(), 1)
    assert "idle reason INTERRUPTED" in caplog.text and cast_player.error is None
    await cast_player.close()


async def test_chromecast_failures_become_cast_errors(cast_player, monkeypatch):
    FakeCattDevice.connect_error = RuntimeError("no answer")
    with pytest.raises(chromecast.ChromecastError, match="could not connect to Den TV"):
        await cast_player.connect()
    FakeCattDevice.connect_error = None
    FakeCattDevice.load_error = RuntimeError("LOAD_FAILED")
    await cast_player.connect()
    with pytest.raises(chromecast.ChromecastError, match="would not load"):
        await cast_player.play("http://x/a.mp4")

    def broken():
        raise RuntimeError("socket closed")

    FakeCattDevice.last._cast.media_controller.pause = broken
    with pytest.raises(chromecast.ChromecastError, match="pause failed"):
        await cast_player.pause()
    FakeCattDevice.last._cast.media_controller.stop = broken
    await cast_player.stop()  # a failed stop is not an error


async def test_chromecast_media_call_needs_a_connection(cast_player):
    with pytest.raises(chromecast.ChromecastError, match="not connected"):
        await cast_player.pause()


async def test_chromecast_subtitle_switch_failure_is_not_fatal(cast_player):
    await cast_player.connect()

    def nope(track):
        raise RuntimeError("no tracks")

    FakeCattDevice.last._controller.enable_subtitle = nope
    await cast_player.play("http://x/a.mp4", subtitle_url="http://x/s.vtt")
    await cast_player.close()


async def test_chromecast_close_survives_a_failed_disconnect(cast_player):
    await cast_player.connect()

    def boom():
        raise RuntimeError("gone")

    FakeCattDevice.last._cast.disconnect = boom
    await cast_player.close()


async def test_chromecast_that_never_starts_stops_being_waited_on(cast_player, monkeypatch):
    monkeypatch.setattr(chromecast, "PLAY_WAIT", 0.002)
    await cast_player.connect()
    await cast_player.play("http://x/a.mp4")
    FakeCattDevice.last._cast.media_controller.status = None
    await asyncio.wait_for(cast_player.wait_until_ended(), 1)
    await cast_player.close()


async def test_chromecast_discovery_keeps_only_devices_with_screens(monkeypatch, real_discover):
    import pychromecast

    stopped = []
    infos = [
        SimpleNamespace(friendly_name="Den TV", host="10.0.0.8", port=8009, uuid="u-1", model_name="Some TV", cast_type="cast"),
        SimpleNamespace(friendly_name="Kitchen speaker", host="10.0.0.9", port=8009, uuid="u-2", model_name="Speaker", cast_type="audio"),
        SimpleNamespace(friendly_name="", host="10.0.0.10", port=None, uuid=None, model_name=None, cast_type=None),
    ]
    browser = SimpleNamespace(stop_discovery=lambda: stopped.append(True))
    monkeypatch.setattr(pychromecast.discovery, "discover_chromecasts", lambda timeout: (infos, browser))
    found = chromecast._discover_sync(1.0)
    assert [c.name for c in found] == ["Den TV", "Kitchen speaker", "10.0.0.10"] and stopped
    assert found[2].port == chromecast.PORT and found[2].cast_type == "cast"
    monkeypatch.setattr(chromecast, "_discover_sync", lambda timeout: found)
    assert [c.name for c in await real_discover["chromecast"](0.1)] == ["Den TV", "10.0.0.10"]


# -- Roku ------------------------------------------------------------------------------------


class FakeEcp:
    """An aiohttp server speaking enough ECP, which fetches what it is told to play."""

    def __init__(self):
        self.inputs = []
        self.keys = []
        self.state = "none"
        self.error = "false"
        self.position = "0 ms"
        self.fetched = 0
        self.forbidden = False
        self.fetch = True

    async def input(self, request):
        if self.forbidden:
            return web.Response(status=403, text="Forbidden")
        self.inputs.append(dict(request.query))
        self.state = "buffer"
        if self.fetch:
            asyncio.ensure_future(self._fetch(request.query["u"]))
        return web.Response(text="")

    async def _fetch(self, url):
        async with aiohttp.ClientSession() as http:
            async with http.get(url) as resp:
                while await resp.content.read(65536):
                    self.fetched += 1
        self.state = "play"
        self.position = "1500 ms"
        await asyncio.sleep(0.05)
        self.state = "stop"

    async def keypress(self, request):
        self.keys.append(request.match_info["key"])
        if request.match_info["key"] == "Back":
            self.state = "close"
        return web.Response(text="")

    async def media_player(self, request):
        xml = (
            f'<?xml version="1.0"?><player error="{self.error}" state="{self.state}">'
            f"<position>{self.position}</position></player>"
        )
        return web.Response(text=xml, content_type="text/xml")

    async def device_info(self, request):
        xml = (
            '<?xml version="1.0"?><device-info><user-device-name>Bedroom Roku</user-device-name>'
            "<serial-number>X123</serial-number><model-name>Streamer</model-name></device-info>"
        )
        return web.Response(text=xml, content_type="text/xml")

    async def start(self):
        app = web.Application()
        app.router.add_post("/input/15985", self.input)
        app.router.add_post("/keypress/{key}", self.keypress)
        app.router.add_get("/query/media-player", self.media_player)
        app.router.add_get("/query/device-info", self.device_info)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self.port


@pytest.fixture
async def ecp(monkeypatch):
    monkeypatch.setattr(roku, "POLL_INTERVAL", 0.01)
    fake = FakeEcp()
    await fake.start()
    yield fake
    await fake.runner.cleanup()


async def _media_server():
    async def video(request):
        return web.Response(body=b"x" * 200_000, content_type="video/mp4")

    app = web.Application()
    app.router.add_get("/media.mp4", video)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/media.mp4"


async def test_roku_plays_through_the_media_player_channel(ecp):
    runner, url = await _media_server()
    player = roku.RokuPlayer(roku.RokuInfo("Bedroom", "127.0.0.1", ecp.port))
    try:
        await player.connect()
        assert await player.capabilities() is roku.CAPABILITIES
        assert await player.state() == base.STATE_NO_MEDIA
        player.mime = "video/x-mkv"
        await player.play(url, "Movie")
        assert ecp.inputs == [{"t": "v", "u": url, "videoName": "Movie", "videoFormat": "mkv"}]
        await asyncio.wait_for(player.wait_until_ended(), 3)
        assert ecp.fetched > 0  # the Roku really fetched the video from our server
        assert await player.position() == 1.5
    finally:
        await player.stop()  # already stopped: no key pressed
        await player.close()
        await runner.cleanup()
    assert ecp.keys == []


async def test_roku_pause_resume_and_stop_use_the_remote(ecp):
    player = roku.RokuPlayer(roku.RokuInfo("Bedroom", "127.0.0.1", ecp.port))
    await player.connect()
    ecp.state = "play"
    await player.pause()
    ecp.state = "pause"
    await player.resume()
    await player.pause()  # paused already: no key
    ecp.state = "play"
    await player.resume()  # playing already: no key
    await player.stop()
    assert ecp.keys == ["Play", "Play", "Back"]
    player.mime = "application/x-unknown"
    ecp.fetch = False
    await player.play("http://127.0.0.1:1/x")
    assert "videoFormat" not in ecp.inputs[-1] and ecp.inputs[-1]["videoName"] == "Castomatic"
    await player.close()
    await player.stop()  # closed: nothing to do


async def test_roku_cannot_start_part_way(ecp):
    player = roku.RokuPlayer(roku.RokuInfo("Bedroom", "127.0.0.1", ecp.port))
    await player.connect()
    with pytest.raises(roku.RokuError, match="cannot start part way"):
        await player.play("http://x/a.mp4", start=30)
    await player.close()


async def test_roku_refusal_explains_the_mobile_apps_setting(ecp):
    ecp.forbidden = True
    player = roku.RokuPlayer(roku.RokuInfo("Bedroom", "127.0.0.1", ecp.port))
    await player.connect()
    with pytest.raises(roku.RokuError, match="Control by mobile apps"):
        await player.play("http://x/a.mp4")
    await player.close()


async def test_roku_player_error_ends_playback(ecp):
    player = roku.RokuPlayer(roku.RokuInfo("Bedroom", "127.0.0.1", ecp.port))
    await player.connect()
    ecp.fetch = False
    await player.play("http://x/a.mp4")
    ecp.error = "true"
    await asyncio.wait_for(player.wait_until_ended(), 1)
    assert "error" in player.error
    await player.close()


async def test_roku_that_stops_answering_counts_as_ended(ecp, monkeypatch):
    monkeypatch.setattr(roku, "POLL_FAILURES", 2)
    player = roku.RokuPlayer(roku.RokuInfo("Bedroom", "127.0.0.1", ecp.port))
    await player.connect()
    ecp.fetch = False
    await player.play("http://x/a.mp4")
    await ecp.runner.cleanup()  # the Roku goes away
    await asyncio.wait_for(player.wait_until_ended(), 3)
    assert await player.position() is None
    await player.stop()  # cannot reach it: not an error
    await player.close()
    await ecp.start()  # for the fixture's cleanup


async def test_roku_discovery_and_naming(ecp, monkeypatch, real_discover):
    async def replies(timeout, target, st):
        assert st == "roku:ecp"
        return [
            {"location": f"http://127.0.0.1:{ecp.port}/"},
            {"location": f"http://127.0.0.1:{ecp.port}/"},  # answered on two interfaces
            {"location": "http:///"},  # no host: ignored
        ]

    monkeypatch.setattr(dlna, "ssdp_replies", replies)
    real = roku
    found = await real_discover["roku"](0.1)
    assert [(r.name, r.serial, r.model, r.port) for r in found] == [("Bedroom Roku", "X123", "Streamer", ecp.port)]
    unnamed = await real._describe("127.0.0.1", ecp.port, http_get=lambda client: _ok({}))
    assert unnamed.name == "Roku 127.0.0.1"
    dead = await real._describe("127.0.0.1", 1)
    assert dead.name == "Roku 127.0.0.1" and dead.serial == ""


async def _ok(value):
    return value


def test_ms_parsing():
    assert roku._ms("2500 ms") == 2.5
    assert roku._ms(None) is None and roku._ms("") is None


# -- Chromecast: does it show the picture? ------------------------------------------------------


class ListeningCast(FakeCast):
    """A cast that takes extra namespace handlers, as pychromecast's does."""

    def __init__(self):
        super().__init__()
        self.handlers = []
        self.media_controller.update_status = lambda: self.media_controller.calls.append("update")

    def register_handler(self, handler):
        self.handlers.append(handler)


class ListeningDevice(FakeCattDevice):
    def __init__(self, ip_addr="", lazy=False):
        super().__init__(ip_addr, lazy)
        self._cast = ListeningCast()
        self._controller = FakeController(self._cast)


@pytest.fixture
def listening_player(monkeypatch):
    monkeypatch.setattr(chromecast, "PICTURE_WAIT", 0.2)
    info = chromecast.ChromecastInfo("Den TV", "127.0.0.1", uuid="abc")
    return chromecast.ChromecastPlayer(info, device_factory=ListeningDevice)


def media_status(video_info=None):
    entry = {"playerState": "PLAYING"}
    if video_info:
        entry["videoInfo"] = video_info
    return {"type": "MEDIA_STATUS", "status": [entry]}


async def test_chromecast_tries_hevc_until_a_device_does_not_show_it(listening_player):
    assert "hevc" in (await listening_player.capabilities()).video
    listening_player.remember_cannot_show("mpeg4")  # never tried as it is: nothing to remember
    assert await listening_player.capabilities() == chromecast.CAPABILITIES
    listening_player.remember_cannot_show("hevc")
    caps = await listening_player.capabilities()
    assert "hevc" not in caps.video and "hevc" not in caps.live_video and "h264" in caps.video
    other = chromecast.ChromecastPlayer(chromecast.ChromecastInfo("Other", "10.0.0.9"))
    assert other.device_id == "chromecast:10.0.0.9"  # no uuid: remembered by address
    assert "hevc" in (await other.capabilities()).video


async def test_chromecast_sees_the_picture_size_it_reports(listening_player):
    await listening_player.connect()
    (listener,) = listening_player._device._cast.handlers
    assert listener.namespace == chromecast.MEDIA_NAMESPACE
    assert listener.receive_message(None, {"type": "RECEIVER_STATUS"}) is False
    listener.receive_message(None, media_status())
    assert listener.video_info is None
    listener.receive_message(None, media_status({"width": 1920, "height": 1080}))
    assert await listening_player.shows_picture() is True
    await listening_player.play("http://x/v.m3u8")  # a new video: nothing known about it yet
    assert listener.video_info is None


async def test_chromecast_playing_sound_without_a_picture(listening_player):
    await listening_player.connect()
    assert await listening_player.shows_picture() is False
    assert "update" in listening_player._device._cast.media_controller.calls  # it asked once
    listening_player._picture.video_info = {"width": 0}
    assert await listening_player.shows_picture() is False


async def test_chromecast_that_cannot_say_whether_it_shows_the_picture(cast_player):
    await cast_player.connect()  # this cast takes no extra handlers
    assert cast_player._picture is None
    assert await cast_player.shows_picture() is None
