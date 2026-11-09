"""What every way of reaching a TV has in common.

A *player* is the object that tells a TV what to play. DLNA, Chromecast and Roku players all
make the TV fetch the media over HTTP from this computer, so they share one interface (the
one :class:`castomatic.dlna.DlnaPlayer` has always had)::

    local_ip: str                 # this computer's address on the TV's network (after connect)
    ended: asyncio.Event          # set once playback has finished or the TV gave up
    async connect() / close()
    async play(url, title="", start=0.0, subtitle_url=None)
    async pause() / resume() / stop() / wait_until_ended()
    async state() -> str          # one of the STATE_* values below
    async position() -> float | None
    async capabilities() -> Capabilities
    mime: str; flags: str         # how the next play() announces the media

AirPlay is different (it only takes HLS) and keeps its own player.

:class:`Capabilities` describes what a TV plays *without conversion*. It is the single input
to "send it as it is, remux it, or transcode it", so nothing anywhere branches on a brand
or model: a TV is described by what it says it plays (DLNA), or by what its protocol
promises every receiver plays (Chromecast's Default Media Receiver, Roku's media player).
"""

from dataclasses import dataclass, field
from typing import FrozenSet, Optional

# Transport states, in UPnP AVTransport's vocabulary, which every player reports in
STATE_PLAYING = "PLAYING"
STATE_PAUSED = "PAUSED_PLAYBACK"
STATE_LOADING = "TRANSITIONING"
STATE_STOPPED = "STOPPED"
STATE_NO_MEDIA = "NO_MEDIA_PRESENT"
ENDED_STATES = (STATE_STOPPED, STATE_NO_MEDIA)

# What a live conversion is sent as. MPEG-TS over one long HTTP response suits DLNA
# renderers; Chromecast and Roku players are built around HLS and play it most reliably.
LIVE_MPEGTS = "mpegts"
LIVE_HLS = "hls"

# How a TV takes subtitles
SUBS_SRT = "srt"  # a separate .srt file it fetches (DLNA)
SUBS_WEBVTT = "webvtt"  # a separate .vtt file it fetches (Chromecast)
SUBS_NONE = "none"  # no way to send them separately (Roku's media player)


class CastError(Exception):
    """A TV refused a request, or could not be reached.

    ``code`` is the protocol's own error code when it sent one (UPnP error codes for DLNA).
    """

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        """Initialize a new CastError."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Capabilities:
    """What a TV plays as it is, and how it prefers to be sent anything else."""

    # Media types it plays from a plain HTTP file, spelled as DLNA renderers list them.
    # Empty means "it did not say", and a cautious default is assumed.
    sink: FrozenSet[str] = field(default_factory=frozenset)
    # Codecs (ffprobe names) it decodes inside those files
    video: FrozenSet[str] = field(default_factory=frozenset)
    audio: FrozenSet[str] = field(default_factory=frozenset)
    hls: bool = False  # plays an HLS playlist given its address
    live: str = LIVE_MPEGTS  # LIVE_MPEGTS or LIVE_HLS
    subtitles: str = SUBS_SRT
    https: bool = False  # can fetch an https:// address itself
    # Codecs that may be copied into a live conversion without re-encoding them
    live_video: FrozenSet[str] = field(default_factory=frozenset)
    live_audio: FrozenSet[str] = field(default_factory=frozenset)

    @property
    def supports_hls(self) -> bool:
        """Alias kept for the DLNA-era callers."""
        return self.hls

    def describe(self) -> str:
        """One line for ``explain`` and ``-v``."""
        sink = ", ".join(sorted(self.sink)) or "not stated (assuming MP4, MKV, MPEG-TS)"
        return (
            f"files: {sink}; video: {', '.join(sorted(self.video))}; "
            f"audio: {', '.join(sorted(self.audio))}; HLS: {'yes' if self.hls else 'no'}; "
            f"subtitles: {self.subtitles}"
        )


def allow_any_origin(app) -> None:
    """Add ``Access-Control-Allow-Origin: *`` to every response ``app`` sends.

    Web-based receivers (Chromecast's media player is a web page) fetch playlists, segments
    and subtitles with script, which a browser only allows across origins with this header.
    Other TVs ignore it.
    """

    async def add_header(request, response) -> None:
        response.headers.setdefault("Access-Control-Allow-Origin", "*")

    app.on_response_prepare.append(add_header)
