"""Session flow against an in-process mock receiver (loopback, no encryption)."""

import asyncio
import plistlib

from unittest.mock import Mock

import pytest
from pyatv import exceptions
from pyatv.support.http import HttpRequest, format_request

from castomatic import airplay
from castomatic.airplay import AirPlayError, AirPlayPlayer


async def _read_message(reader: asyncio.StreamReader):
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
    body = await reader.readexactly(int(headers.get("Content-Length", 0)))
    return lines[0], headers, body


def _plist(body):
    return plistlib.loads(body) if body else None


def _command(body):
    return plistlib.loads(plistlib.loads(body)["params"]["data"])


class MockReceiver:
    """Speaks the receiver side of the captured Airflow flow."""

    def __init__(self, stream_code=200, event_port=True):
        self.requests = []
        self.stream_code = stream_code
        self.event_port = event_port

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        try:
            while True:
                line, headers, body = await _read_message(reader)
                method, path, _ = line.split(" ", 2)
                self.requests.append((method, path, headers, body))
                code, reply = 200, b""
                if method == "SETUP" and b"streams" in body:
                    if self.stream_code == 200:
                        reply = plistlib.dumps(
                            {"streams": [{"type": 130, "streamID": 1}]},
                            fmt=plistlib.FMT_BINARY,
                        )
                    else:
                        code = self.stream_code
                elif method == "SETUP":
                    reply = plistlib.dumps(
                        {"eventPort": 1, "timingPort": 0} if self.event_port else {}
                    )
                writer.write(
                    f"RTSP/1.0 {code} OK\r\nCSeq: {headers['CSeq']}\r\n"
                    f"Content-Type: application/x-apple-binary-plist\r\n"
                    f"Content-Length: {len(reply)}\r\n\r\n".encode()
                    + reply
                )
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass


async def _no_verify(connection):
    return None


@pytest.fixture
async def receiver():
    mock = MockReceiver()
    port = await mock.start()
    yield mock, port
    mock.server.close()


async def test_full_flow(receiver, monkeypatch):
    mock, port = receiver
    monkeypatch.setattr(airplay, "KEEPALIVE_INTERVAL", 0.05)
    events = []

    player = AirPlayPlayer("127.0.0.1", port, secure=_no_verify, on_event=events.append)
    await player.connect()
    await player.play("http://10.0.0.2:9000/index.m3u8", title="Movie", start=90.0)
    await player.pause()
    await asyncio.sleep(0.2)  # let a few keepalives through
    await player.stop()
    await player.close()

    methods = [(m, p.split("/")[-1] if m != "POST" else p) for m, p, *_ in mock.requests]
    assert [m for m, _ in methods[:5]] == ["SETUP", "RECORD", "GET", "SETUP", "POST"]
    assert ("GET", "info") in methods

    session_setup = _plist(mock.requests[0][3])
    assert session_setup["timingProtocol"] == "PTP"
    stream_setup = _plist(mock.requests[3][3])["streams"][0]
    assert stream_setup["type"] == 130 and stream_setup["controlType"] == 1

    commands = [(h, _command(b)) for m, p, h, b in mock.requests if p == "/command"]
    assert all(h["X-Apple-StreamID"] == "1" for h, _ in commands)
    insert, rate_on, rate_off, remove = (c for _, c in commands)
    assert insert["type"] == "insertPlayQueueItem"
    assert insert["item"]["Content-Location"] == "http://10.0.0.2:9000/index.m3u8"
    assert insert["item"]["Start-Position"]["value"] == 90 * airplay.CM_TIMESCALE
    assert insert["metadata"]["Title"] == "Movie"
    assert (rate_on["type"], rate_on["rate"]) == ("setRate", 1.0)
    assert (rate_off["type"], rate_off["rate"]) == ("setRate", 0.0)
    assert remove == {"type": "removePlayQueueItem", "item": {"uuid": insert["item"]["uuid"]}}

    teardown = next(r for r in mock.requests if r[0] == "TEARDOWN")
    assert _command(teardown[3]) == {"streamID": "1", "type": 130}
    assert any(p == "/feedback" for _, p, *_ in mock.requests)


async def test_item_played_to_end_event(receiver):
    mock, port = receiver
    player = AirPlayPlayer("127.0.0.1", port, secure=_no_verify)
    await player.connect()
    assert not player.ended.is_set()
    player._event({"params": {"type": "itemPlayedToEnd"}})
    await asyncio.wait_for(player.wait_until_ended(), 1)
    await player.close()


async def test_stream_setup_refused():
    mock = MockReceiver(stream_code=400)
    port = await mock.start()
    player = AirPlayPlayer("127.0.0.1", port, secure=_no_verify)
    with pytest.raises(AirPlayError, match="400"):
        await player.connect()
    await player.close()
    mock.server.close()


def test_decode_unwraps_nested_plists():
    inner = plistlib.dumps({"name": "playbackState"}, fmt=plistlib.FMT_BINARY)
    event = {"params": {"data": inner, "list": [inner]}, "bad": b"bplist00garbage"}
    decoded = airplay._decode(event)
    assert decoded["params"]["data"] == {"name": "playbackState"}
    assert decoded["params"]["list"] == [{"name": "playbackState"}]
    assert decoded["bad"] == b"bplist00garbage"


def _event_request(body: bytes) -> bytes:
    request = HttpRequest(
        "POST", "/command", "RTSP", "1.0",
        {"CSeq": "7", "Content-Length": str(len(body)), "Server": "AirTunes"}, body,
    )  # fmt: skip
    return format_request(request)


def _channel(callback):
    channel = airplay._make_event_channel(callback)(bytes(32), bytes(32))
    channel.transport = Mock()
    channel.send = Mock()
    return channel


def test_event_channel_decodes_events_and_acknowledges():
    seen = []
    channel = _channel(seen.append)
    inner = plistlib.dumps({"name": "x"}, fmt=plistlib.FMT_BINARY)
    good = plistlib.dumps({"params": {"data": inner}}, fmt=plistlib.FMT_BINARY)
    channel.buffer = _event_request(good) + _event_request(b"not a plist")
    channel.handle_received()
    assert seen == [{"params": {"data": {"name": "x"}}}]
    assert channel.send.call_count == 2  # both requests were acknowledged


def test_event_channel_waits_for_a_complete_request():
    seen = []
    channel = _channel(seen.append)
    channel.buffer = _event_request(b"x" * 10)[:-4]
    channel.handle_received()
    assert seen == [] and channel.send.call_count == 0


def test_event_channel_without_callback_still_acknowledges():
    channel = _channel(None)
    channel.buffer = _event_request(b"")
    channel.handle_received()
    assert channel.send.call_count == 1


async def test_connect_sets_up_the_event_channel(receiver, monkeypatch):
    mock, port = receiver
    opened = []
    channel = Mock()

    async def fake_setup_channel(factory, verifier, address, event_port, *salts):
        opened.append((verifier, address, event_port, salts))
        return None, channel

    monkeypatch.setattr(airplay, "setup_channel", fake_setup_channel)

    async def verify(connection):
        return "verifier"

    player = AirPlayPlayer("127.0.0.1", port, secure=verify)
    await player.connect()
    assert opened[0][0] == "verifier" and opened[0][2] == 1
    await player.close()
    channel.close.assert_called_once()


async def test_connect_requires_an_event_port():
    mock = MockReceiver(event_port=False)
    port = await mock.start()

    async def verify(connection):
        return "verifier"

    player = AirPlayPlayer("127.0.0.1", port, secure=verify)
    with pytest.raises(AirPlayError, match="event port"):
        await player.connect()
    await player.close()
    mock.server.close()


async def test_default_verify_needs_credentials():
    player = AirPlayPlayer("127.0.0.1", 1)
    with pytest.raises(exceptions.InvalidCredentialsError):
        await player._verify(Mock())


async def test_default_verify_uses_stored_credentials(monkeypatch):
    seen = []

    async def fake_verify(credentials, connection):
        seen.append((credentials, connection))
        return "verifier"

    monkeypatch.setattr(airplay, "verify_connection", fake_verify)
    player = AirPlayPlayer("127.0.0.1", 1, credentials="creds")
    assert await player._verify("conn") == "verifier"
    assert seen == [("creds", "conn")]


async def test_stop_before_connect_and_after_connection_loss(receiver):
    mock, port = receiver
    await AirPlayPlayer("127.0.0.1", 1).stop()  # never connected: nothing to do

    player = AirPlayPlayer("127.0.0.1", port, secure=_no_verify)
    await player.connect()
    player.item_uuid = "abc"

    async def lost(*args, **kwargs):
        raise exceptions.ConnectionLostError("gone")

    player._rtsp = lost
    await player.stop()  # must swallow the error
    await player.close()
    await player.close()  # and closing twice is harmless


async def test_keepalive_failure_ends_playback(receiver, monkeypatch):
    mock, port = receiver
    monkeypatch.setattr(airplay, "KEEPALIVE_INTERVAL", 0.01)
    player = AirPlayPlayer("127.0.0.1", port, secure=_no_verify)
    await player.connect()

    async def failing(*args, **kwargs):
        raise TimeoutError

    player._rtsp = failing
    await asyncio.wait_for(player.wait_until_ended(), 2)
    await player.close()


async def test_events_reach_the_callback(receiver):
    mock, port = receiver
    events = []
    player = AirPlayPlayer("127.0.0.1", port, secure=_no_verify, on_event=events.append)
    player._event({"name": "playing"})
    assert events == [{"name": "playing"}] and not player.ended.is_set()


async def test_a_dropped_item_is_a_failure_not_an_ending(receiver):
    mock, port = receiver
    player = AirPlayPlayer("127.0.0.1", port, secure=_no_verify)
    player._event({"params": {"data": {"reason": "ReasonAdvanceDueToError", "type": "currentItemChanged"}}})
    assert player.failed and player.ended.is_set()
    player._event({"params": {"data": {"name": "itemPlayedToEnd"}}})
    assert player.failed
