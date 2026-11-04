"""Just-in-time HLS: serve a media file as HLS without writing any intermediate files.

A VOD playlist is built up front from the source's keyframes. Each ``.ts`` segment is
produced by ffmpeg only when the receiver asks for it. H.264 video and AAC audio are
copied untouched (remuxed); anything else is transcoded, and only that stream.
"""

import asyncio
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

from aiohttp import web

_LOGGER = logging.getLogger(__name__)

PLAYLIST_NAME = "index.m3u8"
MASTER_NAME = "master.m3u8"
SUBTITLE_PLAYLIST_NAME = "subs.m3u8"
SUBTITLE_NAME = "subs.vtt"
SEGMENT_TARGET_SECONDS = 6.0
CACHE_SEGMENTS = 6
MAX_FFMPEG = 2
BOUNDARY_NUDGE = 0.5  # seconds: how far into a segment a start that falls on its edge goes


@dataclass
class Segment:
    """One planned HLS segment."""

    start: float
    duration: float
    # ffmpeg ``-t`` value: stops just before the next keyframe's *decode* time, so
    # B-frame reordering cannot make neighbouring segments overlap
    cut: float


@dataclass
class MediaPlan:
    """What ffmpeg will do for this source, plus the planned segments."""

    source: Union[Path, str]  # a file, or the address of one (a local cache server)
    duration: float
    copy_video: bool
    copy_audio: bool
    has_audio: bool
    audio_index: int
    bandwidth: int = 8_000_000
    resolution: Optional[str] = None
    codecs: Optional[str] = None  # only known when the streams are copied as they are
    subtitle: Optional[Any] = None  # a pyatv_cast.subtitles.Subtitle
    # Added to every segment's timestamps. The first video packet has a negative decode
    # time, which the muxer would otherwise shift for segment 0 only.
    ts_shift: float = 0.0
    segments: List[Segment] = field(default_factory=list)

    def playable_start(self, seconds: float) -> float:
        """``seconds``, moved off the very start of a segment.

        Asked to start exactly where a segment begins, a receiver (Samsung over AirPlay)
        can fetch that segment over and over without ever playing it.
        """
        for seg in self.segments:
            if seconds > 0 and abs(seconds - seg.start) < BOUNDARY_NUDGE:
                return seg.start + min(BOUNDARY_NUDGE, seg.duration / 2)
        return seconds

    @property
    def entry_name(self) -> str:
        """The playlist the receiver should be given."""
        return MASTER_NAME if self.subtitle else PLAYLIST_NAME

    def master(self) -> str:
        """Render the master playlist that adds the subtitle track."""
        sub = self.subtitle
        language = f'LANGUAGE="{sub.language}",' if sub.language else ""
        attrs = [f"BANDWIDTH={self.bandwidth}"]
        if self.resolution:
            attrs.append(f"RESOLUTION={self.resolution}")
        if self.codecs:
            attrs.append(f'CODECS="{self.codecs}"')
        attrs.append('SUBTITLES="subs"')
        return (
            "#EXTM3U\n#EXT-X-VERSION:3\n"
            f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="{sub.name}",{language}'
            f'DEFAULT=YES,AUTOSELECT=YES,FORCED=NO,URI="{SUBTITLE_PLAYLIST_NAME}"\n'
            f"#EXT-X-STREAM-INF:{','.join(attrs)}\n{PLAYLIST_NAME}\n"
        )

    def subtitle_playlist(self) -> str:
        """Render the playlist holding the whole subtitle as one WebVTT segment."""
        return (
            "#EXTM3U\n#EXT-X-VERSION:3\n"
            f"#EXT-X-TARGETDURATION:{int(self.duration + 0.999)}\n"
            "#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PLAYLIST-TYPE:VOD\n"
            f"#EXTINF:{self.duration:.3f},\n{SUBTITLE_NAME}\n#EXT-X-ENDLIST\n"
        )

    def webvtt(self, vtt: bytes) -> bytes:
        """Tell the player how WebVTT time zero maps onto the segments' timestamps.

        Our segments start at ``ts_shift`` seconds rather than 0, and without this
        header the cues would run that much early.
        """
        header, _, rest = vtt.partition(b"\n")
        stamp = f"X-TIMESTAMP-MAP=MPEGTS:{round(self.ts_shift * 90000)},LOCAL:00:00:00.000"
        return header + b"\n" + stamp.encode() + b"\n" + rest

    def playlist(self) -> str:
        """Render the VOD media playlist."""
        target = int(max(s.duration for s in self.segments) + 0.999)
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{target}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
        ]
        for i, seg in enumerate(self.segments):
            lines += [f"#EXTINF:{seg.duration:.6f},", f"seg{i:05d}.ts"]
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"


async def _run(*args: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:  # nobody wants the result: stop it reading the drive
        proc.kill()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {err.decode(errors='replace')[-500:]}")
    return out


async def _run_with_input(data: bytes, *args: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    out, err = await proc.communicate(data)
    if proc.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {err.decode(errors='replace')[-500:]}")
    return out


_H264_PROFILES = (("baseline", 0x42), ("main", 0x4D), ("high", 0x64))


def h264_codec_string(profile: Optional[str], level: Optional[int]) -> str:
    """Return an RFC 6381 ``avc1`` string, e.g. ``avc1.640028``.

    ffprobe names profiles like "Constrained Baseline", so match on a keyword.
    """
    name = (profile or "high").lower()
    profile_idc = next((idc for key, idc in _H264_PROFILES if key in name), 0x64)
    return f"avc1.{profile_idc:02X}00{(level or 40):02X}"


async def probe(source: Union[Path, str]) -> dict:
    """Return stream and format info for ``source`` via ffprobe."""
    out = await _run(
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(source),
    )  # fmt: skip
    return json.loads(out)


async def keyframes(source: Path) -> List[Tuple[float, float]]:
    """Return (presentation, decode) time of every video keyframe, ascending."""
    out = await _run(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,dts_time,flags", "-of", "csv=p=0", str(source),
    )  # fmt: skip
    keys = []
    for line in out.decode().splitlines():
        fields = line.split(",")
        if len(fields) < 3 or not fields[2].startswith("K") or "N/A" in fields[:2]:
            continue
        keys.append((float(fields[0]), float(fields[1])))
    return sorted(keys)


def plan_segments(
    keys: List[Tuple[float, float]],
    duration: float,
    target: float = SEGMENT_TARGET_SECONDS,
) -> List[Segment]:
    """Cut at the first keyframe at least ``target`` seconds after the last cut."""
    cuts = [(0.0, 0.0)]
    for pts, dts in keys:
        if pts - cuts[-1][0] >= target:
            cuts.append((pts, dts))
    segments = []
    for i, (start, _) in enumerate(cuts):
        end_pts, end_dts = cuts[i + 1] if i + 1 < len(cuts) else (duration, duration)
        if end_pts - start > 0.001:
            segments.append(Segment(start, end_pts - start, end_dts - start - 0.001))
    return segments


def _pick_audio(streams: List[dict]) -> Tuple[Optional[dict], int]:
    audio = [s for s in streams if s["codec_type"] == "audio"]
    if not audio:
        return None, 0
    for i, s in enumerate(audio):
        if s.get("tags", {}).get("language") in ("eng", "en"):
            return s, i
    return audio[0], 0


async def plan(
    source: Union[Path, str], subtitle: Optional[Any] = None, index: Optional[Any] = None
) -> MediaPlan:
    """Probe ``source`` and decide what has to be converted.

    ``subtitle`` is an optional :class:`pyatv_cast.subtitles.Subtitle` to offer with it.
    ``index`` is a :class:`pyatv_cast.mp4index.Mp4Index`; with it the keyframes come from
    the file's index instead of ffprobe reading every packet, which a slow or partly
    copied file cannot afford.
    """
    info = await probe(source)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise ValueError(f"{source} has no video stream")
    audio, audio_index = _pick_audio(info["streams"])
    duration = float(info["format"]["duration"])
    result = MediaPlan(
        source=source,
        duration=duration,
        copy_video=video["codec_name"] == "h264" and video.get("pix_fmt") == "yuv420p",
        copy_audio=audio is not None and audio["codec_name"] == "aac",
        has_audio=audio is not None,
        audio_index=audio_index,
        bandwidth=int(info["format"].get("bit_rate", 0)) or 8_000_000,
        resolution=f"{video['width']}x{video['height']}",
        subtitle=subtitle,
    )
    if result.copy_video and (audio is None or result.copy_audio):
        codecs = [h264_codec_string(video.get("profile"), video.get("level"))]
        if audio is not None:
            codecs.append("mp4a.40.2")
        result.codecs = ",".join(codecs)
    keys = index.keyframes if index is not None else await keyframes(source)
    result.ts_shift = max(0.0, -keys[0][1]) if keys else 0.0
    result.segments = plan_segments(keys, duration)
    _LOGGER.info(
        "%s: %d segments, video %s, audio %s",
        getattr(source, "name", source), len(result.segments),
        "copy" if result.copy_video else f"transcode from {video['codec_name']}",
        "none" if not audio else "copy" if result.copy_audio else
        f"transcode from {audio['codec_name']}",
    )  # fmt: skip
    return result


def segment_command(media: MediaPlan, index: int) -> List[str]:
    """Build the ffmpeg command that writes segment ``index`` to stdout."""
    seg = media.segments[index]
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error",
        "-ss", f"{seg.start:.6f}", "-i", str(media.source), "-t", f"{seg.cut:.6f}",
        "-map", "0:v:0",
    ]  # fmt: skip
    if media.has_audio:
        cmd += ["-map", f"0:a:{media.audio_index}"]
    if media.copy_video:
        cmd += ["-c:v", "copy", "-bsf:v", "h264_mp4toannexb"]
    else:
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-force_key_frames", "expr:gte(t,0)",
        ]  # fmt: skip
    if media.has_audio:
        if media.copy_audio:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "256k", "-ac", "2"]
    cmd += [
        "-output_ts_offset", f"{seg.start + media.ts_shift:.6f}", "-muxdelay", "0", "-muxpreload", "0",
        "-f", "mpegts", "pipe:1",
    ]  # fmt: skip
    return cmd


@web.middleware
async def _log_requests(request: web.Request, handler):
    response = await handler(request)
    _LOGGER.debug("%s %s -> %s (%s)", request.method, request.path, response.status, request.remote)
    return response


class JitHlsServer:
    """HTTP server that converts to HLS on demand.

    Serves ``/index.m3u8`` and ``/segNNNNN.ts``. ``static_dir`` mode serves an existing
    HLS directory as is.
    """

    def __init__(self, media: MediaPlan, address: str, port: int = 0) -> None:
        """Initialize a new JitHlsServer."""
        self._media = media
        self._address = address
        self._port = port
        self._cache: "OrderedDict[int, bytes]" = OrderedDict()
        self._inflight: dict = {}
        self._slots = asyncio.Semaphore(MAX_FFMPEG)
        self._running: set = set()  # ffmpeg runs under way
        self._moved: set = set()  # ones stopped because the source changed
        self._runner: Optional[web.AppRunner] = None
        self.requests = 0  # what the renderer asked for: 0 means it never connected
        self.reached = 0.0  # start of the last segment asked for: where a stalled player is

    @property
    def media(self) -> MediaPlan:
        """What is being served."""
        return self._media

    def use_source(self, source: Union[Path, str]) -> None:
        """Cut segments from now on from ``source``, the same video read another way.

        A segment being cut starts over from it too: left reading the old source, the one
        segment the renderer is waiting for would be the last to arrive.
        """
        self._media.source = source
        self._moved |= self._running
        for run in self._running:
            run.cancel()

    async def _segment_bytes(self, index: int) -> bytes:
        if index in self._cache:
            self._cache.move_to_end(index)
            return self._cache[index]
        if index not in self._inflight:
            self._inflight[index] = asyncio.ensure_future(self._convert(index))
        try:
            return await asyncio.shield(self._inflight[index])
        finally:
            task = self._inflight.get(index)
            if task is not None and task.done():
                del self._inflight[index]

    async def _convert(self, index: int) -> bytes:
        async with self._slots:
            while True:
                run = asyncio.ensure_future(_run(*segment_command(self._media, index)))
                self._running.add(run)
                try:
                    data = await run
                    break
                except asyncio.CancelledError:
                    if run not in self._moved:
                        raise
                finally:
                    self._running.discard(run)
                    self._moved.discard(run)
        self._cache[index] = data
        while len(self._cache) > CACHE_SEGMENTS:
            self._cache.popitem(last=False)
        return data

    async def _playlist(self, request: web.Request) -> web.Response:
        return web.Response(
            text=self._media.playlist(), content_type="application/vnd.apple.mpegurl"
        )

    async def _master(self, request: web.Request) -> web.Response:
        return web.Response(text=self._media.master(), content_type="application/vnd.apple.mpegurl")

    async def _subtitle_playlist(self, request: web.Request) -> web.Response:
        return web.Response(
            text=self._media.subtitle_playlist(), content_type="application/vnd.apple.mpegurl"
        )

    async def _subtitle(self, request: web.Request) -> web.Response:
        vtt = self._media.webvtt(await self._media.subtitle.webvtt())
        return web.Response(body=vtt, content_type="text/vtt", charset="utf-8")

    async def _segment(self, request: web.Request) -> web.Response:
        index = int(request.match_info["index"])
        if not 0 <= index < len(self._media.segments):
            raise web.HTTPNotFound()
        self.reached = self._media.segments[index].start
        try:
            data = await self._segment_bytes(index)
        except RuntimeError as ex:
            _LOGGER.error("segment %d failed: %s", index, ex)
            raise web.HTTPInternalServerError() from ex
        _LOGGER.debug("served segment %d (%d bytes)", index, len(data))
        return web.Response(body=data, content_type="video/mp2t")

    async def start(self) -> str:
        """Start serving and return the playlist URL."""

        @web.middleware
        async def count(request: web.Request, handler):
            self.requests += 1
            return await _log_requests(request, handler)

        app = web.Application(middlewares=[count])
        app.router.add_get(f"/{PLAYLIST_NAME}", self._playlist)
        if self._media.subtitle:
            app.router.add_get(f"/{MASTER_NAME}", self._master)
            app.router.add_get(f"/{SUBTITLE_PLAYLIST_NAME}", self._subtitle_playlist)
            app.router.add_get(f"/{SUBTITLE_NAME}", self._subtitle)
        app.router.add_get(r"/seg{index:\d+}.ts", self._segment)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._address, self._port)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        return f"http://{self._address}:{port}/{self._media.entry_name}"

    async def stop(self) -> None:
        """Stop serving and drop cached segments."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._cache.clear()


async def convert_to_directory(media: MediaPlan, directory: Path) -> Path:
    """Convert every segment up front into ``directory`` and return the playlist path.

    The non-just-in-time mode: same segments as :class:`JitHlsServer`, but written to
    disk before playback starts.
    """
    directory.mkdir(parents=True, exist_ok=True)
    slots = asyncio.Semaphore(MAX_FFMPEG)

    async def one(index: int) -> None:
        async with slots:
            data = await _run(*segment_command(media, index))
        (directory / f"seg{index:05d}.ts").write_bytes(data)

    await asyncio.gather(*(one(i) for i in range(len(media.segments))))
    playlist = directory / PLAYLIST_NAME
    playlist.write_text(media.playlist())
    if media.subtitle:
        (directory / SUBTITLE_NAME).write_bytes(media.webvtt(await media.subtitle.webvtt()))
        (directory / SUBTITLE_PLAYLIST_NAME).write_text(media.subtitle_playlist())
        playlist = directory / MASTER_NAME
        playlist.write_text(media.master())
    return playlist


class StaticHlsServer:
    """Serve an existing HLS directory (a local ``.m3u8`` and its segments)."""

    def __init__(self, playlist: Path, address: str, port: int = 0) -> None:
        """Initialize a new StaticHlsServer."""
        self._playlist = playlist
        self._address = address
        self._port = port
        self._runner: Optional[web.AppRunner] = None

    async def _serve(self, request: web.Request) -> web.StreamResponse:
        root = self._playlist.parent.resolve()
        path = (root / request.match_info["name"]).resolve()
        if root not in path.parents or not path.is_file():
            raise web.HTTPNotFound()
        kind = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t"}.get(path.suffix)
        headers = {"Cache-Control": "no-cache"} if kind else {}
        return web.FileResponse(path, headers={**headers, **({"Content-Type": kind} if kind else {})})

    async def start(self) -> str:
        """Start serving and return the playlist URL."""
        app = web.Application()
        app.router.add_get("/{name:.+}", self._serve)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._address, self._port)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        return f"http://{self._address}:{port}/{self._playlist.name}"

    async def stop(self) -> None:
        """Stop serving."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
