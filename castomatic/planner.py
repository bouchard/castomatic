"""Doing the right thing: which protocol, and how to get the video to the TV.

Two decisions, both made from what the TV plays (:class:`Capabilities`) and what is in the
video (:class:`stream.Summary`), never from a brand or model:

1. :func:`order_targets`: when a TV can be reached in several ways, try first the one
   that plays this video *as it is*, then the more reliable protocol
   (:data:`targets.PROTOCOL_ORDER`). Converting is the last resort, not the default.
2. :func:`choose_delivery`: for one protocol, the least invasive way to send the video,
   cheapest first:

   ``url``     hand the TV the address itself (plain HTTP, already playable)
   ``file``    serve the file untouched, with seeking (Range requests)
   ``static``  serve an existing HLS playlist as it is
   ``hls``     just-in-time HLS: segments made as the TV asks, streams copied when they
               can be, so usually a remux and not a transcode
   ``stream``  one live MPEG-TS conversion, copying what the TV takes (DLNA)
   ``live``    a live HLS conversion, copying what the TV takes (Chromecast, Roku)
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from castomatic import sources, stream
from castomatic.backends import chromecast as cast_backend
from castomatic.backends import roku as roku_backend
from castomatic.backends.base import LIVE_HLS, SUBS_NONE, Capabilities  # noqa: F401 (LIVE_HLS re-exported)

_LOGGER = logging.getLogger(__name__)

# Deliveries that send the video exactly as it is
UNTOUCHED = ("url", "file", "static")
LABELS = {
    "url": "the TV fetches the address itself",
    "file": "the file is sent untouched",
    "static": "the HLS playlist is sent as it is",
    "hls": "repackaged as HLS segment by segment, as the TV asks",
    "stream": "converted live into one MPEG-TS stream",
    "live": "converted live into HLS",
}


@dataclass
class Delivery:
    """How the source will reach the TV."""

    kind: str  # one of the module docstring's kinds
    reasons: List[str]  # why conversion is needed, if it is
    notes: List[str] = field(default_factory=list)  # anything else worth saying

    @property
    def untouched(self) -> bool:
        """Whether the video goes to the TV exactly as it is."""
        return self.kind in UNTOUCHED


def dlna_capabilities(sink: Set[str], supports_hls: bool) -> Capabilities:
    """The capabilities a DLNA renderer has, given what it listed."""
    return Capabilities(
        sink=frozenset(sink),
        video=frozenset(stream.DIRECT_VIDEO),
        audio=frozenset(stream.AUDIO_COPY),
        hls=supports_hls,
        live_video=frozenset(stream.VIDEO_COPY),
        live_audio=frozenset(stream.AUDIO_COPY),
    )


def choose_delivery(
    mode: str,
    source: sources.MediaSource,
    summary: stream.Summary,
    sink: Set[str],
    supports_hls: bool,
    caps: Optional[Capabilities] = None,
    start: float = 0.0,
    seeks: bool = True,
) -> Delivery:
    """Pick the least invasive way to get ``source`` to the TV.

    ``sink`` and ``supports_hls`` describe a DLNA renderer; pass ``caps`` for any other
    kind of TV (``sink``/``supports_hls`` are then ignored). ``seeks`` False means the
    TV cannot be told where to start, so a ``start`` past zero needs a conversion that
    itself begins there.
    """
    caps = caps or dlna_capabilities(sink, supports_hls)
    delivery = _choose(mode, source, summary, caps, start, seeks)
    if delivery.kind == "stream" and caps.live == LIVE_HLS:
        delivery.kind = "live"
    return delivery


def _choose(
    mode: str,
    source: sources.MediaSource,
    summary: stream.Summary,
    caps: Capabilities,
    start: float,
    seeks: bool,
) -> Delivery:
    sink = set(caps.sink)
    supports_hls = caps.hls
    reasons = stream.conversion_reasons(
        summary, source.location, sink, set(caps.video), set(caps.audio)
    )
    must_begin_there = start > 0 and not seeks
    if source.relay is not None:
        # yt-dlp's MPEG-TS, buffered here: sent as it is when the TV takes its codecs,
        # otherwise converted by ffmpeg reading our buffer (never the site)
        reasons = [r for r in reasons if not r.startswith("the TV does not list")]
        if mode == "convert":
            reasons = reasons or ["--mode convert"]
        # Always through ffmpeg (copying whatever the TV takes): yt-dlp's raw MPEG-TS played
        # with audio and video noticeably out of sync on a TV in testing, a clean remux does
        # not. It also lets ffmpeg skip ahead for --start, which the buffer cannot.
        return Delivery("stream", reasons)
    if source.via_ytdlp or len(source.inputs) > 1:
        # Resolved addresses are signed, need headers or come as separate video and audio
        return Delivery("stream", [])
    if source.is_hls_playlist:
        if supports_hls and not must_begin_there:
            return Delivery("static" if source.is_local else "url", [])
        why = "the TV cannot start part way" if supports_hls else "the TV does not play HLS playlists"
        return Delivery("stream", [why])
    if mode == "convert":
        if must_begin_there:
            return Delivery("stream", ["--mode convert"])
        return Delivery("hls" if source.is_local and supports_hls else "stream", ["--mode convert"])
    if must_begin_there and mode != "direct":
        # Copying what the TV takes, so this is usually a quick remux, not a re-encode
        return Delivery("stream", reasons + ["the TV cannot be told to start part way"])
    if source.is_local:
        if mode == "direct" or not reasons:
            return Delivery("file", [])
        return Delivery("hls" if supports_hls else "stream", reasons)
    secure = source.location.startswith("https://")
    if mode == "direct" or (not reasons and (not secure or caps.https)):
        return Delivery("url", [])
    return Delivery("stream", reasons or ["the TV cannot fetch https:// addresses itself"])


# -- which protocol first ----------------------------------------------------------------


def static_capabilities(protocol: str) -> Optional[Capabilities]:
    """What a protocol's receivers all play, for protocols where that is fixed."""
    return {"chromecast": cast_backend.CAPABILITIES, "roku": roku_backend.CAPABILITIES}.get(protocol)


def seeks(protocol: str) -> bool:
    """Whether the protocol can start a video part way in."""
    return not (protocol == "roku" and not roku_backend.SEEKS)


@dataclass
class Ranked:
    """One way to reach the TV, with what playing this video over it would involve."""

    target: Any  # targets.Target
    delivery: Optional[Delivery]  # None when it cannot be known before connecting
    rank: int

    @property
    def score(self) -> tuple:
        """Lower is better: plays as it is first, then the more reliable protocol."""
        untouched = self.delivery is not None and self.delivery.untouched
        return (0 if untouched else 1, self.rank)


async def order_targets(
    candidates: List[Any],
    mode: str,
    source: sources.MediaSource,
    summary: Optional[stream.Summary],
    start: float = 0.0,
    probe_dlna: Optional[Callable[[Any], Awaitable[Capabilities]]] = None,
) -> List[Ranked]:
    """Put the ways of reaching the TV in the order to try them.

    ``probe_dlna(target)`` asks a DLNA renderer what it plays (one quick request);
    without it, or if it fails, DLNA keeps its place by reliability alone.
    """
    from castomatic.targets import PROTOCOL_ORDER  # targets imports the backends too

    ranked = []
    for target in candidates:
        caps = static_capabilities(target.protocol)
        if caps is None and target.protocol == "dlna" and probe_dlna and summary:
            try:
                caps = await probe_dlna(target)
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.debug("could not ask %s what it plays: %s", target.tv.name, ex)
        delivery = None
        if caps is not None and summary is not None:
            delivery = choose_delivery(
                mode, source, summary, set(), False, caps, start, seeks(target.protocol)
            )
        elif target.protocol == "airplay" and summary is not None:
            delivery = Delivery("static" if source.is_hls_playlist else "hls", [])
        ranked.append(Ranked(target, delivery, PROTOCOL_ORDER.get(target.protocol, 99)))
    ranked.sort(key=lambda r: r.score)
    return ranked


def subtitle_note(caps: Capabilities, have_subtitle: bool) -> Optional[str]:
    """What the user should know about subtitles on this TV, if anything."""
    if have_subtitle and caps.subtitles == SUBS_NONE:
        return (
            "this TV's media player takes no separate subtitle file, so the subtitles are "
            "left out. Add --burn-subtitles to draw them onto the picture instead."
        )
    return None


def describe(ranked: List[Ranked]) -> List[str]:
    """Lines for ``castomatic explain``: each way to reach the TV, in order, and why."""
    from castomatic.targets import LABELS as PROTOCOL_LABELS

    lines = []
    for number, item in enumerate(ranked, 1):
        name = PROTOCOL_LABELS.get(item.target.protocol, item.target.protocol)
        if item.delivery is None:
            how = "decided once connected (the TV is asked what it plays)"
        else:
            how = LABELS[item.delivery.kind]
            if item.delivery.reasons:
                how += f" ({'; '.join(item.delivery.reasons)})"
        lines.append(f"  {number}. {name}: {how}")
    return lines


def capabilities_summary(caps: Dict[str, Capabilities]) -> List[str]:
    """Lines describing what each protocol's receiver plays."""
    return [f"  {name}: {c.describe()}" for name, c in caps.items()]
