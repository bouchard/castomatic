"""Roku, through its External Control Protocol (ECP: plain HTTP on port 8060).

Devices answer an SSDP search for ``roku:ecp``, using the same per-interface search as DLNA.
Media is handed to the built-in media player channel ("PlayOnRoku", channel 15985) with
:meth:`rokuecp.Roku.play_on_roku`, and ``/query/media-player`` reports how it is going.

Two limits of that channel shape how media is sent:

* it cannot be told to start part way in, so a ``--start`` is done by sending a
  conversion that itself begins there (``seek=False`` in the capabilities);
* it takes no separate subtitle file, so subtitles are either burned in or left out.

Newer Roku OS versions can switch "Control by mobile apps" off, which blocks ECP. The
error raised for a refused request says where that setting is.
"""

import asyncio
import logging
import socket
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from castomatic import dlna
from castomatic.backends.base import (
    LIVE_HLS,
    STATE_LOADING,
    STATE_NO_MEDIA,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_STOPPED,
    SUBS_NONE,
    CastError,
    Capabilities,
)

_LOGGER = logging.getLogger(__name__)

PORT = 8060
SEARCH_TARGET = "roku:ecp"
POLL_INTERVAL = 2.0
POLL_FAILURES = 5
# What Roku's media player plays on every current model, per Roku's published list of
# supported formats. Nothing narrower is assumed for any particular model.
CAPABILITIES = Capabilities(
    sink=frozenset({"video/mp4", "video/x-mkv", "video/x-matroska", "video/quicktime"}),
    video=frozenset({"h264", "hevc"}),
    audio=frozenset({"aac", "ac3", "eac3", "mp3"}),
    hls=True,
    live=LIVE_HLS,
    subtitles=SUBS_NONE,
    https=True,
    live_video=frozenset({"h264"}),
    live_audio=frozenset({"aac", "ac3", "mp3"}),
)
SEEKS = False  # the media player channel cannot be told where to start
_STATES = {
    "play": STATE_PLAYING,
    "pause": STATE_PAUSED,
    "buffer": STATE_LOADING,
    "open": STATE_LOADING,
    "startup": STATE_LOADING,
    "stop": STATE_STOPPED,
    "close": STATE_STOPPED,
    "finished": STATE_STOPPED,
    "none": STATE_NO_MEDIA,
}
_FORMATS = {
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/x-mkv": "mkv",
    "video/x-matroska": "mkv",
    "application/vnd.apple.mpegurl": "hls",
    "application/x-mpegurl": "hls",
    "video/mp2t": "ts",
    "video/vnd.dlna.mpeg-tts": "ts",
}
ECP_HINT = (
    "On the Roku: Settings > System > Advanced system settings > Control by mobile apps, "
    "and set Network access to Default (or Permissive)."
)


class RokuError(CastError):
    """A Roku refused a request or could not be reached."""


@dataclass
class RokuInfo:
    """A Roku found on the network."""

    name: str
    host: str
    port: int = PORT
    serial: str = ""
    model: str = ""  # shown to the user only; never used to decide anything


def _host_port(location: str) -> tuple:
    parts = urlparse(location)
    return parts.hostname or "", parts.port or PORT


async def _describe(host: str, port: int, http_get: Any = None) -> RokuInfo:
    """Ask a Roku its name (``/query/device-info``), falling back to its address."""
    from rokuecp import Roku

    try:
        async with Roku(host, port=port) as roku:
            info = await (http_get(roku) if http_get else roku._request("/query/device-info"))
        data = info.get("device-info", {}) if isinstance(info, dict) else {}
        name = data.get("user-device-name") or data.get("friendly-device-name") or ""
        return RokuInfo(
            name or f"Roku {host}", host, port, data.get("serial-number", ""),
            data.get("model-name", ""),
        )  # fmt: skip
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.debug("Roku at %s did not describe itself: %s", host, ex)
        return RokuInfo(f"Roku {host}", host, port)


async def discover(timeout: float = 3.0, target: tuple = dlna.SSDP_GROUP) -> List[RokuInfo]:
    """Every Roku that answers an SSDP search within ``timeout`` seconds."""
    replies = await dlna.ssdp_replies(timeout, target, SEARCH_TARGET)
    seen: Dict[str, tuple] = {}
    for reply in replies:
        host, port = _host_port(reply["location"])
        if host:
            seen.setdefault(host, (host, port))
    return list(await asyncio.gather(*(_describe(h, p) for h, p in seen.values())))


def _local_ip_for(host: str) -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((host, 9))  # no packet is sent
        return probe.getsockname()[0]
    finally:
        probe.close()


def _ms(text: Any) -> Optional[float]:
    """``"12345 ms"`` -> 12.345."""
    try:
        return float(str(text).split()[0]) / 1000
    except (ValueError, IndexError):
        return None


class RokuPlayer:
    """Plays a URL with a Roku's built-in media player.

    Same interface as :class:`castomatic.dlna.DlnaPlayer`.
    """

    def __init__(self, info: RokuInfo, client_factory: Any = None) -> None:
        """Initialize a new RokuPlayer.

        ``client_factory(host, port=...)`` makes the :class:`rokuecp.Roku`; tests pass a fake.
        """
        self.info = info
        self.mime = "video/mp4"
        self.flags = ""  # DLNA only
        self.ended = asyncio.Event()
        self.error: Optional[str] = None
        self.local_ip = ""
        self._factory = client_factory
        self._roku: Any = None
        self._poll: Optional["asyncio.Task[None]"] = None

    async def connect(self) -> None:
        """Open the ECP client and work out which local address reaches the Roku."""
        self.local_ip = _local_ip_for(self.info.host)
        factory = self._factory
        if factory is None:
            from rokuecp import Roku

            factory = Roku
        self._roku = factory(self.info.host, port=self.info.port, request_timeout=8)

    async def close(self) -> None:
        """Stop polling and close the client."""
        if self._poll:
            self._poll.cancel()
            self._poll = None
        if self._roku is not None:
            await self._roku.close_session()
            self._roku = None

    async def capabilities(self) -> Capabilities:
        """What Roku's media player plays."""
        return CAPABILITIES

    async def play(
        self,
        url: str,
        title: str = "",
        start: float = 0.0,
        subtitle_url: Optional[str] = None,
    ) -> None:
        """Hand ``url`` to the media player channel.

        ``start`` must be 0 (see the module docstring) and ``subtitle_url`` is ignored.
        """
        if start:
            raise RokuError("the Roku media player cannot start part way into a video")
        if self._poll:
            self._poll.cancel()
            self._poll = None
        self.ended.clear()
        self.error = None
        params = {"videoName": title or "Castomatic"}
        fmt = _FORMATS.get(self.mime)
        if fmt:
            params["videoFormat"] = fmt
        _LOGGER.debug("ECP input/15985 u=%s %s", url, params)
        await self._call(self._roku.play_on_roku(url, params), "play the video")
        self._poll = asyncio.ensure_future(self._poll_state())

    async def pause(self) -> None:
        """Pause playback (the remote's play/pause key, only when playing)."""
        if await self.state() == STATE_PLAYING:
            await self._call(self._roku.remote("play"), "pause")

    async def resume(self) -> None:
        """Resume playback (the remote's play/pause key, only when paused)."""
        if await self.state() == STATE_PAUSED:
            await self._call(self._roku.remote("play"), "resume")

    async def stop(self) -> None:
        """Leave the media player (the remote's back key)."""
        if self._roku is None:
            return
        try:
            if await self.state() not in (STATE_STOPPED, STATE_NO_MEDIA):
                await self._roku.remote("back")
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.debug("Roku did not acknowledge stop: %s", ex)

    async def wait_until_ended(self) -> None:
        """Block until playback has stopped."""
        await self.ended.wait()

    async def state(self) -> str:
        """The media player's state, as one of the shared STATE_* values."""
        player = await self._player()
        if player.get("@error") == "true":
            self.error = "the Roku media player reported an error"
            return STATE_STOPPED
        return _STATES.get(player.get("@state", "none"), STATE_NO_MEDIA)

    async def position(self) -> Optional[float]:
        """Seconds into the video, or None if the Roku will not say."""
        try:
            return _ms((await self._player()).get("position"))
        except RokuError:
            return None

    # -- internals --------------------------------------------------------

    async def _player(self) -> Dict[str, Any]:
        reply = await self._call(self._roku._request("/query/media-player"), "report its state")
        player = reply.get("player") if isinstance(reply, dict) else None
        return player if isinstance(player, dict) else {}

    async def _call(self, request: Any, what: str) -> Any:
        try:
            return await request
        except Exception as ex:  # rokuecp raises RokuError, RokuConnectionError, ...
            text = str(ex)
            hint = f" {ECP_HINT}" if "403" in text or "Forbidden" in text else ""
            raise RokuError(f"the Roku would not {what}: {text or type(ex).__name__}.{hint}") from ex

    async def _poll_state(self) -> None:
        """Mark playback ended once the player stops after having played."""
        played = False
        failures = 0
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                state = await self.state()
                failures = 0
            except RokuError:
                failures += 1
                if failures >= POLL_FAILURES:
                    self.ended.set()
                    return
                continue
            if self.error:
                self.ended.set()
                return
            played = played or state in (STATE_PLAYING, STATE_PAUSED, STATE_LOADING)
            if played and state in (STATE_STOPPED, STATE_NO_MEDIA):
                self.ended.set()
                return
