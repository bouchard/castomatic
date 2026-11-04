"""DLNA: description parsing, SSDP discovery, SOAP control and polling (mock renderer)."""

import asyncio
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

from pyatv_cast import dlna

REAL_LOCAL_ADDRESSES = dlna.local_addresses

DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <friendlyName>[TV] Living Room</friendlyName>
    <UDN>uuid:1234</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
        <controlURL>/rc</controlURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <controlURL>/cm</controlURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <controlURL>/ctl</controlURL>
      </service>
    </serviceList>
  </device>
</root>"""

ENVELOPE = (
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>{}'
    "</s:Body></s:Envelope>"
)


class MockRenderer:
    """An aiohttp server speaking just enough UPnP AVTransport."""

    def __init__(self):
        self.actions = []
        self.states = ["STOPPED"]  # consumed by GetTransportInfo, last one repeats
        self.fail = {}  # action name -> HTTP status
        self.sink = "http-get:*:video/mp4:*,http-get:*:video/x-mkv:*"

    async def description(self, request):
        return web.Response(text=DESCRIPTION, content_type="text/xml")

    async def control(self, request):
        action = request.headers["SOAPACTION"].strip('"').split("#")[1]
        body = ET.fromstring(await request.read())
        args = {
            el.tag: el.text
            for el in body.find(f".//{{{dlna.AV_TRANSPORT}}}{action}")
        }
        self.actions.append((action, args))
        if action in self.fail:
            error = (
                '<s:Fault><detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
                "<errorCode>701</errorCode><errorDescription>Nope</errorDescription>"
                "</UPnPError></detail></s:Fault>"
            )
            return web.Response(status=self.fail[action], text=ENVELOPE.format(error))
        inner = ""
        if action == "GetTransportInfo":
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            inner = f"<CurrentTransportState>{state}</CurrentTransportState>"
        reply = f'<u:{action}Response xmlns:u="{dlna.AV_TRANSPORT}">{inner}</u:{action}Response>'
        return web.Response(text=ENVELOPE.format(reply), content_type="text/xml")

    async def connection(self, request):
        if "GetProtocolInfo" in self.fail:
            return web.Response(status=500)
        reply = (
            f'<u:GetProtocolInfoResponse xmlns:u="{dlna.CONNECTION_MANAGER}">'
            f"<Source></Source><Sink>{self.sink}</Sink></u:GetProtocolInfoResponse>"
        )
        return web.Response(text=ENVELOPE.format(reply), content_type="text/xml")

    async def start(self):
        app = web.Application()
        app.router.add_post("/cm", self.connection)
        app.router.add_get("/dmr", self.description)
        app.router.add_post("/ctl", self.control)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}/dmr"


@pytest.fixture(autouse=True)
def loopback_search(monkeypatch):
    monkeypatch.setattr(dlna, "local_addresses", lambda: ["127.0.0.1"])


@pytest.fixture
async def renderer(monkeypatch):
    monkeypatch.setattr(dlna, "POLL_INTERVAL", 0.02)
    mock = MockRenderer()
    mock.location = await mock.start()
    yield mock
    await mock.runner.cleanup()


def test_parse_description():
    renderer = dlna.parse_description("http://10.0.0.5:9197/dmr", DESCRIPTION)
    assert renderer.friendly_name == "[TV] Living Room" and renderer.udn == "uuid:1234"
    assert renderer.control_url == "http://10.0.0.5:9197/ctl"
    assert renderer.connection_url == "http://10.0.0.5:9197/cm"
    assert renderer.host == "10.0.0.5"


def test_parse_description_honours_url_base():
    xml = DESCRIPTION.replace("<device>", "<URLBase>http://10.0.0.6:80/</URLBase><device>")
    assert dlna.parse_description("http://x/dmr", xml).control_url == "http://10.0.0.6:80/ctl"


def test_parse_description_errors():
    with pytest.raises(dlna.DlnaError, match="not a UPnP device"):
        dlna.parse_description("http://x", '<root xmlns="urn:schemas-upnp-org:device-1-0"/>')
    no_av = DESCRIPTION.replace("AVTransport", "Other")
    with pytest.raises(dlna.DlnaError, match="no AVTransport"):
        dlna.parse_description("http://x", no_av)


def test_didl_escapes_and_describes_the_item():
    text = dlna.didl("http://h/a.m3u8?x=1&y=2", "Tom & <Jerry>", "application/vnd.apple.mpegurl")
    root = ET.fromstring(text)  # well-formed despite the special characters
    ns = {"d": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/", "dc": "http://purl.org/dc/elements/1.1/"}
    assert root.find(".//dc:title", ns).text == "Tom & <Jerry>"
    res = root.find(".//d:res", ns)
    assert res.text == "http://h/a.m3u8?x=1&y=2"
    assert "application/vnd.apple.mpegurl" in res.attrib["protocolInfo"]


def test_clock_formats_seconds():
    assert dlna.clock(0) == "0:00:00"
    assert dlna.clock(3725.9) == "1:02:05"


class _Responder(asyncio.DatagramProtocol):
    def __init__(self, location):
        self.location = location
        self.queries = []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.queries.append(data.decode())
        reply = f"HTTP/1.1 200 OK\r\nLOCATION: {self.location}\r\nST: x\r\n\r\n"
        self.transport.sendto(reply.encode(), addr)
        self.transport.sendto(reply.encode(), addr)  # duplicates are collapsed


async def _responder(location):
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: _Responder(location), local_addr=("127.0.0.1", 0)
    )
    return transport, protocol, transport.get_extra_info("sockname")


async def test_ssdp_search_collects_locations():
    transport, responder, target = await _responder("http://127.0.0.1:1/dmr")
    try:
        assert await dlna.ssdp_search(timeout=0.2, target=target) == ["http://127.0.0.1:1/dmr"]
    finally:
        transport.close()
    assert dlna.MEDIA_RENDERER in responder.queries[0]


async def test_find_renderer_by_location_host_and_name(renderer):
    transport, _, target = await _responder(renderer.location)
    try:
        by_loc = await dlna.find_renderer(location=renderer.location)
        by_host = await dlna.find_renderer(host="127.0.0.1", timeout=0.2, target=target)
        by_name = await dlna.find_renderer(name="living room", timeout=0.2, target=target)
    finally:
        transport.close()
    assert by_loc.friendly_name == by_host.friendly_name == by_name.friendly_name


async def test_find_renderer_reports_what_it_saw(renderer):
    transport, _, target = await _responder(renderer.location)
    try:
        with pytest.raises(dlna.DlnaError, match=r"saw: \[TV\] Living Room"):
            await dlna.find_renderer(host="10.9.9.9", timeout=0.2, target=target)
    finally:
        transport.close()


async def test_find_renderer_skips_unreachable_devices():
    transport, _, target = await _responder("http://127.0.0.1:9/dmr")
    try:
        with pytest.raises(dlna.DlnaError, match="saw: none"):
            await dlna.find_renderer(host="127.0.0.1", timeout=0.2, target=target)
    finally:
        transport.close()


async def _player(mock):
    player = dlna.DlnaPlayer(await dlna.find_renderer(location=mock.location))
    await player.connect()
    return player


async def test_play_sequence_and_controls(renderer):
    renderer.states = ["TRANSITIONING", "PLAYING", "PLAYING", "STOPPED"]
    player = await _player(renderer)
    assert player.local_ip == "127.0.0.1"

    await player.play("http://127.0.0.1:1/index.m3u8", title="Movie", start=125.0)
    await asyncio.wait_for(player.wait_until_ended(), 5)  # stops after it has played
    await player.pause()
    await player.resume()
    await player.stop()
    await player.close()

    names = [a for a, _ in renderer.actions]
    assert names[:3] == ["Stop", "SetAVTransportURI", "Play"]  # Stop clears an old session
    assert "Seek" in names and names[-3:] == ["Pause", "Play", "Stop"]
    uri = next(args for a, args in renderer.actions if a == "SetAVTransportURI")
    assert uri["CurrentURI"] == "http://127.0.0.1:1/index.m3u8"
    assert "Movie" in uri["CurrentURIMetaData"]
    seek = next(args for a, args in renderer.actions if a == "Seek")
    assert (seek["Unit"], seek["Target"]) == ("REL_TIME", "0:02:05")
    assert all(args["InstanceID"] == "0" for _, args in renderer.actions)


async def test_not_ended_before_it_has_played(renderer):
    renderer.states = ["NO_MEDIA_PRESENT"]  # e.g. still loading
    player = await _player(renderer)
    await player.play("http://x/a.m3u8")
    await asyncio.sleep(0.15)
    assert not player.ended.is_set()
    await player.close()


async def test_seek_gives_up_if_playback_never_starts(renderer, monkeypatch):
    monkeypatch.setattr(dlna, "PLAY_WAIT", 0.1)
    renderer.states = ["TRANSITIONING"]
    player = await _player(renderer)
    with pytest.raises(dlna.DlnaError, match="never reached PLAYING"):
        await player.play("http://x/a.m3u8", start=30.0)
    await player.close()


async def test_upnp_errors_are_reported(renderer):
    renderer.fail["SetAVTransportURI"] = 500
    player = await _player(renderer)
    with pytest.raises(dlna.DlnaError, match="HTTP 500 UPnP error 701: Nope"):
        await player.play("http://x/a.m3u8")
    await player.close()


async def test_error_without_upnp_body(renderer):
    renderer.fail["Pause"] = 400
    player = await _player(renderer)
    renderer.fail["Pause"] = 400
    assert dlna._upnp_error("not xml") == ""
    with pytest.raises(dlna.DlnaError, match="Pause failed: HTTP 400"):
        await player.pause()
    await player.close()


async def test_polling_gives_up_when_the_renderer_disappears(renderer, monkeypatch):
    monkeypatch.setattr(dlna, "POLL_FAILURES", 2)
    renderer.states = ["PLAYING"]
    player = await _player(renderer)
    await player.play("http://x/a.m3u8")
    renderer.fail["GetTransportInfo"] = 500
    await asyncio.wait_for(player.wait_until_ended(), 5)
    await player.close()


async def test_stop_tolerates_a_dead_renderer(renderer):
    player = await _player(renderer)
    renderer.fail["Stop"] = 500
    await player.stop()  # swallowed
    await player.close()
    await player.stop()  # never connected / already closed: no-op
    await player.close()


async def test_direct_file_server_supports_ranges(tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_bytes(bytes(range(100)))
    server = dlna.DirectFileServer(media, "127.0.0.1")
    url = await server.start()
    try:
        assert url.endswith("/media.mkv")
        async with aiohttp.ClientSession() as http:
            async with http.get(url, headers={"Range": "bytes=10-19"}) as resp:
                assert resp.status == 206
                assert await resp.read() == bytes(range(10, 20))
                assert resp.headers["transferMode.dlna.org"] == "Streaming"
                assert resp.headers["Accept-Ranges"] == "bytes"
                assert "DLNA.ORG_OP=01" in resp.headers["contentFeatures.dlna.org"]
    finally:
        await server.stop()
    await server.stop()


def test_guess_mime():
    from pathlib import Path

    assert dlna.guess_mime(Path("a.mp4")) == "video/mp4"
    assert dlna.guess_mime(Path("a.unknownext")) == "video/mp4"


def test_local_addresses_skip_loopback_and_link_local(monkeypatch):
    def adapter(*ips):
        return SimpleNamespace(ips=[SimpleNamespace(ip=ip) for ip in ips])

    monkeypatch.setattr(
        dlna.ifaddr, "get_adapters",
        lambda: [adapter("127.0.0.1"), adapter("10.0.0.5", ("fe80::1", 0, 0)),
                 adapter("169.254.1.2", "100.64.0.9")],
    )  # fmt: skip
    assert REAL_LOCAL_ADDRESSES() == ["10.0.0.5", "100.64.0.9"]


async def test_ssdp_search_skips_interfaces_it_cannot_bind(monkeypatch):
    transport, _, target = await _responder("http://127.0.0.1:1/dmr")
    # 203.0.113.1 (TEST-NET) is not one of our addresses, so binding to it fails
    monkeypatch.setattr(dlna, "local_addresses", lambda: ["203.0.113.1", "127.0.0.1"])
    try:
        assert await dlna.ssdp_search(timeout=0.2, target=target) == ["http://127.0.0.1:1/dmr"]
    finally:
        transport.close()


async def test_supports_hls_reads_the_renderers_media_types(renderer):
    player = await _player(renderer)
    assert not await player.supports_hls()  # mp4 and mkv only
    renderer.sink += ",http-get:*:application/vnd.apple.mpegurl:*"
    assert await player.supports_hls()
    await player.close()


async def test_supports_hls_is_false_when_it_cannot_tell(renderer):
    player = await _player(renderer)
    renderer.fail["GetProtocolInfo"] = 500
    assert not await player.supports_hls()
    player.renderer.connection_url = None
    assert not await player.supports_hls()
    await player.close()


def test_guess_mime_uses_the_type_renderers_list_for_mkv():
    from pathlib import Path

    assert dlna.guess_mime(Path("a.MKV")) == "video/x-mkv"


async def test_play_ignores_a_failing_initial_stop(renderer):
    renderer.fail["Stop"] = 500  # e.g. nothing loaded yet
    player = await _player(renderer)
    await player.play("http://x/a.mp4")
    assert [a for a, _ in renderer.actions][:3] == ["Stop", "SetAVTransportURI", "Play"]
    await player.close()


async def test_discover_renderers_returns_each_one_found(renderer):
    transport, _, target = await _responder(renderer.location)
    try:
        found = await dlna.discover_renderers(timeout=0.2, target=target)
    finally:
        transport.close()
    assert [r.friendly_name for r in found] == ["[TV] Living Room"]


async def test_sink_types_lists_what_the_renderer_plays(renderer):
    renderer.sink = "http-get:*:video/mp4:*,http-get:*:VIDEO/x-mkv:DLNA.ORG_PN=X,http-get:*:*:*,broken"
    player = await _player(renderer)
    assert await player.sink_types() == {"video/mp4", "video/x-mkv"}
    player.renderer.connection_url = None
    assert await player.sink_types() == set()
    await player.close()


async def test_state_reads_the_transport_state(renderer):
    renderer.states = ["PLAYING"]
    player = await _player(renderer)
    assert await player.state() == "PLAYING"
    await player.close()


async def test_upnp_error_code_is_kept_on_the_exception(renderer):
    renderer.fail["Pause"] = 500
    player = await _player(renderer)
    with pytest.raises(dlna.DlnaError) as info:
        await player.pause()
    assert info.value.code == 701
    await player.close()
    assert dlna.DlnaError("plain").code is None
    assert dlna._upnp_code("not xml") is None


def test_didl_uses_the_given_flags():
    text = dlna.didl("http://h/a.ts", "T", "video/mpeg", flags="DLNA.ORG_OP=00")
    assert "http-get:*:video/mpeg:DLNA.ORG_OP=00" in text


async def test_direct_server_counts_what_the_renderer_asks_for(tmp_path):
    media = tmp_path / "movie.mp4"
    media.write_bytes(b"x")
    server = dlna.DirectFileServer(media, "127.0.0.1")
    url = await server.start()
    assert server.requests == 0
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url) as resp:
                await resp.read()
        assert server.requests == 1
    finally:
        await server.stop()


@pytest.mark.parametrize(
    "answer,expected",
    [
        ({"RelTime": "0:01:02.500"}, 62.5),
        ({"RelTime": "1:00:00"}, 3600.0),
        ({"RelTime": "NOT_IMPLEMENTED"}, None),
        ({}, None),
    ],
)
async def test_position(answer, expected, monkeypatch):
    player = dlna.DlnaPlayer.__new__(dlna.DlnaPlayer)

    async def action(name):
        assert name == "GetPositionInfo"
        return answer

    monkeypatch.setattr(player, "_action", action)
    assert await player.position() == expected


async def test_position_when_the_renderer_refuses(monkeypatch):
    player = dlna.DlnaPlayer.__new__(dlna.DlnaPlayer)

    async def action(name):
        raise dlna.DlnaError("not supported")

    monkeypatch.setattr(player, "_action", action)
    assert await player.position() is None


async def test_reloading_does_not_read_as_the_end(monkeypatch):
    player = dlna.DlnaPlayer.__new__(dlna.DlnaPlayer)
    player.ended = asyncio.Event()
    player.flags = player.mime = ""
    old = asyncio.ensure_future(asyncio.sleep(10))
    player._poll = old

    async def action(name, **kw):
        return {}

    async def forever():
        await asyncio.sleep(10)

    monkeypatch.setattr(player, "_action", action)
    monkeypatch.setattr(player, "_poll_state", forever)
    await player.play("http://x/a.mp4")
    await asyncio.sleep(0)
    assert old.cancelled() and player._poll is not old
    player._poll.cancel()
