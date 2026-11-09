"""Command line: ``castomatic SOURCE`` plays a file or URL on your TV.

With no other options it finds the TV (asking once if there are several, then
remembering), sends the file untouched if the TV can play it, converts just in time if not,
and asks yt-dlp for the video when given a web page. A TV reachable in several ways
(Chromecast, DLNA, Roku, AirPlay) is tried the most reliable way first, and the next way
if that one does not start playing.
"""

import argparse
import asyncio
import logging
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional

import aiohttp
from pyatv import exceptions as pyatv_exceptions

from castomatic import (
    banner, buffering, config, dlna, hls, planner, sources, stream, subtitles, targets, ytstream,
)  # fmt: skip
from castomatic.errors import UPNP_HINTS, UserError
from castomatic.airplay import AirPlayError, AirPlayPlayer
from castomatic.backends.base import SUBS_NONE, SUBS_WEBVTT, CastError, Capabilities
from castomatic.backends.chromecast import ChromecastPlayer
from castomatic.backends.roku import RokuPlayer
from castomatic.live import LiveHls
from castomatic.planner import Delivery, choose_delivery  # noqa: F401  (part of this module's API)

HLS_MIME = "application/vnd.apple.mpegurl"
# pyatv has no common base class for its errors; these are the ones a connection can raise
PYATV_ERRORS = (
    pyatv_exceptions.ConnectionFailedError,
    pyatv_exceptions.ConnectionLostError,
    pyatv_exceptions.AuthenticationError,
    pyatv_exceptions.InvalidCredentialsError,
    pyatv_exceptions.OperationTimeoutError,
    pyatv_exceptions.ProtocolError,
    pyatv_exceptions.HttpError,
    pyatv_exceptions.InvalidResponseError,
    pyatv_exceptions.BackOffError,
    pyatv_exceptions.PlaybackError,
)
_LOGGER = logging.getLogger(__name__)

START_WATCH_SECONDS = 25.0
FEED_INTERVAL = 2.0  # seconds between checks of how far ahead the copy is
RESTARTS = 5  # an AirPlay receiver that drops the video is given it again this often
WATCH_INTERVAL = 2.0
EXAMPLES = """\
examples:
  castomatic movie.mkv                     play a file on your TV
  castomatic https://youtu.be/VIDEO        any page yt-dlp (or castor) understands
  castomatic https://host/video.mp4        a video address
  castomatic movie.mkv --start 600         begin 10 minutes in
  castomatic --list                        show the TVs on the network
  castomatic movie.mkv --choose            pick a different TV

commands:
  castomatic doctor                        check everything, and say what to fix
  castomatic explain movie.mkv             show what would happen, without playing
"""


class StartFailed(Exception):
    """Playback did not get going this way; another way may work.

    Only raised when there is another way left to try (``args.failover``); otherwise the
    same situation is reported as advice and playback is left to the TV.
    """

    def __init__(self, reason: str, hint: str = "", fetched: bool = False, untouched: bool = False) -> None:
        """Initialize a new StartFailed."""
        super().__init__(reason)
        self.reason = reason
        self.hint = hint
        self.fetched = fetched  # the TV did ask this computer for the video
        self.untouched = untouched  # the video was being sent as it is

    def as_user_error(self) -> UserError:
        """For when nothing is left to try."""
        return UserError(self.reason, self.hint or None)


def explain_dlna_error(ex: CastError, tv_name: str) -> UserError:
    """Turn a renderer error into advice."""
    hint = UPNP_HINTS.get(ex.code or 0)
    if hint is None:
        hint = (
            f"Is {tv_name} awake? Some TVs show an 'allow' prompt the first time a new "
            "device sends to them: accept it with the remote. Run with -v for details."
        )
    return UserError(f"{tv_name} refused the request: {ex}", hint)


def _lost_contact(tv_name: str, ex: BaseException) -> UserError:
    return UserError(
        f"lost contact with {tv_name} ({type(ex).__name__}).",
        "Check that the TV is still on and on the same network, then run again.",
    )


async def _check_subtitle(sub: Optional[Any], fmt: str, offset: float = 0.0) -> Optional[Any]:
    """Convert the subtitle now, so a bad file is a warning and not a failed playback."""
    if sub is None:
        return None
    try:
        await (sub.webvtt() if fmt == "webvtt" else sub.srt(offset))
    except RuntimeError as ex:
        where = sub.sidecar or "the embedded track"
        print(f"warning: subtitles skipped, could not convert {where}: {ex}")
        return None
    return sub


async def _find_subtitle(args: argparse.Namespace, source: sources.MediaSource) -> Optional[Any]:
    if args.no_subtitles or source.is_hls_playlist:
        return None
    explicit = Path(args.subtitles).expanduser() if args.subtitles else None
    if explicit is None and not source.is_local:
        return None
    try:
        return await subtitles.find(source.path or Path("."), explicit)
    except FileNotFoundError as ex:
        raise UserError(str(ex), "Check the path given to --subtitles.") from ex


async def _subtitle_for(args, source, buffered) -> Optional[Any]:
    """The subtitle to use, leaving out one that would mean reading a slow drive end to end."""
    sub = await _find_subtitle(args, source)
    if not buffered:
        return sub
    usable = buffered.subtitle(sub)
    if sub and not usable:
        print(
            "note: the subtitles inside this video need all of it, so they are left out this "
            "time; they will be there when it is played from the finished copy."
        )
    return usable


async def _ticker(started: float, duration: Optional[float]) -> None:
    while True:
        elapsed = time.monotonic() - started
        total = f" / {duration:.0f}s" if duration else ""
        print(f"\r  {elapsed:7.0f}s{total}", end="", flush=True)
        await asyncio.sleep(1)


FIREWALL_HINT = (
    "a firewall on this computer is the usual cause (on a Mac: System Settings > Network > "
    "Firewall, allow incoming connections for Python), or the TV is showing an 'allow' "
    "prompt for you to accept."
)


async def _watch_start(
    player: Any, server: Any, tv_name: str, failover: bool = False
) -> Optional[StartFailed]:
    """If the TV has not started playing after a while, say why that usually is.

    With ``failover``, nothing is printed: the reason is returned so the caller can try
    another way instead.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + START_WATCH_SECONDS
    while loop.time() < deadline:
        await asyncio.sleep(WATCH_INTERVAL)
        try:
            if await player.state() in ("PLAYING", "PAUSED_PLAYBACK"):
                return None
        except (CastError, aiohttp.ClientError, asyncio.TimeoutError):
            continue
    hits = getattr(server, "requests", None)
    if hits is None:
        failed = StartFailed(
            f"{tv_name} has not started playing yet.",
            "if the TV shows an 'allow' prompt, accept it with the remote.",
        )
    elif hits == 0:
        failed = StartFailed(
            f"{tv_name} accepted the command but never asked this computer for the video.",
            FIREWALL_HINT,
        )
    else:
        failed = StartFailed(
            f"{tv_name} fetched the video but has not started playing it.",
            "it may not support this format. Try `--mode convert`. If the TV shows an "
            "'allow' prompt, accept it with the remote.",
            fetched=True,
        )
    if failover:
        return failed
    print()
    print(f"warning: {failed.reason}\n  hint: {failed.hint}")
    return None


def _check_start(args: argparse.Namespace, duration: Optional[float]) -> None:
    if args.start < 0:
        raise UserError("--start cannot be negative.")
    if duration and args.start >= duration:
        raise UserError(
            f"--start {args.start:.0f}s is past the end of the video ({duration:.0f}s).",
            f"Choose a start under {duration:.0f} seconds.",
        )


async def _play_dlna(
    args: argparse.Namespace,
    source: sources.MediaSource,
    summary: stream.Summary,
    target: targets.Target,
    workdir: Optional[Path],
    buffered: Optional[buffering.BufferedMedia] = None,
    fallback: Optional[buffering.Fallback] = None,
) -> None:
    player = dlna.DlnaPlayer(target.tv.renderer)
    await _play_renderer(args, source, summary, target, workdir, buffered, fallback, player)


async def _play_chromecast(
    args: argparse.Namespace,
    source: sources.MediaSource,
    summary: stream.Summary,
    target: targets.Target,
    workdir: Optional[Path],
    buffered: Optional[buffering.BufferedMedia] = None,
    fallback: Optional[buffering.Fallback] = None,
) -> None:
    player = ChromecastPlayer(target.tv.chromecast)
    await _play_renderer(args, source, summary, target, workdir, buffered, fallback, player)


async def _play_roku(
    args: argparse.Namespace,
    source: sources.MediaSource,
    summary: stream.Summary,
    target: targets.Target,
    workdir: Optional[Path],
    buffered: Optional[buffering.BufferedMedia] = None,
    fallback: Optional[buffering.Fallback] = None,
) -> None:
    player = RokuPlayer(target.tv.roku)
    await _play_renderer(
        args, source, summary, target, workdir, buffered, fallback, player, seeks=False
    )


async def _capabilities(player: Any) -> Capabilities:
    """What the TV plays: asked of the player, or (older players) built from its sink."""
    if hasattr(player, "capabilities"):
        return await player.capabilities()
    sink = await player.sink_types()
    return planner.dlna_capabilities(sink, any("mpegurl" in m for m in sink))


async def _play_renderer(
    args: argparse.Namespace,
    source: sources.MediaSource,
    summary: stream.Summary,
    target: targets.Target,
    workdir: Optional[Path],
    buffered: Optional[buffering.BufferedMedia],
    fallback: Optional[buffering.Fallback],
    player: Any,
    seeks: bool = True,
) -> None:
    """Play on a TV that fetches the video from us over HTTP (DLNA, Chromecast, Roku)."""
    tv = target.tv
    failover = getattr(args, "failover", False)
    try:
        await player.connect()
    except (OSError, CastError) as ex:
        reason = f"cannot reach {tv.name} at {tv.host}: {getattr(ex, 'strerror', None) or ex}."
        hint = "Is the TV on, and on the same network as this computer?"
        if failover:
            raise StartFailed(reason, hint) from ex
        raise UserError(reason, hint) from ex
    server: Any = None
    delivery: Optional[Delivery] = None
    try:
        caps = await _capabilities(player)
        sink = set(caps.sink)
        _LOGGER.info("%s plays: %s", tv.name, caps.describe())
        delivery = choose_delivery(
            args.mode, source, summary, sink, caps.hls, caps, args.start, seeks
        )
        sub = await _subtitle_for(args, source, buffered)
        burn: Optional[Path] = None
        if sub and getattr(args, "burn_subtitles", False):
            burn = await _burn_file(sub, args.start, workdir)
            if burn is not None:
                live = "live" if caps.live == planner.LIVE_HLS else "stream"
                delivery = Delivery(live, delivery.reasons + ["drawing the subtitles onto the picture"])
            sub = None
        if sub and caps.subtitles == SUBS_NONE:
            print(f"note: {planner.subtitle_note(caps, True)}")
            sub = None
        if sub and delivery.kind == "live":
            print("note: subtitles are not sent while converting live; they are left out.")
            sub = None
        duration = summary.duration or source.duration

        async def start_playing(at: float) -> float:
            """Serve the video and start it at ``at`` seconds; returns the renderer's shift."""
            nonlocal server
            reading = source
            if buffered and delivery.kind == "stream":  # ffmpeg reads the copy, not the drive
                reading = replace(source, inputs=[sources.Input(buffered.url)])
            now = argparse.Namespace(**{**vars(args), "start": at})
            url, server, mime, start = await _prepare_dlna(
                now, reading, summary, delivery, sink, sub, player, workdir, buffered, caps, burn
            )
            if buffered:  # a renderer asked to start part way plays the opening first
                await _buffer_gate(buffered, at, duration, opening=start > 0)
            player.mime = mime
            _announce(delivery, server)
            try:
                await player.play(
                    url, title=source.title, start=start,
                    subtitle_url=getattr(server, "subtitle_url", None),
                )  # fmt: skip
            except CastError as ex:
                if failover:
                    advice = explain_dlna_error(ex, tv.name)
                    raise StartFailed(advice.message, advice.hint or "", untouched=delivery.untouched) from ex
                raise
            print(f"Playing on {tv.name}. Press Ctrl-C to stop.")
            return at - start

        shift = await start_playing(args.start)
        watch = None if buffered else fallback
        try:
            reached = await _wait(
                player, server, tv.name, source, summary, args, buffered, shift, watch
            )
        except StartFailed as ex:
            ex.untouched = delivery.untouched
            raise
        if reached is not None:
            buffered = fallback.media
            if delivery.kind == "hls":  # the same segments, from the next one on
                server.use_source(buffered.url)
            else:  # the renderer is tied to the old address: reload it where it was
                await _hold(player)
                await server.stop()
                server = None
                shift = await start_playing(reached)
            await _wait(player, server, tv.name, source, summary, args, buffered, shift)
    except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
        raise _lost_contact(tv.name, ex) from ex
    except CastError as ex:
        raise explain_dlna_error(ex, tv.name) from ex
    finally:
        await player.stop()
        if server:
            await server.stop()
        await player.close()
        if server is not None and getattr(server, "last_error", None):
            print(f"\nffmpeg reported: {server.last_error}")


async def _prepare_dlna(
    args, source, summary, delivery, sink, sub, player, workdir, buffered=None, caps=None, burn=None
):
    """Start whatever serves the media, returning (url, server, mime, start for the TV)."""
    ip = player.local_ip
    kind = delivery.kind
    sub_format = "webvtt" if caps is not None and caps.subtitles == SUBS_WEBVTT else "srt"
    if kind == "url":
        return source.location, None, stream.matching_mime(source.location, sink), args.start
    if kind == "file":
        sub = await _check_subtitle(sub, sub_format)
        server = dlna.DirectFileServer(
            source.path, ip, subtitle=sub, respond=buffered.respond if buffered else None,
            subtitle_format=sub_format,
        )  # fmt: skip
        mime = stream.matching_mime(source.location, sink)
        return await server.start(), server, mime, args.start
    if kind == "static":
        server = hls.StaticHlsServer(source.path, ip)
        return await server.start(), server, HLS_MIME, args.start
    if kind == "hls":
        sub = await _check_subtitle(sub, "webvtt")
        media = await (buffered.plan(sub, _print_progress) if buffered else hls.plan(source.path, sub))
        url, server = await _hls_server(args, media, ip, workdir)
        return url, server, HLS_MIME, media.playable_start(args.start)
    if kind == "live":  # HLS written as ffmpeg goes: what Chromecast and Roku play best
        plan = stream.make_plan(
            source.inputs, summary, force_convert=args.mode == "convert",
            video_ok=set(caps.live_video) if caps else None,
            audio_ok=set(caps.live_audio) if caps else None,
        )  # fmt: skip
        plan.burn = burn
        server = _LiveServer(LiveHls(plan, workdir / "live", start=args.start), ip)
        server.description = plan.description
        return await server.start(), server, HLS_MIME, 0.0  # ffmpeg already skipped ahead
    plan = stream.make_plan(source.inputs, summary, force_convert=args.mode == "convert")
    plan.burn = burn
    sub = await _check_subtitle(sub, "srt", args.start)
    server = stream.StreamServer(plan, ip, start=args.start, subtitle=sub)
    server.description = plan.description  # shown by _announce
    server.mime = next((m for m in stream.TS_MIMES if m in sink), stream.TS_MIMES[0])
    player.flags = stream.STREAM_FLAGS
    return await server.start(), server, server.mime, 0.0  # ffmpeg already skipped ahead


class _LiveServer:
    """A live HLS conversion and the server for it, started and stopped as one."""

    def __init__(self, live: LiveHls, address: str) -> None:
        """Initialize a new _LiveServer."""
        self.live = live
        self._address = address
        self._server: Optional[hls.StaticHlsServer] = None
        self.description = ""

    @property
    def requests(self) -> Optional[int]:
        """What the TV asked for (None before it started)."""
        return self._server.requests if self._server else None

    @property
    def last_error(self) -> Optional[str]:
        """Why ffmpeg stopped, if it failed."""
        return self.live.error if self.live.failed.is_set() else None

    async def start(self) -> str:
        """Start ffmpeg, wait for the first segments, then serve them."""
        print("Preparing the video; playback starts in a moment ...")
        self._server = hls.StaticHlsServer(await self.live.start(), self._address)
        return await self._server.start()

    async def stop(self) -> None:
        """Stop serving and stop ffmpeg."""
        if self._server:
            await self._server.stop()
        await self.live.stop()


async def _burn_file(sub: Any, start: float, workdir: Optional[Path]) -> Optional[Path]:
    """Write the subtitle as SRT, timed for a conversion starting at ``start``, to burn in."""
    if workdir is None:  # run() makes one whenever --burn-subtitles is given
        return None
    if not stream.can_burn_subtitles():
        print(f"warning: subtitles cannot be drawn onto the picture, so they are left out.\n  hint: {stream.BURN_HINT}")
        return None
    try:
        data = await sub.srt(start)
    except RuntimeError as ex:
        print(f"warning: subtitles skipped, could not convert them: {ex}")
        return None
    path = workdir / "burn.srt"
    path.write_bytes(data)
    return path


async def _hls_server(args, media, ip, workdir):
    if args.no_jit:
        name = getattr(media.source, "name", "the video")
        print(f"Converting {name} ({len(media.segments)} segments) ...")
        server = hls.StaticHlsServer(await hls.convert_to_directory(media, workdir), ip)
    else:
        server = hls.JitHlsServer(media, ip)
    return await server.start(), server


def _announce(delivery: Delivery, server: Any) -> None:
    why = "; ".join(delivery.reasons)
    if delivery.kind in ("file", "url"):
        print("Sending the video as is: no conversion needed.")
    elif delivery.kind == "static":
        print("Serving the HLS playlist as is.")
    elif delivery.kind == "hls":
        print(f"Converting to HLS as the TV asks ({why}).")
    elif delivery.kind == "live":
        what = getattr(server, "description", "")
        suffix = f" ({why})" if why else ""
        print(f"Converting live to HLS{suffix}: {what}.")
    else:
        what = getattr(server, "description", "")
        suffix = f" ({why})" if why else ""
        print(f"Converting just in time{suffix}: {what}.")


async def _keep_fed(player, buffered, duration: float, shift: float) -> None:
    """Wait for the end, holding the renderer if it gets close to the end of the copy.

    Renderers react to an HTTP read that goes quiet in their own ways, many by giving up,
    so it is paused while there is still time. ``shift`` is the video time the renderer's
    position 0 is at (a live stream starts where it was asked to).
    """
    cache = buffered.cache
    ended = asyncio.ensure_future(player.wait_until_ended())
    try:
        while not cache.complete:
            done, _ = await asyncio.wait({ended}, timeout=FEED_INTERVAL)
            if done:
                return
            seconds = await player.position()
            offset = cache.position if seconds is None else buffered.offset_at(seconds + shift, duration)
            if buffered.running_dry(offset, duration) and await _hold(player):
                await buffered.fill_ahead(offset, duration, _buffer_status)
                print("\r  Buffered enough to carry on." + " " * 40)
                await _release(player)
        await ended
    finally:
        ended.cancel()


async def _hold(player) -> bool:
    """Pause a renderer that is playing. False if it isn't, or will not pause."""
    try:
        if await player.state() not in ("PLAYING", "TRANSITIONING"):
            return False
        await player.pause()
        return True
    except (CastError, aiohttp.ClientError, asyncio.TimeoutError) as ex:
        _LOGGER.debug("renderer would not pause: %s", ex)
        return False


async def _release(player) -> None:
    try:
        await player.resume()
    except (CastError, aiohttp.ClientError, asyncio.TimeoutError) as ex:
        _LOGGER.debug("renderer would not resume: %s", ex)


async def _watch_direct(player, fallback, start: float, shift: float) -> Optional[float]:
    """Wait for the end of a video playing directly from its file.

    If the file stops keeping up, opens the copy and returns the time reached instead.
    """
    began = time.monotonic()
    reached = {"last": None, "moving": False}

    async def stalled() -> bool:
        """Whether a renderer that says it is playing has stopped getting anywhere."""
        try:
            state = await player.state()
        except (CastError, aiohttp.ClientError, asyncio.TimeoutError):
            return False
        seconds = await player.position()
        last, reached["last"] = reached["last"], seconds
        if seconds is None or last is None or state not in ("PLAYING", "TRANSITIONING"):
            return False
        if seconds == last:
            return reached["moving"]
        reached["moving"] = True
        return False

    ended = asyncio.ensure_future(player.wait_until_ended())
    slowed = asyncio.ensure_future(fallback.slowed(stalled))
    try:
        await asyncio.wait({ended, slowed}, return_when=asyncio.FIRST_COMPLETED)
        if ended.done():
            return None
        slowed.result()
        if await fallback.open() is None:
            await ended
            return None
        seconds = await player.position()
        if seconds is None:  # the renderer won't say: as far as the clock has gone
            return start + time.monotonic() - began
        return seconds + shift
    finally:
        ended.cancel()
        slowed.cancel()


async def _wait(
    player, server, tv_name, source, summary, args, buffered=None, shift=0.0, watch=None
) -> Optional[float]:
    """Wait for the end. With ``watch``, returns the time reached if a switch is needed."""
    duration = summary.duration or source.duration
    failover = getattr(args, "failover", False)
    ticker = asyncio.ensure_future(_ticker(time.monotonic(), duration))
    watcher = asyncio.ensure_future(_watch_start(player, server, tv_name, failover))
    try:
        if failover:
            failed = await _first_verdict(player, watcher, tv_name)
            if failed:
                raise failed
        if buffered:  # holding for the drive can take as long as it takes
            await _keep_fed(player, buffered, duration, shift)
            return None
        if watch:
            return await _watch_direct(player, watch, args.start, shift)
        limit = (duration - args.start + 30) if duration else None
        await asyncio.wait_for(player.wait_until_ended(), limit)
    except asyncio.TimeoutError:
        pass
    finally:
        ticker.cancel()
        watcher.cancel()
        print()
    return None


async def _first_verdict(player: Any, watcher: "asyncio.Future[Any]", tv_name: str) -> Optional[StartFailed]:
    """Wait until playback has either got going or clearly failed to."""
    ended = asyncio.ensure_future(player.wait_until_ended())
    try:
        await asyncio.wait({watcher, ended}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        ended.cancel()
    error = getattr(player, "error", None)
    if not watcher.done() and error:  # the TV gave up without playing
        return StartFailed(f"{tv_name} could not play it: {error}.", fetched=True)
    return watcher.result() if watcher.done() else None


async def _swap_when_slow(fallback: buffering.Fallback, server: Any) -> None:
    """Move HLS segments onto a copy if the file stops keeping up. Nothing to interrupt."""
    await fallback.slowed()
    media = await fallback.open()
    if media:
        server.use_source(media.url)


async def _play_airplay(
    args: argparse.Namespace,
    source: sources.MediaSource,
    summary: stream.Summary,
    target: targets.Target,
    workdir: Optional[Path],
    buffered: Optional[buffering.BufferedMedia] = None,
    fallback: Optional[buffering.Fallback] = None,
) -> None:
    tv = target.tv
    info = tv.airplay
    player = AirPlayPlayer(info.address, info.port, info.credentials)
    server: Any = None
    live: Optional[LiveHls] = None
    swap: Optional["asyncio.Future[None]"] = None
    try:
        try:
            await player.connect()
        except AirPlayError:
            raise
        except (*PYATV_ERRORS, OSError, asyncio.TimeoutError) as ex:
            if getattr(args, "failover", False):
                raise StartFailed(f"could not connect to {tv.name} over AirPlay: {ex}") from ex
            raise UserError(
                f"could not connect to {tv.name} over AirPlay: {ex}",
                "The TV's AirPlay port changes between sessions, so try again. If it keeps "
                "failing, the pairing may have been removed: pair again with "
                f"`atvremote --id {info.identifier} --protocol airplay pair`.",
            ) from ex
        start = args.start
        if source.is_hls_playlist and not source.is_local and not source.via_ytdlp:
            url = source.location
        elif source.is_hls_playlist and source.is_local:
            server = hls.StaticHlsServer(source.path, player.local_ip)
            url = await server.start()
        elif not source.is_local:
            # Remote video: ffmpeg writes HLS segments as it downloads, and playback starts
            # as soon as the first ones exist. (--download-first fetches everything first.)
            plan = stream.make_plan(source.inputs, summary, force_convert=args.mode == "convert")
            print(f"Preparing the video ({plan.description}); playback starts in a moment ...")
            live = LiveHls(plan, workdir / "live", start=args.start)
            server = hls.StaticHlsServer(await live.start(), player.local_ip)
            url = await server.start()
            start = 0.0  # ffmpeg already skipped ahead
            if args.subtitles:
                print("note: subtitles are not sent while streaming live; add --download-first.")
        else:
            sub = await _subtitle_for(args, source, buffered)
            sub = await _check_subtitle(sub, "webvtt")
            media = await (
                buffered.plan(sub, _print_progress) if buffered else hls.plan(source.path, sub)
            )
            url, server = await _hls_server(args, media, player.local_ip, workdir)
            start = media.playable_start(start)
            if buffered:
                await _buffer_gate(buffered, args.start, summary.duration or source.duration)
            elif fallback:
                swap = asyncio.ensure_future(_swap_when_slow(fallback, server))
            if sub:
                print("Subtitles are offered as a track: choose them in the TV's own subtitle menu.")
        await player.play(url, title=source.title, start=start)
        print(f"Playing on {tv.name} over AirPlay. Press Ctrl-C to stop.")
        duration = summary.duration or source.duration
        for _ in range(RESTARTS):
            await _wait_airplay(player, duration, args, live, buffered or fallback)
            if not player.failed:
                break
            media = buffered
            if fallback and not buffered:  # dropping the video is proof enough: use a copy
                if swap:
                    swap.cancel()
                media = fallback.media or await fallback.open()
                if media and server:
                    server.use_source(media.url)
            at = await _restart_point(server, media, duration)
            # The session the receiver gave up in will not take the video again
            await player.stop()
            await player.close()
            await player.connect()
            await player.play(url, title=source.title, start=at)
        else:
            raise UserError(
                f"{tv.name} kept stopping the video.",
                "The video arrives too slowly for it. Play it again: what was copied so far "
                "is kept, so the next try starts with more of it already here.",
            )
    except AirPlayError as ex:
        raise UserError(
            f"{tv.name} refused the AirPlay request: {ex}",
            "Stop whatever the TV is playing and try again. Run with -v for details.",
        ) from ex
    except (*PYATV_ERRORS, OSError, asyncio.TimeoutError) as ex:
        raise _lost_contact(tv.name, ex) from ex
    finally:
        if swap:
            swap.cancel()
        await player.stop()
        if live:
            await live.stop()
        if server:
            await server.stop()
        await player.close()


async def _restart_point(server: Any, media: Optional[buffering.BufferedMedia], duration) -> float:
    """After the receiver dropped the video: where to start it again, once that can last."""
    at = getattr(server, "reached", None)
    if at is None or media is None:  # nothing would be any different the second time
        raise UserError(
            "the TV stopped the video part way.",
            "It may have been waiting too long for the next part. Try again.",
        )
    at = server.media.playable_start(at)
    print(f"\nnote: the TV gave up waiting for the video; starting again at {at:.0f}s.")
    await media.wait_ready(at, duration, _buffer_status)
    print("\r  Buffered enough to carry on." + " " * 40)
    return at


async def _wait_airplay(
    player: AirPlayPlayer,
    duration: Optional[float],
    args,
    live: Optional[LiveHls] = None,
    buffered: Any = None,
) -> None:
    ticker = asyncio.ensure_future(_ticker(time.monotonic(), duration))
    waiting = [asyncio.ensure_future(player.wait_until_ended())]
    if live:
        waiting.append(asyncio.ensure_future(live.failed.wait()))
    try:
        # Holding for the drive can take as long as it takes
        limit = (duration - args.start + 30) if duration and not buffered else None
        await asyncio.wait(waiting, timeout=limit, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in waiting:
            task.cancel()
        ticker.cancel()
        print()
    if live and live.failed.is_set():
        raise UserError(
            f"the video stopped downloading part way: {live.error}",
            "The connection may have dropped, or the link expired. Try again, or use "
            "`--download-first` to fetch it completely before playing.",
        )


def _print_progress(done: int, total: Optional[int], speed: Optional[float]) -> None:
    size = sources.format_size
    percent = f"{100 * done / total:5.1f}%  " if total else ""
    of = f" / {size(total)}" if total else ""
    rate = f"  {size(speed)}/s" if speed else ""
    print(f"\r  Downloading  {percent}{size(done)}{of}{rate}   ", end="", flush=True)


def _relay_status(have: float, want: float, speed: float) -> None:
    size = sources.format_size
    print(f"\r  Buffering  {have:.0f}s of {want:.0f}s of video, {size(speed)}/s   ", end="", flush=True)


async def _start_relay(args: argparse.Namespace, source: sources.MediaSource, directory: Path) -> tuple:
    """Stream a yt-dlp video into a local buffer and wait until playing it cannot run dry.

    Returns the source as the program will now read it (our own buffer, never the site),
    what is in it, and the two things to stop afterwards.
    """
    kind = ytstream.YtdlpBuffer if source.via_ytdlp else ytstream.RemoteBuffer
    buffer = kind(source, directory)
    feed = ytstream.BufferServer(buffer, "127.0.0.1")
    await buffer.start()
    try:
        what = "live stream" if source.is_live else "video"
        tool = " with yt-dlp" if source.via_ytdlp else ""
        print(f"Buffering the {what}{tool} before playing ...")
        await buffer.wait_ready(args.start, _relay_status)
        print("\r  Buffered enough to play without stopping; the rest keeps arriving." + " " * 20)
        info = await stream.probe_input(sources.Input(str(buffer.path)))
        summary = replace(stream.summarize([info]), duration=source.duration)  # the buffer is only a part
        url = await feed.start()
    except BaseException:
        await feed.stop()
        await buffer.stop()
        raise
    return replace(source, inputs=[sources.Input(url)], relay=buffer), summary, (buffer, feed)


def _buffer_status(have: int, need: int, rate: Optional[float]) -> None:
    size = sources.format_size
    speed = f", {size(rate)}/s" if rate else ""
    eta = ""
    if rate and need > have:
        minutes = (need - have) / rate / 60
        eta = f", about {minutes:.0f} min to go" if minutes >= 1.5 else ", under 2 min to go"
    print(f"\r  Buffering  {size(have)} of {size(need)} needed{speed}{eta}   ", end="", flush=True)


async def _buffer_gate(
    buffered: buffering.BufferedMedia, start: float, duration: float, opening: bool = False
) -> None:
    """Hold playback back until the local copy is far enough ahead that it cannot stall."""
    if buffered.cache.complete:
        return
    await buffered.wait_ready(start, duration, _buffer_status, opening=opening)
    print("\r  Buffered enough to play without stopping; the rest keeps copying." + " " * 20)


async def _finish_copy(buffered: buffering.BufferedMedia) -> None:
    """After playing, fetch whatever was skipped, so next time needs nothing from the drive."""
    if buffered.cache.complete:
        return
    print("Finishing the local copy (Ctrl-C leaves the rest for next time) ...")
    await buffered.finish(_print_progress)
    print()


def _copy_report(buffered: buffering.BufferedMedia) -> None:
    c = buffered.cache
    if c.complete:
        print(f"The local copy is complete: {c.path}")
    else:
        print(
            f"The local copy is {100 * c.have_bytes / c.size:.0f}% complete; playing the same "
            "file again carries on from there."
        )


async def _download_first(
    args: argparse.Namespace, source: sources.MediaSource, directory: Path
) -> tuple:
    """Fetch a remote video completely, then return it as a local source and its summary."""
    about = f" (about {sources.format_size(source.size_estimate)})" if source.size_estimate else ""
    print(f"Downloading {source.title!r}{about} before playing ...")
    local = await sources.download(
        source, directory, _print_progress, want_subtitles=not args.no_subtitles,
        verbose=args.verbose, on_note=lambda text: print(f"\nnote: {text}"),
    )  # fmt: skip
    print("\n  Done.")
    info = await stream.probe_input(local.inputs[0])
    return local, stream.summarize([info])


async def _list(args: argparse.Namespace) -> None:
    print("Looking for TVs ...")
    tvs = await targets.list_tvs(args.credentials)
    saved = config.saved_tv()
    if not tvs:
        raise targets.no_tv_error([], None)
    print(f"Found {len(tvs)} TV{'s' if len(tvs) != 1 else ''}:")
    for tv in tvs:
        mark = "  (your TV)" if saved and targets.matches(tv, saved["host"]) else ""
        print(f"  {tv.name:<28} {tv.host:<15} {', '.join(tv.protocols)}{mark}")


async def run(args: argparse.Namespace) -> None:
    """Do what the command line asked."""
    if args.forget:
        print("Forgot your TV." if config.forget_tv() else "No TV was remembered.")
        if not args.source and not args.list:
            return
    if args.list:
        await _list(args)
        return
    if not args.source:
        raise UserError("nothing to play.", "Give a video file or URL: `castomatic movie.mkv`.")
    if config.is_first_run() and sys.stdout.isatty() and not getattr(args, "quiet", False):
        banner.welcome()
    stream.ensure_ffmpeg()

    source = await sources.resolve(
        args.source, args.ytdlp_format, args.cookies_from_browser, args.verbose
    )
    if source.via_ytdlp:
        print(f"yt-dlp found: {source.title}" + (" (live)" if source.is_live else ""))
    if source.via_castor:
        print(f"castor found the video on {source.page_url}")
    if args.engine == "castor":
        await _cast_with_castor(args, source)
        return
    # A yt-dlp video is streamed by yt-dlp itself into a local buffer and re-streamed from
    # there; ffmpeg never opens the site's address. --download-first fetches it all instead.
    relay = source.via_ytdlp and not args.download_first
    if relay and source.is_live and args.start:
        raise UserError("--start cannot be used with a live stream.", "A live stream plays from now: leave --start out.")
    summary: Optional[stream.Summary] = None
    must_download = False
    try:
        if not relay:
            infos = [await stream.probe_input(i) for i in source.inputs]
            summary = stream.summarize(infos)
            if await _too_slow_to_stream(args, source, infos):
                relay = True
    except stream.ProbeError as ex:
        if source.via_castor:  # castor's stream only plays from inside castor
            print(f"note: the stream castor found cannot be read directly ({ex.reason}); letting castor play it.")
            await _cast_with_castor(args, source)
            return
        if not source.via_ytdlp:
            raise
        # Sites often refuse ffmpeg's direct requests but let yt-dlp's downloader through
        print(f"note: ffmpeg cannot read this video straight from the site ({ex.reason}).")
        must_download = True
    _check_start(args, (summary.duration if summary else None) or source.duration)

    found = await targets.resolve_targets(args.tv, args.protocol, args.choose, args.credentials)
    ranked = await planner.order_targets(
        found, args.mode, source, summary, args.start,
        probe_dlna=_probe_dlna if len(found) > 1 else None,
    )  # fmt: skip
    if len(ranked) > 1:
        _LOGGER.info("ways to reach the TV, in order:\n%s", "\n".join(planner.describe(ranked)))
    attempts = [r.target for r in ranked]
    target = attempts[0]
    print(f"{source.title}  ->  {target.tv.name} ({target.protocol.upper()})")

    # A temporary folder is needed to convert to disk (--no-jit), for a remote video sent
    # as growing HLS segments (AirPlay, Chromecast, Roku), or for a full download
    remote_for_airplay = (
        target.protocol == "airplay" and not source.is_local
        and not (source.is_hls_playlist and not source.via_ytdlp)
    )
    live_hls = getattr(args, "burn_subtitles", False) or any(
        _may_convert_live(t.protocol, source, args) for t in attempts
    )
    download = must_download or (args.download_first and (remote_for_airplay or source.via_ytdlp))
    workdir = None
    buffered: Optional[buffering.BufferedMedia] = None
    fallback: Optional[buffering.Fallback] = None
    feed: Optional[tuple] = None
    if args.workdir and not Path(args.workdir).is_dir():
        raise UserError(f"--workdir {args.workdir} is not a folder.", "Create it, or leave --workdir out.")
    if args.no_jit or remote_for_airplay or download or relay or live_hls:
        workdir = tempfile.TemporaryDirectory(prefix="castomatic-", dir=args.workdir)
    try:
        path = Path(workdir.name) if workdir else None
        if relay:
            source, summary, feed = await _start_relay(args, source, path)
        if source.is_local and not source.is_hls_playlist and not download:
            duration = summary.duration or source.duration
            note = lambda text: print(f"note: {text}")  # noqa: E731
            buffered = await buffering.choose(source.path, duration, on_note=note)
            if not buffered and duration and not args.no_jit:
                fallback = buffering.Fallback(source.path, duration, on_note=note)
        if download:
            source, summary = await _download_first(args, source, path)
            _check_start(args, summary.duration or source.duration)
        await _play_with_failover(args, attempts, source, summary, path, buffered, fallback)
        if buffered or (fallback and fallback.media):
            await _finish_copy(buffered or fallback.media)
    finally:
        if feed:
            await feed[1].stop()
            await feed[0].stop()
        media = buffered or (fallback.media if fallback else None)
        if media:
            await media.close()
            _copy_report(media)
        if workdir:
            try:
                workdir.cleanup()
            except OSError:  # an abandoned download thread may still be writing into it
                pass


REMOTE_MARGIN = 1.5  # a remote video must download this much faster than it plays


async def _too_slow_to_stream(args: argparse.Namespace, source: sources.MediaSource, infos: List[dict]) -> bool:
    """Whether a remote media address downloads too slowly to play without stalling.

    If so it is buffered here first (a wait at the start instead of pauses part way),
    exactly like a yt-dlp video. Only plain media addresses are checked: yt-dlp videos are
    always buffered, and an HLS playlist is the TV's own business.
    """
    if source.is_local or source.via_ytdlp or source.is_hls_playlist or args.download_first:
        return False
    if len(source.inputs) != 1 or source.is_live:
        return False
    fmt = infos[0].get("format", {}) if infos else {}
    try:
        bitrate = float(fmt.get("bit_rate") or 0) / 8  # bytes per second
    except (TypeError, ValueError):
        bitrate = 0.0
    if not bitrate:
        return False
    rate = await sources.measure_url_rate(source.inputs[0])
    if rate is None:
        return False
    size = sources.format_size
    _LOGGER.info("remote video: downloads at %s/s, plays at %s/s", size(rate), size(bitrate))
    if rate >= REMOTE_MARGIN * bitrate:
        return False
    print(
        f"note: this address downloads at {size(rate)}/s and the video plays at "
        f"{size(bitrate)}/s, so it is buffered here first: playback may take a moment to "
        "start, but will not stop part way."
    )
    return True


async def _cast_with_castor(args: argparse.Namespace, source: sources.MediaSource) -> None:
    """Hand the whole job to castor: the TV is found here, castor does the rest."""
    from castomatic.engines import castor

    if castor.find() is None:
        raise UserError("--engine castor needs castor, which is not installed.", castor.INSTALL_HINT)
    found = await targets.resolve_targets(args.tv, args.protocol, args.choose, args.credentials)
    usable = [t for t in found if t.protocol in castor.DEVICE_TYPES]
    if not usable:
        raise UserError(
            f"castor cannot reach {found[0].tv.name}: it plays to DLNA, Chromecast and Roku, "
            f"and this TV offers {', '.join(found[0].tv.protocols)}.",
            "Leave out --engine castor to play it with castomatic.",
        )
    target = usable[0]
    what = source.page_url or source.location
    print(f"{source.title}  ->  {target.tv.name} ({target.protocol.upper()}, via castor)")
    await castor.cast(what, target.protocol, target.tv.host, target.tv.name, args.start, args.verbose)


def _may_convert_live(protocol: str, source: sources.MediaSource, args: argparse.Namespace) -> bool:
    """Whether playing over ``protocol`` could mean a live HLS conversion on disk."""
    if protocol not in ("chromecast", "roku"):
        return False
    if protocol == "roku" and args.start > 0:
        return True  # the Roku cannot start part way, so a conversion does it
    return not source.is_local or source.via_ytdlp


def _player_for(protocol: str) -> Callable[..., Awaitable[None]]:
    """The function that plays over ``protocol`` (looked up now, so tests can replace it)."""
    return {
        "dlna": _play_dlna, "airplay": _play_airplay,
        "chromecast": _play_chromecast, "roku": _play_roku,
    }[protocol]  # fmt: skip


async def _probe_dlna(target: targets.Target) -> Capabilities:
    """Ask a DLNA renderer what it plays, to decide which protocol to try first."""
    player = dlna.DlnaPlayer(target.tv.renderer)
    await player.connect()
    try:
        return await player.capabilities()
    finally:
        await player.close()


async def _play_with_failover(args, attempts, source, summary, workdir, buffered, fallback) -> None:
    """Play over the first way that works.

    If the TV fetched the video untouched but never started it, the same way is tried
    again converting (unless --mode said otherwise). After that, the next protocol.
    """
    queue = [(target, args.mode) for target in attempts]
    while queue:
        target, mode = queue.pop(0)
        retry_converting = mode == "auto"
        now = argparse.Namespace(
            **{**vars(args), "mode": mode, "failover": bool(queue) or retry_converting}
        )
        try:
            await _player_for(target.protocol)(
                now, source, summary, target, workdir, buffered, fallback
            )
            return
        except StartFailed as ex:
            if retry_converting and ex.untouched and ex.fetched:
                queue.insert(0, (target, "convert"))
                print(f"\nnote: {ex.reason}\n  Trying again, converting the video for it ...")
                continue
            if not queue:
                raise ex.as_user_error() from ex
            nxt = queue[0][0]
            print(
                f"\nnote: {ex.reason}\n  Trying {nxt.tv.name} over "
                f"{targets.LABELS[nxt.protocol]} instead ..."
            )


async def explain(args: argparse.Namespace) -> None:
    """``castomatic explain SOURCE``: everything ``castomatic SOURCE`` would decide, and why.

    Nothing is played and nothing is remembered.
    """
    if not args.source:
        raise UserError("nothing to explain.", "Give a video: `castomatic explain movie.mkv`.")
    stream.ensure_ffmpeg()
    source = await sources.resolve(args.source, args.ytdlp_format, args.cookies_from_browser, args.verbose)
    found_by = "castor" if source.via_castor else "yt-dlp" if source.via_ytdlp else None
    print(f"Source:    {source.title}" + (f"  (found by {found_by})" if found_by else ""))
    print(f"Where:     {'this computer' if source.is_local else stream.describe(source.inputs[0])}")
    summary = None
    infos: List[dict] = []
    try:
        infos = [await stream.probe_input(i) for i in source.inputs]
        summary = stream.summarize(infos)
    except stream.ProbeError as ex:
        print(f"Streams:   cannot be read directly ({ex.reason}); it would be buffered through yt-dlp")
    if summary:
        audio = summary.audio_codec or "none"
        length = f"{summary.duration:.0f}s" if summary.duration else "unknown length"
        print(f"Streams:   video {summary.video_codec} ({summary.pix_fmt or '?'}), audio {audio}, {length}")
    await _explain_speed(source, summary, infos)

    try:
        found = await targets.resolve_targets(
            args.tv, args.protocol, args.choose, args.credentials, interactive=sys.stdin.isatty()
        )
    except UserError as ex:
        print(f"TV:        none ({ex.message})")
        print("           Showing what would happen on each kind of TV instead.")
        found = _every_kind_of_tv(args.protocol)
        probe = None
    else:
        tv = found[0].tv
        print(f"TV:        {tv.name} ({tv.host}), reachable over {', '.join(tv.protocols)}")
        probe = _probe_dlna
    ranked = await planner.order_targets(
        found, args.mode, source, summary, args.start, probe_dlna=probe
    )
    print("Plan:      tried in this order, moving on if one does not start:")
    for line in planner.describe(ranked):
        print(line)
    if args.mode == "auto" and any(r.delivery and r.delivery.untouched for r in ranked):
        print("           (a TV that fetches the untouched video but cannot play it is sent a converted copy)")
    caps = {
        targets.LABELS[r.target.protocol]: planner.static_capabilities(r.target.protocol)
        for r in ranked if planner.static_capabilities(r.target.protocol)
    }  # fmt: skip
    if caps:
        print("What each protocol plays as it is:")
        for line in planner.capabilities_summary(caps):
            print(line)


def _every_kind_of_tv(protocol: str) -> List[targets.Target]:
    """A pretend TV offering every protocol (paired), for explaining with no TV around."""
    from castomatic.backends.chromecast import ChromecastInfo
    from castomatic.backends.roku import RokuInfo

    tv = targets.TV(
        "a TV", "0.0.0.0",
        dlna.Renderer("http://0.0.0.0/", "a TV", "", "http://0.0.0.0/"),
        targets.AirPlayInfo("", "0.0.0.0", 7000, object(), "a TV"),  # type: ignore[arg-type]
        ChromecastInfo("a TV", "0.0.0.0"), RokuInfo("a TV", "0.0.0.0"),
    )  # fmt: skip
    return targets.candidate_targets(tv, protocol)


async def _explain_speed(source: sources.MediaSource, summary: Optional[stream.Summary], infos: List[dict]) -> None:
    """Say whether the video arrives fast enough to play straight away."""
    duration = (summary.duration if summary else None) or source.duration
    if source.via_ytdlp:
        print("Buffering: yes: web videos are buffered here first, until playing cannot run dry")
        return
    if source.is_local and source.path and duration:
        rate = await buffering.measure_rate(source.path)
        bitrate = source.path.stat().st_size / duration
        network = " (network share)" if buffering.is_network_path(source.path) else ""
        factor = buffering.FAST_FACTOR * (buffering.NETWORK_FACTOR if network else 1.0)
        size = sources.format_size
        if rate is None:
            print(f"Buffering: no{network}: reading speed could not be measured")
        elif rate >= factor * bitrate:
            print(f"Buffering: not needed{network}: reads at {size(rate)}/s, plays at {size(bitrate)}/s")
        else:
            print(f"Buffering: yes{network}: reads at {size(rate)}/s, plays at {size(bitrate)}/s, so a local copy leads the way")
        return
    if not source.is_local and not source.is_hls_playlist and infos:
        try:
            bitrate = float(infos[0].get("format", {}).get("bit_rate") or 0) / 8
        except (TypeError, ValueError):
            bitrate = 0.0
        rate = await sources.measure_url_rate(source.inputs[0]) if bitrate else None
        if rate is None:
            print("Buffering: unknown: the download speed could not be measured")
        elif rate >= REMOTE_MARGIN * bitrate:
            print(f"Buffering: not needed: downloads at {sources.format_size(rate)}/s, plays at {sources.format_size(bitrate)}/s")
        else:
            print(f"Buffering: yes: downloads at {sources.format_size(rate)}/s, plays at {sources.format_size(bitrate)}/s")


def build_parser() -> argparse.ArgumentParser:
    """The command line."""
    parser = argparse.ArgumentParser(
        prog="castomatic",
        description="Cast anything anywhere: play a video file, URL or web page on any TV "
        "(Chromecast, DLNA, Roku or AirPlay 2).",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", nargs="?", help="a video file, a media URL, or a web page URL")
    tv = parser.add_argument_group("choosing the TV")
    tv.add_argument(
        "--tv", "--host", "--id", "--location", dest="tv", metavar="TV",
        help="the TV's name (or part of it), IP address or AirPlay id. By default the TV is "
        "found automatically, asked about once if there are several, and remembered",
    )  # fmt: skip
    tv.add_argument(
        "--choose", action="store_true",
        help="ask which TV to use, even if one is remembered",
    )  # fmt: skip
    tv.add_argument("--forget", action="store_true", help="forget the remembered TV")
    tv.add_argument(
        "--list", action="store_true", help="list the TVs found on the network and exit"
    )  # fmt: skip
    tv.add_argument(
        "--protocol", choices=["auto", *targets.PROTOCOLS], default="auto",
        help="auto (default) tries the way that plays the video as it is first, then the "
        "most reliable (Chromecast, DLNA, Roku, AirPlay), moving on if one fails",
    )  # fmt: skip
    play = parser.add_argument_group("playback")
    play.add_argument("--start", type=float, default=0.0, metavar="SECONDS", help="start position")
    play.add_argument(
        "--mode", choices=["auto", "direct", "convert"], default="auto",
        help="auto (default) sends the file untouched when the TV can play it and converts "
        "just in time otherwise; direct never converts; convert always converts",
    )  # fmt: skip
    play.add_argument(
        "--subtitles", metavar="FILE",
        help="subtitle file (.srt/.vtt); by default one next to the video or inside it is used",
    )  # fmt: skip
    play.add_argument("--no-subtitles", action="store_true", help="send no subtitles")
    play.add_argument(
        "--burn-subtitles", action="store_true",
        help="draw the subtitles onto the picture (re-encodes the video); for TVs that take "
        "no subtitle file, such as Roku's media player",
    )  # fmt: skip
    play.add_argument(
        "--download-first", action="store_true",
        help="AirPlay with a web or remote video: download all of it, with progress, before "
        "playing (gives a seek bar and subtitles), instead of streaming it live",
    )  # fmt: skip
    web = parser.add_argument_group("web pages (yt-dlp)")
    web.add_argument(
        "--ytdlp-format", default=sources.DEFAULT_YTDLP_FORMAT, metavar="FORMAT",
        help="yt-dlp format selector (default: up to 1080p, H.264 + AAC)",
    )  # fmt: skip
    web.add_argument(
        "--cookies-from-browser", metavar="BROWSER",
        help="use this browser's login cookies, e.g. chrome or firefox",
    )  # fmt: skip
    adv = parser.add_argument_group("advanced")
    adv.add_argument(
        "--engine", choices=["castomatic", "castor"], default="castomatic",
        help="castor (if installed) hands the whole cast to castor instead; castomatic "
        "already asks castor for pages yt-dlp cannot read",
    )  # fmt: skip
    adv.add_argument(
        "--no-jit", action="store_true", help="HLS only: convert everything to disk before playing"
    )  # fmt: skip
    adv.add_argument("--workdir", metavar="DIR", help="where --no-jit writes (default: temporary)")
    adv.add_argument("--credentials", help="AirPlay credentials, overriding the stored ones")
    adv.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="-v shows each decision and why; -vv also everything sent to and from the TV",
    )  # fmt: skip
    adv.add_argument("--log-file", metavar="FILE", help="also write the full (-vv) log to FILE")
    adv.add_argument("-q", "--quiet", action="store_true", help="no welcome or banner")
    return parser


def setup_logging(verbose: int, log_file: Optional[str] = None) -> None:
    """-v: castomatic's decisions (INFO). -vv: everything, protocol traffic included (DEBUG)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose >= 2 else logging.INFO if verbose == 1 else logging.WARNING)
    fmt = "%(asctime)s %(name)s: %(message)s" if verbose >= 2 else "%(name)s: %(message)s"
    console.setFormatter(logging.Formatter(fmt, "%H:%M:%S"))
    root.addHandler(console)
    level = console.level
    if log_file:
        try:
            to_file = logging.FileHandler(log_file)
        except OSError as ex:
            raise UserError(f"cannot write the log to {log_file}: {ex.strerror or ex}.") from ex
        to_file.setLevel(logging.DEBUG)
        to_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(to_file)
        level = logging.DEBUG
    root.setLevel(level)
    if verbose < 2:  # libraries' own chatter only at -vv
        for noisy in ("pychromecast", "zeroconf", "pyatv", "aiohttp", "asyncio", "rokuecp"):
            logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point. Returns the exit status."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["play"]:  # the old ``castomatic play FILE`` form still works
        argv = argv[1:]
    command = run
    if argv[:1] == ["doctor"]:
        from castomatic import doctor

        args = doctor.build_parser().parse_args(argv[1:])
        command = doctor.run
    else:
        if argv[:1] == ["explain"]:
            argv, command = argv[1:], explain
        if argv in ([], ["-h"], ["--help"]) and sys.stdout.isatty():
            banner.show()
        args = build_parser().parse_args(argv)
    try:
        setup_logging(args.verbose, getattr(args, "log_file", None))
        status = asyncio.run(command(args))
        return status if isinstance(status, int) else 0
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except UserError as ex:
        print(ex.render(), file=sys.stderr)
        return 1
    except Exception as ex:  # pylint: disable=broad-except
        print(
            f"error: something unexpected went wrong: {type(ex).__name__}: {ex}\n"
            "  hint: run again with -v for details; if it keeps happening, please report it "
            "at https://github.com/bouchard/castomatic/issues",
            file=sys.stderr,
        )
        if args.verbose:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
