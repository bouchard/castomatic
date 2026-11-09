"""Chromecast (Google Cast), built on catt and the pychromecast it uses.

Finding devices uses pychromecast's mDNS browser directly. That only reads each device's
announcement and returns as soon as the timeout passes, whereas catt's own ``discover()``
also connects to each device in turn. Playing uses catt: :class:`catt.api.CattDevice`
connects by address and starts the Default Media Receiver, and its controller loads the
media. catt and pychromecast are synchronous, so every call runs in a worker thread.

The TV fetches media from this computer over HTTP, so the same local servers as DLNA are
used (file as it is, just-in-time HLS, live HLS). catt's own file server is not used.
"""

import asyncio
import logging
import socket
from dataclasses import dataclass
from typing import Any, List, Optional

from castomatic.backends.base import (
    LIVE_HLS,
    STATE_LOADING,
    STATE_NO_MEDIA,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_STOPPED,
    SUBS_WEBVTT,
    CastError,
    Capabilities,
)

_LOGGER = logging.getLogger(__name__)

PORT = 8009
POLL_INTERVAL = 2.0
POLL_FAILURES = 5
PLAY_WAIT = 15.0
# What every Cast receiver's Default Media Receiver plays, per Google's published list of
# supported media. Nothing narrower is assumed for any particular device.
CAPABILITIES = Capabilities(
    sink=frozenset({"video/mp4", "video/webm"}),
    video=frozenset({"h264", "vp8", "vp9"}),
    audio=frozenset({"aac", "mp3", "opus", "vorbis", "flac"}),
    hls=True,
    live=LIVE_HLS,
    subtitles=SUBS_WEBVTT,
    https=True,
    live_video=frozenset({"h264"}),
    live_audio=frozenset({"aac", "mp3"}),
)
# Cast "audio" devices and speaker groups have no screen
VIDEO_CAST_TYPES = ("cast",)
_STATES = {
    "PLAYING": STATE_PLAYING,
    "PAUSED": STATE_PAUSED,
    "BUFFERING": STATE_LOADING,
    "LOADING": STATE_LOADING,
    "IDLE": STATE_STOPPED,
}


class ChromecastError(CastError):
    """A Cast device refused a request or could not be reached."""


@dataclass
class ChromecastInfo:
    """A Cast device found on the network."""

    name: str
    host: str
    port: int = PORT
    uuid: str = ""
    model: str = ""  # shown to the user only; never used to decide anything
    cast_type: str = "cast"


def _discover_sync(timeout: float) -> List[ChromecastInfo]:
    import pychromecast  # imported here: it starts zeroconf machinery on import

    infos, browser = pychromecast.discovery.discover_chromecasts(timeout=timeout)
    try:
        return [
            ChromecastInfo(
                info.friendly_name or info.host, info.host, info.port or PORT,
                str(info.uuid or ""), info.model_name or "", info.cast_type or "cast",
            )
            for info in infos
        ]  # fmt: skip
    finally:
        browser.stop_discovery()


async def discover(timeout: float = 5.0) -> List[ChromecastInfo]:
    """Every Cast device with a screen that answers within ``timeout`` seconds."""
    found = await asyncio.to_thread(_discover_sync, timeout)
    return [c for c in found if c.cast_type in VIDEO_CAST_TYPES]


def _local_ip_for(host: str) -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((host, 9))  # no packet is sent
        return probe.getsockname()[0]
    finally:
        probe.close()


class ChromecastPlayer:
    """Plays a URL on a Cast device's Default Media Receiver.

    Same interface as :class:`castomatic.dlna.DlnaPlayer`.
    """

    def __init__(self, info: ChromecastInfo, device_factory: Any = None) -> None:
        """Initialize a new ChromecastPlayer.

        ``device_factory(ip_addr=...)`` makes the catt device; tests pass a fake.
        """
        self.info = info
        self.mime = "video/mp4"
        self.flags = ""  # DLNA only; kept so callers can treat every player alike
        self.ended = asyncio.Event()
        self.error: Optional[str] = None  # why the receiver stopped, if it said
        self.local_ip = ""
        self._factory = device_factory
        self._device: Any = None
        self._poll: Optional["asyncio.Task[None]"] = None

    # -- connection -------------------------------------------------------

    async def connect(self) -> None:
        """Connect to the device and work out which local address reaches it."""
        self.local_ip = _local_ip_for(self.info.host)
        factory = self._factory
        if factory is None:
            from catt.api import CattDevice

            factory = CattDevice

        def open_device() -> Any:
            device = factory(ip_addr=self.info.host, lazy=True)
            device.controller  # connects, and raises if the device does not answer
            return device

        try:
            self._device = await asyncio.to_thread(open_device)
        except Exception as ex:  # catt raises its own CastError, pychromecast several
            raise ChromecastError(f"could not connect to {self.info.name}: {ex}") from ex

    async def close(self) -> None:
        """Stop polling and disconnect."""
        if self._poll:
            self._poll.cancel()
            self._poll = None
        cast = self._cast
        if cast is not None:
            try:
                await asyncio.to_thread(cast.disconnect)
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.debug("disconnect failed: %s", ex)
        self._device = None

    async def capabilities(self) -> Capabilities:
        """What the Default Media Receiver plays."""
        return CAPABILITIES

    # -- playback ---------------------------------------------------------

    async def play(
        self,
        url: str,
        title: str = "",
        start: float = 0.0,
        subtitle_url: Optional[str] = None,
    ) -> None:
        """Load ``url`` and start playing, from ``start`` seconds."""
        if self._poll:
            self._poll.cancel()
            self._poll = None
        self.ended.clear()
        self.error = None
        controller = self._device.controller

        def load() -> None:
            controller.prep_app()
            controller.play_media_url(
                url, content_type=self.mime, title=title or None, subtitles=subtitle_url,
                current_time=start or None, stream_type="BUFFERED",
            )  # fmt: skip

        _LOGGER.debug("cast LOAD %s (%s) at %.1fs subtitles=%s", url, self.mime, start, subtitle_url)
        try:
            await asyncio.to_thread(load)
        except Exception as ex:  # pylint: disable=broad-except
            raise ChromecastError(f"{self.info.name} would not load the video: {ex}") from ex
        if subtitle_url:
            try:  # the track is loaded; turn it on
                await asyncio.to_thread(controller.enable_subtitle, 1)
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.debug("could not turn subtitles on: %s", ex)
        self._poll = asyncio.ensure_future(self._poll_state())

    async def pause(self) -> None:
        """Pause playback."""
        await self._media_call("pause")

    async def resume(self) -> None:
        """Resume playback."""
        await self._media_call("play")

    async def stop(self) -> None:
        """Stop playback, leaving the TV on the Cast home screen."""
        if self._device is None:
            return
        try:
            await self._media_call("stop")
        except ChromecastError:
            _LOGGER.debug("receiver did not acknowledge stop")

    async def wait_until_ended(self) -> None:
        """Block until playback has stopped."""
        await self.ended.wait()

    async def state(self) -> str:
        """The playback state, as one of the shared STATE_* values."""
        status = self._status()
        if status is None:
            return STATE_NO_MEDIA
        return _STATES.get(getattr(status, "player_state", "") or "", STATE_NO_MEDIA)

    async def position(self) -> Optional[float]:
        """Seconds into the video, or None if the receiver has not said."""
        status = self._status()
        seconds = getattr(status, "adjusted_current_time", None) if status else None
        return float(seconds) if seconds is not None else None

    # -- internals --------------------------------------------------------

    @property
    def _cast(self) -> Any:
        return getattr(self._device, "_cast", None) if self._device is not None else None

    def _status(self) -> Any:
        cast = self._cast
        controller = getattr(cast, "media_controller", None) if cast is not None else None
        return getattr(controller, "status", None) if controller is not None else None

    async def _media_call(self, name: str) -> None:
        cast = self._cast
        if cast is None:
            raise ChromecastError("not connected")
        try:
            await asyncio.to_thread(getattr(cast.media_controller, name))
        except Exception as ex:  # pylint: disable=broad-except
            raise ChromecastError(f"{name} failed: {ex}") from ex

    async def _poll_state(self) -> None:
        """Mark playback ended once the receiver goes idle after having played."""
        played = False
        loop = asyncio.get_running_loop()
        began = loop.time()
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            status = self._status()
            state = await self.state()
            played = played or state in (STATE_PLAYING, STATE_PAUSED, STATE_LOADING)
            reason = getattr(status, "idle_reason", None) if status else None
            if state == STATE_STOPPED and reason == "ERROR":
                self.error = "the receiver reported a playback error"
                self.ended.set()
                return
            if played and state in (STATE_STOPPED, STATE_NO_MEDIA):
                self.ended.set()
                return
            if state == STATE_STOPPED and reason in ("FINISHED", "CANCELLED", "INTERRUPTED"):
                self.ended.set()  # it played and finished between two looks
                return
            if not played and loop.time() - began > 10 * PLAY_WAIT:
                self.ended.set()  # never started: stop waiting on it
                return
