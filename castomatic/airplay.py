"""AirPlay 2 HLS playback session.

The flow was captured from Airflow talking to a receiver. Everything is on one
HAP-encrypted RTSP control connection:

1. ``pair-verify`` with stored credentials.
2. ``SETUP`` (session), then connect the encrypted event channel it announces, then
   ``RECORD`` and ``GET /info``.
3. ``SETUP`` with a type 130 stream; the receiver answers with a ``streamID``.
4. ``POST /command`` (header ``X-Apple-StreamID``) with ``insertPlayQueueItem`` whose
   ``Content-Location`` is a plain ``http://`` HLS playlist, then ``setRate``. Command
   bodies are ``{"params": {"data": <binary plist>}}``.
5. The receiver fetches the playlist and segments itself. ``POST /feedback`` keeps the
   session alive.
6. ``removePlayQueueItem`` and ``TEARDOWN`` end it.
"""

import asyncio
import logging
import plistlib
from typing import Any, Awaitable, Callable, Dict, Optional
from uuid import uuid4

from pyatv import exceptions
from pyatv.auth.hap_pairing import HapCredentials
from pyatv.protocols.airplay.ap2_session import (
    EVENTS_READ_INFO,
    EVENTS_SALT,
    EVENTS_WRITE_INFO,
)
from pyatv.protocols.airplay.auth import verify_connection
from pyatv.protocols.airplay.channels import EventChannel
from pyatv.auth.hap_channel import setup_channel
from pyatv.support.http import HttpConnection, HttpResponse, http_connect
from pyatv.support.rtsp import RtspSession

_LOGGER = logging.getLogger(__name__)

STREAM_TYPE_HLS = 130
CLIENT_TYPE_UUID = "A6B27562-B43A-4F2D-B75F-82391E250194"
KEEPALIVE_INTERVAL = 2.0
CM_TIMESCALE = 10_000_000

# Returns whatever setup_channel() needs to derive event channel keys, or None to
# skip the event channel (used by tests against an unencrypted mock receiver).
SecureFunc = Callable[[HttpConnection], Awaitable[Any]]
EventFunc = Callable[[Dict[str, Any]], None]


class AirPlayError(exceptions.PlaybackError):
    """Raised when the receiver rejects or aborts HLS playback."""


def _command(body: Dict[str, Any]) -> Dict[str, Any]:
    return {"params": {"data": plistlib.dumps(body, fmt=plistlib.FMT_BINARY)}}


def _body(resp: HttpResponse) -> Dict[str, Any]:
    body = resp.body
    return plistlib.loads(body.encode() if isinstance(body, str) else body)


def _decode(data: Any) -> Any:
    """Decode nested binary plists so events are readable."""
    if isinstance(data, bytes) and data.startswith(b"bplist00"):
        try:
            return _decode(plistlib.loads(data))
        except Exception:  # pylint: disable=broad-except
            return data
    if isinstance(data, dict):
        return {k: _decode(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_decode(v) for v in data]
    return data


def _make_event_channel(callback: Optional[EventFunc]):
    class _Channel(EventChannel):
        def handle_received(self) -> None:
            if callback:
                data = self.buffer
                while data:
                    request, _, data = self.parse_request(data)
                    if request is None:
                        break
                    body = request.body
                    if isinstance(body, bytes) and body:
                        try:
                            callback(_decode(plistlib.loads(body)))
                        except Exception:  # pylint: disable=broad-except
                            _LOGGER.debug("undecodable event: %r", body[:64])
            super().handle_received()

    return _Channel


class AirPlayPlayer:
    """Plays an HLS playlist URL on an AirPlay 2 receiver."""

    def __init__(
        self,
        address: str,
        port: int,
        credentials: Optional[HapCredentials] = None,
        secure: Optional[SecureFunc] = None,
        on_event: Optional[EventFunc] = None,
        name: str = "castomatic",
    ) -> None:
        """Initialize a new AirPlayPlayer."""
        self._address = address
        self._port = port
        self._credentials = credentials
        self._secure = secure or self._verify
        self._on_event = on_event
        self._name = name
        self.control: Optional[HttpConnection] = None
        self.rtsp: Optional[RtspSession] = None
        self.stream_id: Optional[str] = None
        self.item_uuid: Optional[str] = None
        self.ended = asyncio.Event()
        self.failed = False  # the receiver gave up on the video rather than finishing it
        self._event_channel: Optional[EventChannel] = None
        self._keepalive: Optional["asyncio.Task[None]"] = None

    @property
    def local_ip(self) -> str:
        """This machine's address as seen by the receiver."""
        assert self.control is not None
        return self.control.local_ip

    async def _verify(self, connection: HttpConnection) -> Any:
        if self._credentials is None:
            raise exceptions.InvalidCredentialsError("no AirPlay credentials")
        return await verify_connection(self._credentials, connection)

    # -- session ----------------------------------------------------------

    async def connect(self) -> None:
        """Authenticate and set up the session and the HLS stream."""
        self.control = await http_connect(self._address, self._port)
        verifier = await self._secure(self.control)
        self.rtsp = RtspSession(self.control)

        resp = await self._rtsp(
            "SETUP",
            body={
                "deviceID": "02:00:00:00:00:01",
                "groupContainsGroupLeader": False,
                "groupUUID": str(uuid4()),
                "model": "castomatic",
                "name": self._name,
                "senderSupportsRelay": True,
                "sessionUUID": "00000000-0000-0000-0000-000000000000",
                "timingProtocol": "PTP",
            },
        )
        info = _body(resp)
        if verifier is not None:
            if "eventPort" not in info:
                raise AirPlayError("SETUP response missing event port")
            _, self._event_channel = await setup_channel(
                _make_event_channel(self._event),
                verifier,
                self.control.remote_ip,
                info["eventPort"],
                EVENTS_SALT,
                EVENTS_READ_INFO,
                EVENTS_WRITE_INFO,
            )
        await self._rtsp("RECORD")
        await self._rtsp("GET", uri="/info")

        resp = await self._rtsp(
            "SETUP",
            body={
                "streams": [
                    {
                        "clientTypeUUID": CLIENT_TYPE_UUID,
                        "clientUUID": str(uuid4()).upper(),
                        "controlType": 1,
                        "type": STREAM_TYPE_HLS,
                    }
                ]
            },
        )
        streams = _body(resp).get("streams") or []
        if not streams or "streamID" not in streams[0]:
            raise AirPlayError("SETUP did not return a stream")
        self.stream_id = str(streams[0]["streamID"])
        self._keepalive = asyncio.ensure_future(self._keepalive_loop())

    async def close(self) -> None:
        """Stop keepalives and close all connections."""
        if self._keepalive:
            self._keepalive.cancel()
            self._keepalive = None
        if self._event_channel:
            self._event_channel.close()
            self._event_channel = None
        if self.control:
            self.control.close()
            self.control = None

    # -- playback ---------------------------------------------------------

    async def play(self, url: str, title: str = "", start: float = 0.0) -> None:
        """Start playing the HLS playlist at ``url`` and begin playback."""
        self.item_uuid = str(uuid4())
        self.ended.clear()
        self.failed = False
        await self._command(
            {
                "type": "insertPlayQueueItem",
                "item": {
                    "Content-Location": url,
                    "Start-Position": {
                        "value": int(start * CM_TIMESCALE),
                        "timescale": CM_TIMESCALE,
                        "flags": 1,
                        "epoch": 0,
                    },
                    "clientBundleID": "com.apple.WebKit.WebContent",
                    "referenceRestrictions": 3,
                    "uuid": self.item_uuid,
                },
                "metadata": {
                    "Album": "",
                    "Artist": "",
                    "Title": title,
                    "TotalTrackCount": "",
                    "TrackNumber": "",
                },
            }
        )
        await self.set_rate(1.0)

    async def set_rate(self, rate: float) -> None:
        """Set the playback rate: 1 plays, 0 pauses."""
        await self._command({"type": "setRate", "rate": rate})

    async def pause(self) -> None:
        """Pause playback."""
        await self.set_rate(0.0)

    async def resume(self) -> None:
        """Resume playback."""
        await self.set_rate(1.0)

    async def wait_until_ended(self) -> None:
        """Block until the receiver reports the item played to the end."""
        await self.ended.wait()

    async def stop(self) -> None:
        """Remove the item and tear the stream down."""
        if self.rtsp is None:
            return
        try:
            if self.item_uuid:
                await self._command(
                    {"type": "removePlayQueueItem", "item": {"uuid": self.item_uuid}}
                )
            await self._rtsp(
                "TEARDOWN",
                body=_command({"streamID": self.stream_id, "type": STREAM_TYPE_HLS}),
            )
        except (exceptions.ConnectionLostError, TimeoutError):
            _LOGGER.debug("connection gone while stopping")

    # -- internals --------------------------------------------------------

    async def _command(self, body: Dict[str, Any]) -> HttpResponse:
        return await self._rtsp(
            "POST",
            uri="/command",
            headers={"X-Apple-StreamID": self.stream_id},
            body=_command(body),
        )

    async def _rtsp(
        self,
        method: str,
        uri: Optional[str] = None,
        headers: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> HttpResponse:
        assert self.rtsp is not None
        resp = await self.rtsp.exchange(
            method, uri=uri, headers=headers, body=body, allow_error=True
        )
        if resp.code != 200:
            raise AirPlayError(
                f"{method} {uri or ''} refused: {resp.code} {resp.message}"
            )
        return resp

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            try:
                await self._rtsp("POST", uri="/feedback")
            except (exceptions.ConnectionLostError, TimeoutError, AirPlayError):
                _LOGGER.debug("keepalive failed; stopping it")
                self.ended.set()
                return

    def _event(self, event: Dict[str, Any]) -> None:
        _LOGGER.debug("event: %s", event)
        text = repr(event)
        if "itemPlayedToEnd" in text:
            self.ended.set()
        elif "ReasonAdvanceDueToError" in text:  # e.g. a segment came too late: it is gone
            self.failed = True
            self.ended.set()
        if self._on_event:
            self._on_event(event)
