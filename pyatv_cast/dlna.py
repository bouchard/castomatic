"""DLNA / UPnP AV (MediaRenderer) playback, an alternative to AirPlay.

The renderer is controlled with SOAP calls to its AVTransport service
(``SetAVTransportURI``, ``Play``, ``Pause``, ``Seek``, ``Stop``) and then fetches the
media itself over HTTP from this machine, exactly like the AirPlay path. It is
unauthenticated, so nothing needs to be paired.
"""

import asyncio
import logging
import mimetypes
import socket
import xml.etree.ElementTree as ET
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

import aiohttp
import ifaddr
from aiohttp import web
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

SSDP_GROUP = ("239.255.255.250", 1900)
MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
CONNECTION_MANAGER = "urn:schemas-upnp-org:service:ConnectionManager:1"
POLL_INTERVAL = 2.0
POLL_FAILURES = 5
PLAY_WAIT = 15.0

_DEVICE_NS = {"d": "urn:schemas-upnp-org:device-1-0"}
_ENDED_STATES = ("STOPPED", "NO_MEDIA_PRESENT")
_FLAGS = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"


class DlnaError(Exception):
    """Raised when the renderer rejects a request or cannot be found.

    ``code`` is the UPnP error code when the renderer sent one.
    """

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        """Initialize a new DlnaError."""
        super().__init__(message)
        self.code = code


class Renderer:
    """A discovered MediaRenderer."""

    def __init__(
        self,
        location: str,
        friendly_name: str,
        udn: str,
        control_url: str,
        connection_url: Optional[str] = None,
    ):
        """Initialize a new Renderer."""
        self.location = location
        self.friendly_name = friendly_name
        self.udn = udn
        self.control_url = control_url
        self.connection_url = connection_url

    @property
    def host(self) -> str:
        """Address of the renderer."""
        return urlparse(self.location).hostname or ""


def parse_description(location: str, xml_text: str) -> Renderer:
    """Parse a UPnP device description and find the AVTransport control URL."""
    root = ET.fromstring(xml_text)
    device = root.find("d:device", _DEVICE_NS)
    if device is None:
        raise DlnaError(f"{location}: not a UPnP device description")
    base = root.findtext("d:URLBase", default=location, namespaces=_DEVICE_NS)
    control = connection = None
    for service in root.iter("{%s}service" % _DEVICE_NS["d"]):
        kind = service.findtext("d:serviceType", namespaces=_DEVICE_NS)
        url = urljoin(base, service.findtext("d:controlURL", namespaces=_DEVICE_NS) or "")
        if kind == AV_TRANSPORT:
            control = url
        elif kind == CONNECTION_MANAGER:
            connection = url
    if control is None:
        raise DlnaError(f"{location}: device has no AVTransport service")
    return Renderer(
        location,
        device.findtext("d:friendlyName", default="", namespaces=_DEVICE_NS),
        device.findtext("d:UDN", default="", namespaces=_DEVICE_NS),
        control,
        connection,
    )


class _Search(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.locations: List[str] = []

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        for line in data.decode(errors="replace").split("\r\n"):
            if line.lower().startswith("location:"):
                location = line.split(":", 1)[1].strip()
                if location not in self.locations:
                    self.locations.append(location)


def local_addresses() -> List[str]:
    """IPv4 addresses of this machine's interfaces, excluding loopback and link-local."""
    found = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if isinstance(ip.ip, str) and not ip.ip.startswith(("127.", "169.254.")):
                found.append(ip.ip)
    return found


async def ssdp_search(
    timeout: float = 3.0, target: Tuple[str, int] = SSDP_GROUP
) -> List[str]:
    """Return the description URLs of every MediaRenderer that answers an M-SEARCH.

    The search is sent from every local interface. A single multicast socket would
    leave through the default route, which on a machine with a VPN or tunnel is often
    not the LAN.
    """
    loop = asyncio.get_running_loop()
    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_GROUP[0]}:{SSDP_GROUP[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {max(1, int(timeout))}\r\n"
        f"ST: {MEDIA_RENDERER}\r\n\r\n"
    ).encode()
    endpoints = []
    for address in local_addresses():
        try:
            transport, protocol = await loop.create_datagram_endpoint(
                _Search, family=socket.AF_INET, local_addr=(address, 0)
            )
        except OSError as ex:
            _LOGGER.debug("cannot search from %s: %s", address, ex)
            continue
        sock = transport.get_extra_info("socket")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
        transport.sendto(message, target)
        endpoints.append((transport, protocol))
    try:
        await asyncio.sleep(timeout)
        locations: List[str] = []
        for _, protocol in endpoints:
            locations += [loc for loc in protocol.locations if loc not in locations]
        return locations
    finally:
        for transport, _ in endpoints:
            transport.close()


async def _fetch(http: aiohttp.ClientSession, location: str) -> Optional[Renderer]:
    try:
        async with http.get(location, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return parse_description(location, await resp.text())
    except (aiohttp.ClientError, asyncio.TimeoutError, ET.ParseError, DlnaError) as ex:
        _LOGGER.debug("ignoring %s: %s", location, ex)
        return None


async def discover_renderers(
    timeout: float = 3.0, target: Tuple[str, int] = SSDP_GROUP
) -> List[Renderer]:
    """Find every MediaRenderer on the network."""
    locations = await ssdp_search(timeout, target)
    async with aiohttp.ClientSession() as http:
        found = [await _fetch(http, location) for location in locations]
    return [renderer for renderer in found if renderer]


async def find_renderer(
    host: Optional[str] = None,
    name: Optional[str] = None,
    location: Optional[str] = None,
    timeout: float = 3.0,
    target: Tuple[str, int] = SSDP_GROUP,
) -> Renderer:
    """Find one renderer by description URL, address, or (part of) its name or UDN."""
    locations = [location] if location else await ssdp_search(timeout, target)
    async with aiohttp.ClientSession() as http:
        found = [r for r in [await _fetch(http, loc) for loc in locations] if r]
    for renderer in found:
        if host and renderer.host == host:
            return renderer
        if name and name.lower() in (renderer.friendly_name + renderer.udn).lower():
            return renderer
        if location:
            return renderer
    seen = ", ".join(f"{r.friendly_name} ({r.host})" for r in found) or "none"
    raise DlnaError(f"no matching DLNA renderer found; saw: {seen}")


def didl(
    url: str, title: str, mime: str, subtitle_url: Optional[str] = None, flags: str = _FLAGS
) -> str:
    """Build the DIDL-Lite metadata describing the item.

    A subtitle is announced the way Samsung renderers read it (``sec:CaptionInfoEx``)
    and as a second ``res`` of type ``text/srt`` for other renderers.
    """
    caption = ""
    if subtitle_url:
        caption = (
            f'<res protocolInfo="http-get:*:text/srt:*">{escape(subtitle_url)}</res>'
            f'<sec:CaptionInfoEx sec:type="srt">{escape(subtitle_url)}</sec:CaptionInfoEx>'
        )
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:sec="http://www.sec.co.kr/">'
        '<item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{escape(title)}</dc:title>"
        "<upnp:class>object.item.videoItem</upnp:class>"
        f'<res protocolInfo="http-get:*:{mime}:{flags}">{escape(url)}</res>'
        f"{caption}</item></DIDL-Lite>"
    )


def clock(seconds: float) -> str:
    """Format seconds as the ``H:MM:SS`` used by Seek."""
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


class DlnaPlayer:
    """Plays a URL on a DLNA MediaRenderer. Same interface as :class:`AirPlayPlayer`."""

    def __init__(
        self,
        renderer: Renderer,
        mime: str = "application/vnd.apple.mpegurl",
        flags: str = _FLAGS,
    ):
        """Initialize a new DlnaPlayer."""
        self.renderer = renderer
        self.mime = mime
        self.flags = flags
        self.ended = asyncio.Event()
        self._http: Optional[aiohttp.ClientSession] = None
        self._poll: Optional["asyncio.Task[None]"] = None
        self.local_ip = ""

    async def connect(self) -> None:
        """Open the HTTP session and work out which local address reaches the renderer."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((self.renderer.host, 9))  # no packet is sent
            self.local_ip = probe.getsockname()[0]
        finally:
            probe.close()
        self._http = aiohttp.ClientSession()

    async def close(self) -> None:
        """Stop polling and close the HTTP session."""
        if self._poll:
            self._poll.cancel()
            self._poll = None
        if self._http:
            await self._http.close()
            self._http = None

    async def play(
        self,
        url: str,
        title: str = "",
        start: float = 0.0,
        subtitle_url: Optional[str] = None,
    ) -> None:
        """Load ``url`` on the renderer and start playing, optionally from ``start``.

        ``subtitle_url`` points at an SRT file the renderer should show.
        """
        if self._poll:  # this Stop must not read as the end of the video
            self._poll.cancel()
            self._poll = None
        self.ended.clear()
        try:  # a renderer left mid-load by an earlier session answers "transport is locked"
            await self._action("Stop")
        except DlnaError:
            _LOGGER.debug("nothing to stop")
        await self._action(
            "SetAVTransportURI",
            CurrentURI=escape(url),
            CurrentURIMetaData=escape(didl(url, title, self.mime, subtitle_url, self.flags)),
        )
        await self._action("Play", Speed="1")
        self._poll = asyncio.ensure_future(self._poll_state())
        if start > 0:
            await self._wait_for("PLAYING", PLAY_WAIT)
            await self._action("Seek", Unit="REL_TIME", Target=clock(start))

    async def pause(self) -> None:
        """Pause playback."""
        await self._action("Pause")

    async def resume(self) -> None:
        """Resume playback."""
        await self._action("Play", Speed="1")

    async def wait_until_ended(self) -> None:
        """Block until playback has stopped."""
        await self.ended.wait()

    async def stop(self) -> None:
        """Stop playback."""
        if self._http is None:
            return
        try:
            await self._action("Stop")
        except (DlnaError, aiohttp.ClientError, asyncio.TimeoutError):
            _LOGGER.debug("renderer did not acknowledge Stop")

    # -- internals --------------------------------------------------------

    async def sink_types(self) -> Set[str]:
        """The media types the renderer says it can play (empty if it cannot say)."""
        if not self.renderer.connection_url:
            return set()
        try:
            info = await self._action(
                "GetProtocolInfo", service=CONNECTION_MANAGER,
                url=self.renderer.connection_url, instance=False,
            )  # fmt: skip
        except (DlnaError, aiohttp.ClientError, asyncio.TimeoutError):
            return set()
        types = set()
        for entry in info.get("Sink", "").split(","):
            fields = entry.split(":")
            if len(fields) >= 3 and fields[2] not in ("", "*"):
                types.add(fields[2].strip().lower())
        return types

    async def supports_hls(self) -> bool:
        """Whether the renderer lists an HLS media type among what it can play."""
        return any("mpegurl" in mime for mime in await self.sink_types())

    async def state(self) -> str:
        """The renderer's transport state, e.g. PLAYING or TRANSITIONING."""
        return await self._state()

    async def position(self) -> Optional[float]:
        """Seconds into the video the renderer is showing, or None if it will not say."""
        try:
            info = await self._action("GetPositionInfo")
            hours, minutes, seconds = info.get("RelTime", "").split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except (DlnaError, aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None  # some answer NOT_IMPLEMENTED, or an empty time, while stopped

    async def _action(
        self,
        name: str,
        service: str = AV_TRANSPORT,
        url: Optional[str] = None,
        instance: bool = True,
        **arguments: str,
    ) -> Dict[str, str]:
        assert self._http is not None
        if instance:
            arguments = {"InstanceID": "0", **arguments}
        args = "".join(f"<{k}>{v}</{k}>" for k, v in arguments.items())
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{name} xmlns:u="{service}">{args}</u:{name}>'
            "</s:Body></s:Envelope>"
        )
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{service}#{name}"',
        }
        async with self._http.post(
            url or self.renderer.control_url, data=body.encode(), headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:  # fmt: skip
            text = await resp.text()
            if resp.status != 200:
                raise DlnaError(
                    f"{name} failed: HTTP {resp.status} {_upnp_error(text)}",
                    _upnp_code(text),
                )
        reply = ET.fromstring(text).find(".//{%s}%sResponse" % (service, name))
        return {child.tag.split("}")[-1]: child.text or "" for child in (reply or [])}

    async def _state(self) -> str:
        info = await self._action("GetTransportInfo")
        return info.get("CurrentTransportState", "")

    async def _wait_for(self, state: str, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while await self._state() != state:
            if asyncio.get_running_loop().time() > deadline:
                raise DlnaError(f"renderer never reached {state}")
            await asyncio.sleep(POLL_INTERVAL / 4)

    async def _poll_state(self) -> None:
        """Mark playback ended once the renderer stops after having played."""
        played = False
        failures = 0
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                state = await self._state()
                failures = 0
            except (DlnaError, aiohttp.ClientError, asyncio.TimeoutError):
                failures += 1
                if failures >= POLL_FAILURES:
                    _LOGGER.debug("renderer stopped answering")
                    self.ended.set()
                    return
                continue
            played = played or state in ("PLAYING", "TRANSITIONING")
            if played and state in _ENDED_STATES:
                self.ended.set()
                return


def _upnp_error(text: str) -> str:
    try:
        root = ET.fromstring(text)
        code = root.findtext(".//{urn:schemas-upnp-org:control-1-0}errorCode")
        desc = root.findtext(".//{urn:schemas-upnp-org:control-1-0}errorDescription")
        return f"UPnP error {code}: {desc}" if code else ""
    except ET.ParseError:
        return ""


def guess_mime(path: Path) -> str:
    """Best-effort media type for a video file."""
    if path.suffix.lower() == ".mkv":
        return "video/x-mkv"  # what DLNA renderers list, not video/x-matroska
    return mimetypes.guess_type(path.name)[0] or "video/mp4"


class DirectFileServer:
    """Serve one file unchanged, with Range support, for renderers that play it as is.

    An optional subtitle is served as ``/subs.srt`` and announced to the renderer in
    the ``CaptionInfo.sec`` header (what Samsung renderers ask for).
    """

    def __init__(
        self,
        path: Path,
        address: str,
        port: int = 0,
        subtitle: Any = None,
        respond: Optional[Callable[..., Awaitable[web.StreamResponse]]] = None,
    ) -> None:
        """Initialize a new DirectFileServer.

        ``respond(request, headers)``, if given, answers instead of reading ``path``: how
        a file still being copied from a slow drive is served.
        """
        self._path = path
        self._respond = respond
        self._address = address
        self._port = port
        self._subtitle = subtitle
        self._srt = b""
        self._runner: Optional[web.AppRunner] = None
        self.subtitle_url: Optional[str] = None
        self.requests = 0  # what the renderer asked for: 0 means it never connected

    async def _serve(self, request: web.Request) -> web.StreamResponse:
        self.requests += 1
        _LOGGER.debug(
            "%s %s from %s headers=%s", request.method, request.path, request.remote,
            dict(request.headers),
        )  # fmt: skip
        # Renderers ask for these with "getcontentFeatures.dlna.org: 1" and some
        # (Samsung) wait forever without an answer
        headers = {
            "transferMode.dlna.org": "Streaming",
            "contentFeatures.dlna.org": _FLAGS,
            "Accept-Ranges": "bytes",
        }
        if self.subtitle_url:
            headers["CaptionInfo.sec"] = self.subtitle_url
        if self._respond:
            return await self._respond(request, headers)
        return web.FileResponse(self._path, headers=headers)

    async def _serve_subtitle(self, request: web.Request) -> web.Response:
        _LOGGER.debug("subtitle requested by %s", request.remote)
        return web.Response(body=self._srt, content_type="text/srt", charset="utf-8")

    async def start(self) -> str:
        """Start serving and return the file's URL."""
        app = web.Application()
        app.router.add_route("*", "/media{ext:\\.[A-Za-z0-9]+}", self._serve)
        if self._subtitle is not None:
            self._srt = await self._subtitle.srt()
            app.router.add_get("/subs.srt", self._serve_subtitle)
        # A request waiting on a slow drive must not hold up stopping
        self._runner = web.AppRunner(app, access_log=None, shutdown_timeout=1.0)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._address, self._port)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        base = f"http://{self._address}:{port}"
        if self._subtitle is not None:
            self.subtitle_url = f"{base}/subs.srt"
        return f"{base}/media{self._path.suffix or '.mp4'}"

    async def stop(self) -> None:
        """Stop serving."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None


def _upnp_code(text: str) -> Optional[int]:
    try:
        code = ET.fromstring(text).findtext(".//{urn:schemas-upnp-org:control-1-0}errorCode")
        return int(code) if code else None
    except (ET.ParseError, ValueError):
        return None
