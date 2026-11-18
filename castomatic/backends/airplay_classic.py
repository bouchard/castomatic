"""Classic AirPlay video: hand the receiver a URL, as the first AirPlay senders did.

For receivers that say (in their AirPlay service record) that no pairing is needed: receiver
apps on Android and Fire TV, and Apple TVs with pairing switched off. Everything is plain
HTTP on the AirPlay port, tied together by an ``X-Apple-Session-ID`` header:

* ``POST /play`` with a binary plist naming the URL (``Content-Location``);
* ``GET /playback-info`` for ``readyToPlay``, ``rate``, ``position`` and ``duration``;
* ``POST /rate?value=0|1`` to pause and resume, ``POST /scrub?position=S`` to seek;
* ``POST /stop``.

What is learned from receivers seen so far, and handled without singling any out:

* a start position in the ``/play`` body may be ignored, so the start is a seek once the
  receiver says it is ready;
* ``/playback-info`` may hang when nothing is playing, so every request has a timeout;
* the end is ``rate`` 0 with ``position`` at ``duration``; the report stays like that after
  a stop.

The protocol has no subtitle file, so subtitles go inside the video
(``ignores_subtitle_links`` is always true) or into the HLS playlist.
"""

import asyncio
import logging
import plistlib
import uuid
from typing import Any, Dict, Optional

import aiohttp

from castomatic.backends.base import (
    LIVE_HLS,
    STATE_LOADING,
    STATE_NO_MEDIA,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_STOPPED,
    SUBS_SRT,
    CastError,
    Capabilities,
)
from castomatic.backends.roku import _local_ip_for

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 5.0
POLL_INTERVAL = 2.0
POLL_FAILURES = 5
READY_WAIT = 20.0  # how long the receiver has to say it is ready before a start is skipped to
END_SLACK = 1.0  # seconds from the end that count as the end
# What Apple specifies for AirPlay video: MP4 and QuickTime files and HLS, with H.264 or
# HEVC and AAC, AC-3 or E-AC-3 audio. Nothing beyond that is assumed for any receiver.
CAPABILITIES = Capabilities(
    sink=frozenset({"video/mp4", "video/quicktime", "video/x-m4v"}),
    video=frozenset({"h264", "hevc"}),
    audio=frozenset({"aac", "ac3", "eac3", "mp3"}),
    hls=True,
    live=LIVE_HLS,
    subtitles=SUBS_SRT,  # put inside the video, see the module docstring
    https=True,
    live_video=frozenset({"h264", "hevc"}),
    live_audio=frozenset({"aac"}),
)


class ClassicAirPlayError(CastError):
    """The receiver refused a request or could not be reached."""


class ClassicAirPlayPlayer:
    """Plays a URL on a classic AirPlay receiver. Same interface as :class:`DlnaPlayer`."""

    def __init__(self, address: str, port: int, name: str = "") -> None:
        """Initialize a new ClassicAirPlayPlayer."""
        self.address = address
        self.port = port
        self.name = name or address
        self.mime = "video/mp4"
        self.flags = ""  # DLNA only
        self.ended = asyncio.Event()
        self.error: Optional[str] = None
        self.local_ip = ""
        self._session = str(uuid.uuid4()).upper()
        self._http: Optional[aiohttp.ClientSession] = None
        self._poll: Optional["asyncio.Task[None]"] = None
        self._played = False  # seen playing since the last play(): an end is only an end after that

    @property
    def _base(self) -> str:
        return f"http://{self.address}:{self.port}"

    async def connect(self) -> None:
        """Open the HTTP session and work out which local address reaches the receiver."""
        self.local_ip = _local_ip_for(self.address)
        self._http = aiohttp.ClientSession(
            headers={"User-Agent": "MediaControl/1.0", "X-Apple-Session-ID": self._session},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        )

    async def close(self) -> None:
        """Stop polling and close the session."""
        if self._poll:
            self._poll.cancel()
            self._poll = None
        if self._http:
            await self._http.close()
            self._http = None

    async def capabilities(self) -> Capabilities:
        """What AirPlay video receivers play."""
        return CAPABILITIES

    def ignores_subtitle_links(self) -> bool:
        """Always: classic AirPlay has no way to send a subtitle file."""
        return True

    def remember_ignores_subtitle_links(self) -> None:
        """Nothing to remember: it is true of every classic AirPlay receiver."""

    async def play(
        self, url: str, title: str = "", start: float = 0.0, subtitle_url: Optional[str] = None
    ) -> None:
        """Hand ``url`` to the receiver and start it at ``start`` seconds."""
        if self._poll:
            self._poll.cancel()
            self._poll = None
        self.ended.clear()
        self.error = None
        self._played = False
        body = plistlib.dumps(
            {"Content-Location": url, "Start-Position-Seconds": float(start)}, fmt=plistlib.FMT_BINARY
        )
        _LOGGER.debug("AirPlay POST /play %s at %.1fs", url, start)
        await self._request(
            "POST", "/play", "play the video", data=body,
            headers={"Content-Type": "application/x-apple-binary-plist"},
        )  # fmt: skip
        if start > 0:
            await self._skip_to(start)
        self._poll = asyncio.ensure_future(self._poll_state())

    async def pause(self) -> None:
        """Pause playback."""
        await self._request("POST", "/rate?value=0", "pause")

    async def resume(self) -> None:
        """Resume playback."""
        await self._request("POST", "/rate?value=1", "resume")

    async def stop(self) -> None:
        """Stop playback (never raises: the receiver may already be gone)."""
        if self._http is None:
            return
        try:
            await self._request("POST", "/stop", "stop")
        except ClassicAirPlayError as ex:
            _LOGGER.debug("AirPlay receiver did not acknowledge stop: %s", ex)

    async def wait_until_ended(self) -> None:
        """Block until playback has stopped."""
        await self.ended.wait()

    async def state(self) -> str:
        """The receiver's state, as one of the shared STATE_* values."""
        return _state(await self._info())

    async def position(self) -> Optional[float]:
        """Seconds into the video, or None if the receiver will not say."""
        try:
            info = await self._info()
        except ClassicAirPlayError:
            return None
        value = info.get("position")
        return float(value) if isinstance(value, (int, float)) else None

    # -- internals --------------------------------------------------------

    async def _skip_to(self, start: float) -> None:
        """Seek to ``start`` once the receiver is ready, unless it already began there."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + READY_WAIT
        while loop.time() < deadline:
            try:
                info = await self._info()
            except ClassicAirPlayError:
                info = {}
            if info.get("readyToPlay") and info.get("duration"):
                if abs(float(info.get("position") or 0.0) - start) > 2.0:
                    await self._request("POST", f"/scrub?position={start:.3f}", "seek")
                return
            await asyncio.sleep(0.5)
        _LOGGER.debug("AirPlay receiver never said it was ready; not seeking to %.1fs", start)

    async def _info(self) -> Dict[str, Any]:
        raw = await self._request("GET", "/playback-info", "report its state")
        try:
            info = plistlib.loads(raw) if raw else {}
        except Exception as ex:  # pylint: disable=broad-except
            raise ClassicAirPlayError(f"the AirPlay receiver sent an unreadable state: {ex}") from ex
        info = info if isinstance(info, dict) else {}
        self._played = self._played or _state(info) in (STATE_PLAYING, STATE_PAUSED)
        return info

    async def _request(self, method: str, path: str, what: str, **kwargs: Any) -> bytes:
        if self._http is None:
            raise ClassicAirPlayError("not connected to the AirPlay receiver")
        try:
            async with self._http.request(method, self._base + path, **kwargs) as resp:
                body = await resp.read()
                if resp.status >= 400:
                    raise ClassicAirPlayError(f"the AirPlay receiver would not {what}: HTTP {resp.status}")
                return body
        except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
            raise ClassicAirPlayError(
                f"the AirPlay receiver would not {what}: {type(ex).__name__} {ex}".strip()
            ) from ex

    async def _poll_state(self) -> None:
        """Mark playback ended at the end of the video, or when the receiver stops answering."""
        failures = 0
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                info = await self._info()
                failures = 0
            except ClassicAirPlayError as ex:
                failures += 1
                _LOGGER.debug("AirPlay state not read (%d): %s", failures, ex)
                if failures >= POLL_FAILURES and self._played:
                    self.ended.set()
                    return
                continue
            state = _state(info)
            if self._played and state in (STATE_STOPPED, STATE_NO_MEDIA):
                _LOGGER.debug("AirPlay receiver %s after playing: %s", state, info)
                self.ended.set()
                return


def _state(info: Dict[str, Any]) -> str:
    """The shared state for a ``/playback-info`` report."""
    if not info or "readyToPlay" not in info:
        return STATE_NO_MEDIA
    if not info.get("readyToPlay"):
        return STATE_LOADING
    duration = float(info.get("duration") or 0.0)
    position = float(info.get("position") or 0.0)
    rate = float(info.get("rate") or 0.0)
    if duration and position >= duration - END_SLACK and rate == 0:
        return STATE_STOPPED  # at the end
    return STATE_PLAYING if rate > 0 else STATE_PAUSED
