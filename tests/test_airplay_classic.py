"""Classic AirPlay video (POST /play) against a fake receiver."""

import asyncio
import plistlib

import pytest
from aiohttp import web

from castomatic import cli, planner
from castomatic.backends import airplay_classic as classic
from castomatic.backends.base import STATE_LOADING, STATE_NO_MEDIA, STATE_PAUSED, STATE_PLAYING, STATE_STOPPED
from castomatic.sources import Input, MediaSource
from castomatic.targets import TV, AirPlayInfo, Target
from tests.test_cli import parse, summary


class Receiver:
    """A classic AirPlay receiver: plays, reports, seeks, pauses and stops."""

    def __init__(self):
        self.calls = []
        self.info = {}
        self.ignore_start = True
        self.fail = set()  # paths answered with HTTP 500
        self.hang = False
        self.garbage = False
        self.runner = None
        self.port = 0

    async def start(self):
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self):
        await self.runner.cleanup()

    async def handle(self, request):
        path = request.path
        self.calls.append((request.method, path, dict(request.query), request.headers.get("X-Apple-Session-ID")))
        if path in self.fail:
            return web.Response(status=500)
        if path == "/play":
            body = plistlib.loads(await request.read())
            start = 0.0 if self.ignore_start else body["Start-Position-Seconds"]
            self.info = {"readyToPlay": True, "rate": 1.0, "position": start, "duration": 100.0}
        elif path == "/playback-info":
            if self.hang:
                await asyncio.sleep(1)
            if self.garbage:
                return web.Response(body=b"not a plist")
            return web.Response(body=plistlib.dumps(self.info, fmt=plistlib.FMT_BINARY) if self.info else b"")
        elif path == "/scrub":
            self.info["position"] = float(request.query["position"])
        elif path == "/rate":
            self.info["rate"] = float(request.query["value"])
        return web.Response()


@pytest.fixture
async def receiver(monkeypatch):
    monkeypatch.setattr(classic, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(classic, "REQUEST_TIMEOUT", 0.3)
    fake = Receiver()
    await fake.start()
    yield fake
    await fake.stop()


async def _player(receiver):
    player = classic.ClassicAirPlayPlayer("127.0.0.1", receiver.port, "Fire TV")
    await player.connect()
    return player


async def test_plays_seeks_to_the_start_and_controls_playback(receiver):
    player = await _player(receiver)
    try:
        assert player.local_ip == "127.0.0.1" and player.ignores_subtitle_links()
        player.remember_ignores_subtitle_links()  # nothing to do, nothing raised
        assert await player.capabilities() is classic.CAPABILITIES
        await player.play("http://me/media.mp4", "Movie", start=42.0)
        methods = [(m, p) for m, p, _, _ in receiver.calls]
        assert ("POST", "/play") in methods and ("POST", "/scrub") in methods
        assert receiver.info["position"] == 42.0 and await player.position() == 42.0
        assert len({sid for *_, sid in receiver.calls}) == 1  # one session throughout
        await player.pause()
        assert await player.state() == STATE_PAUSED
        await player.resume()
        assert await player.state() == STATE_PLAYING
        receiver.info.update(position=100.0, rate=0.0)  # the end
        await asyncio.wait_for(player.wait_until_ended(), 2)
        await player.stop()
        assert receiver.calls[-1][1] == "/stop"
    finally:
        await player.close()


async def test_no_seek_when_the_receiver_started_where_asked(receiver):
    receiver.ignore_start = False
    player = await _player(receiver)
    try:
        await player.play("http://me/media.mp4", start=30.0)
        assert "/scrub" not in [p for _, p, _, _ in receiver.calls]
        await player.play("http://me/media.mp4")  # a second play replaces the first poller
    finally:
        await player.close()


async def test_a_receiver_that_never_gets_ready_is_not_seeked(receiver, monkeypatch):
    monkeypatch.setattr(classic, "READY_WAIT", 0.2)
    player = await _player(receiver)

    async def unready(*args, **kwargs):
        raise classic.ClassicAirPlayError("busy")

    try:
        monkeypatch.setattr(player, "_info", unready)
        await player.play("http://me/media.mp4", start=30.0)
        assert "/scrub" not in [p for _, p, _, _ in receiver.calls]
        assert await player.position() is None
    finally:
        await player.close()


async def test_a_receiver_that_stops_answering_has_ended(receiver):
    player = await _player(receiver)
    try:
        await player.play("http://me/media.mp4")
        await asyncio.sleep(0.05)  # seen playing
        receiver.fail.add("/playback-info")
        await asyncio.wait_for(player.wait_until_ended(), 2)
    finally:
        await player.close()


async def test_refusals_and_bad_answers_are_errors(receiver):
    player = classic.ClassicAirPlayPlayer("127.0.0.1", receiver.port)
    with pytest.raises(classic.ClassicAirPlayError, match="not connected"):
        await player.state()
    await player.stop()  # not connected: nothing to stop
    await player.connect()
    try:
        receiver.fail.add("/play")
        with pytest.raises(classic.ClassicAirPlayError, match="would not play the video: HTTP 500"):
            await player.play("http://me/media.mp4")
        receiver.garbage = True
        with pytest.raises(classic.ClassicAirPlayError, match="unreadable state"):
            await player.state()
        receiver.garbage, receiver.hang = False, True
        with pytest.raises(classic.ClassicAirPlayError, match="TimeoutError"):
            await player.state()
        receiver.fail.add("/stop")
        await player.stop()  # never raises
    finally:
        await player.close()


@pytest.mark.parametrize(
    "info,state",
    [
        ({}, STATE_NO_MEDIA),
        ({"duration": 5.0}, STATE_NO_MEDIA),
        ({"readyToPlay": False}, STATE_LOADING),
        ({"readyToPlay": True, "rate": 1.0, "position": 3.0, "duration": 10.0}, STATE_PLAYING),
        ({"readyToPlay": True, "rate": 0.0, "position": 3.0, "duration": 10.0}, STATE_PAUSED),
        ({"readyToPlay": True, "rate": 0.0, "position": 9.5, "duration": 10.0}, STATE_STOPPED),
        ({"readyToPlay": True, "rate": 1.0, "position": 10.0, "duration": 10.0}, STATE_PLAYING),
    ],
)
def test_states(info, state):
    assert classic._state(info) == state


def _target(pairing_needed=False, credentials=None):
    air = AirPlayInfo("AA", "127.0.0.1", 7000, credentials, "Fire TV", pairing_needed=pairing_needed)
    return Target(TV("Fire TV", "127.0.0.1", airplay=air), "airplay")


async def test_classic_airplay_is_planned_like_a_tv_that_fetches_files():
    src = MediaSource("a", [Input("/m/a.mp4")], None)
    (ranked,) = await planner.order_targets([_target()], "auto", src, summary(), 0.0)
    assert ranked.delivery.kind != "hls" or ranked.delivery.reasons  # not the AirPlay 2 default
    (paired,) = await planner.order_targets([_target(True, object())], "auto", src, summary(), 0.0)
    assert paired.delivery.kind == "hls" and paired.delivery.reasons == []


async def test_an_airplay_tv_that_needs_no_pairing_is_played_by_url(monkeypatch):
    seen = {}

    async def renderer(args, source, summ, target, workdir, buffered, fallback, player):
        seen["player"] = player

    monkeypatch.setattr(cli, "_play_renderer", renderer)
    await cli._play_airplay(parse("x"), MediaSource("a", [Input("x")]), summary(), _target(), None)
    assert isinstance(seen["player"], classic.ClassicAirPlayPlayer) and seen["player"].name == "Fire TV"
