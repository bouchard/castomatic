"""Doing the right thing: delivery per TV capability, and which protocol to try first."""

from pathlib import Path

import pytest

from castomatic import planner, stream
from castomatic.backends import chromecast, roku
from castomatic.backends.base import SUBS_NONE, SUBS_SRT, Capabilities
from castomatic.sources import Input, MediaSource
from castomatic.targets import TV, AirPlayInfo, Target

CAST = chromecast.CAPABILITIES
ROKU = roku.CAPABILITIES


def summary(video="h264", pix="yuv420p", audio="aac", duration=60.0):
    return stream.Summary(0, video, pix, 0 if audio else None, 0, audio, duration)


def local(path="/m/a.mkv"):
    return MediaSource("a", [Input(path)], Path(path))


def remote(url="http://h/a.mp4", **kw):
    return MediaSource("a", [Input(url)], **kw)


@pytest.mark.parametrize(
    "caps,mode,source,summ,start,seeks,kind,untouched",
    [
        # Chromecast: MP4 H.264/AAC plays as it is; MKV is repackaged (a remux), not re-encoded
        (CAST, "auto", local("/m/a.mp4"), summary(), 0, True, "file", True),
        (CAST, "auto", local("/m/a.mkv"), summary(), 0, True, "hls", False),
        (CAST, "auto", local("/m/a.mp4"), summary(audio="ac3"), 0, True, "hls", False),
        (CAST, "auto", local("/m/a.webm"), summary(video="vp9", pix="yuv420p", audio="opus"), 0, True, "file", True),
        # Chromecast fetches https:// itself, so a playable address is handed over
        (CAST, "auto", remote("https://h/a.mp4"), summary(), 0, True, "url", True),
        # ... and anything that needs converting live goes as HLS, not MPEG-TS
        (CAST, "auto", remote("https://h/a.mkv"), summary(audio="ac3"), 0, True, "live", False),
        (CAST, "auto", remote("https://y/watch", via_ytdlp=True), summary(), 0, True, "live", False),
        # Roku plays MKV with AC3 untouched
        (ROKU, "auto", local("/m/a.mkv"), summary(audio="ac3"), 0, False, "file", True),
        # ... but cannot be told to start part way, so a conversion begins there
        (ROKU, "auto", local("/m/a.mkv"), summary(audio="ac3"), 30, False, "live", False),
        (ROKU, "direct", local("/m/a.mkv"), summary(), 30, False, "file", True),
        (ROKU, "convert", local("/m/a.mkv"), summary(), 30, False, "live", False),
        (ROKU, "convert", local("/m/a.mkv"), summary(), 0, False, "hls", False),
        (ROKU, "auto", remote("https://h/live.m3u8"), summary(), 0, False, "url", True),
        (ROKU, "auto", remote("https://h/live.m3u8"), summary(), 10, False, "live", False),
        (ROKU, "auto", local("/m/a.avi"), summary(video="mpeg4"), 0, False, "hls", False),
    ],
)  # fmt: skip
def test_choose_delivery_from_capabilities(caps, mode, source, summ, start, seeks, kind, untouched):
    delivery = planner.choose_delivery(mode, source, summ, set(), False, caps, start, seeks)
    assert (delivery.kind, delivery.untouched) == (kind, untouched)


def test_reasons_say_why():
    delivery = planner.choose_delivery("auto", local("/m/a.mkv"), summary(audio="dts"), set(), False, CAST)
    assert delivery.reasons == ["the TV does not list .mkv files", "audio is dts"]
    delivery = planner.choose_delivery("auto", local("/m/a.mkv"), summary(), set(), False, ROKU, 5, False)
    assert delivery.reasons == ["the TV cannot be told to start part way"]


def test_dlna_capabilities_follow_what_the_renderer_lists():
    caps = planner.dlna_capabilities({"video/mp4"}, True)
    assert caps.hls and caps.subtitles == SUBS_SRT and "video/mp4" in caps.sink
    assert "mpeg2video" in caps.live_video  # MPEG-TS carries it


def test_static_capabilities_and_seeking():
    assert planner.static_capabilities("chromecast") is CAST
    assert planner.static_capabilities("dlna") is None
    assert planner.seeks("roku") is False and planner.seeks("dlna") is True


def test_subtitle_note_only_when_the_tv_cannot_take_them():
    assert "burn-subtitles" in planner.subtitle_note(ROKU, True)
    assert planner.subtitle_note(ROKU, False) is None
    assert planner.subtitle_note(CAST, True) is None
    assert ROKU.subtitles == SUBS_NONE


def _tv(**kw):
    return TV("Den", "10.0.0.5", **kw)


def _targets(*protocols):
    tv = _tv()
    return [Target(tv, p) for p in protocols]


async def test_order_prefers_untouched_then_reliability():
    found = _targets("chromecast", "dlna", "roku", "airplay")
    # An MKV with AC3: only Roku plays it as it is, so Roku goes first despite its rank
    ranked = await planner.order_targets(found, "auto", local("/m/a.mkv"), summary(audio="ac3"))
    assert [r.target.protocol for r in ranked] == ["roku", "chromecast", "dlna", "airplay"]
    # An MP4 H.264/AAC plays as it is on both Chromecast and Roku: the more reliable first
    ranked = await planner.order_targets(found, "auto", local("/m/a.mp4"), summary())
    assert [r.target.protocol for r in ranked][:2] == ["chromecast", "roku"]


async def test_order_asks_dlna_what_it_plays():
    found = _targets("chromecast", "dlna")

    async def probe(target):
        return planner.dlna_capabilities({"video/x-mkv"}, False)

    ranked = await planner.order_targets(found, "auto", local("/m/a.mkv"), summary(audio="ac3"), probe_dlna=probe)
    assert [r.target.protocol for r in ranked] == ["dlna", "chromecast"]
    assert ranked[0].delivery.kind == "file"


async def test_order_keeps_dlna_by_rank_when_it_cannot_be_asked():
    async def broken(target):
        raise OSError("unreachable")

    found = _targets("dlna", "roku")
    ranked = await planner.order_targets(found, "auto", local("/m/a.mp4"), summary(), probe_dlna=broken)
    assert ranked[0].target.protocol == "roku" and ranked[1].delivery is None
    ranked = await planner.order_targets(found, "auto", local("/m/a.mp4"), None)  # nothing probed yet
    assert [r.target.protocol for r in ranked] == ["dlna", "roku"]


async def test_describe_lists_each_way_in_order():
    tv = _tv(airplay=AirPlayInfo("id", "10.0.0.5", 7000, object()))
    ranked = await planner.order_targets(
        [Target(tv, "airplay"), Target(tv, "dlna"), Target(tv, "chromecast")], "auto",
        local("/m/a.mkv"), summary(audio="dts"),
    )  # fmt: skip
    lines = planner.describe(ranked)
    assert lines[0].startswith("  1. Chromecast: repackaged as HLS") and "audio is dts" in lines[0]
    assert "decided once connected" in lines[1]
    assert lines[2].startswith("  3. AirPlay")
    assert planner.capabilities_summary({"Roku": ROKU})[0].startswith("  Roku: files:")


async def test_airplay_hls_source_is_untouched():
    ranked = await planner.order_targets(_targets("airplay"), "auto", remote("https://h/x.m3u8"), summary())
    assert ranked[0].delivery.kind == "static"


def test_custom_capabilities_never_check_brands():
    # A made-up receiver that only plays VP9/Opus WebM: decided purely from what it says
    caps = Capabilities(sink=frozenset({"video/webm"}), video=frozenset({"vp9"}), audio=frozenset({"opus"}), hls=False)
    assert planner.choose_delivery("auto", local("/m/a.webm"), summary("vp9", "yuv420p", "opus"), set(), False, caps).kind == "file"
    assert planner.choose_delivery("auto", local("/m/a.mp4"), summary(), set(), False, caps).kind == "stream"
