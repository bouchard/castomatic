"""Decide whether a source needs converting, and convert it just in time as a live stream.

For DLNA renderers that play plain files but not HLS, a source that cannot be sent as it
is gets converted by ffmpeg while the renderer is downloading it: MPEG-TS over HTTP. Video
and audio that the TV already understands are copied, so only the streams that need it
are re-encoded.
"""

import asyncio
import functools
import json
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

from aiohttp import web

from castomatic.backends.base import allow_any_origin
from castomatic.errors import UserError
from castomatic.sources import Input

_LOGGER = logging.getLogger(__name__)

VIDEO_COPY = {"h264", "hevc", "mpeg2video"}  # codecs that go into MPEG-TS unchanged
AUDIO_COPY = {"aac", "ac3", "eac3", "mp3", "mp2"}
DIRECT_VIDEO = VIDEO_COPY | {"mpeg4"}  # codecs a TV plays from a plain file
OK_PIXEL_FORMATS = {"h264": {"yuv420p"}, "hevc": {"yuv420p", "yuv420p10le"}}
TS_MIMES = ("video/vnd.dlna.mpeg-tts", "video/mpeg", "video/mp2t")
STREAM_FLAGS = "DLNA.ORG_OP=00;DLNA.ORG_CI=1;DLNA.ORG_FLAGS=01700000000000000000000000000000"
# Assumed when a renderer cannot tell us what it plays
DEFAULT_SINK = {"video/mp4", "video/x-mkv", "video/vnd.dlna.mpeg-tts"}
CONTAINER_MIMES = {
    ".mp4": ("video/mp4",),
    ".m4v": ("video/mp4",),
    ".mkv": ("video/x-mkv", "video/x-matroska"),
    ".webm": ("video/webm",),
    ".avi": ("video/x-msvideo", "video/avi", "video/x-avi"),
    ".mov": ("video/quicktime",),
    ".ts": TS_MIMES,
    ".m2ts": TS_MIMES,
    ".mpg": ("video/mpeg",),
    ".mpeg": ("video/mpeg",),
    ".wmv": ("video/x-ms-wmv",),
    ".flv": ("video/x-flv",),
    ".3gp": ("video/3gpp",),
}  # fmt: skip


def ensure_ffmpeg() -> None:
    """Fail early, with the fix, if ffmpeg or ffprobe is missing."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise UserError(
            f"{' and '.join(missing)} not found on your PATH.",
            "Install ffmpeg with `brew install ffmpeg` (or your package manager). It is "
            "used to inspect the video and to convert it when the TV cannot play it.",
        )


def _header_args(headers: Dict[str, str]) -> List[str]:
    if not headers:
        return []
    return ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]


class ProbeError(UserError):
    """ffprobe could not read an input; ``reason`` is ffmpeg's short explanation."""

    def __init__(self, message: str, hint: str, reason: str) -> None:
        """Initialize a new ProbeError."""
        super().__init__(message, hint)
        self.reason = reason


def describe(source: Input) -> str:
    """A short name for an input: signed web addresses are hundreds of characters long."""
    if not source.is_remote:
        return source.location
    parts = urlparse(source.location)
    name = Path(parts.path).name
    return f"{parts.scheme}://{parts.netloc}" + (f"/…/{name}" if name else "")


async def probe_input(source: Input) -> Dict[str, Any]:
    """ffprobe one input, turning failures into a message the user can act on."""
    args = [
        "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
        *_header_args(source.headers), source.location,
    ]  # fmt: skip
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        lines = err.decode(errors="replace").strip().splitlines()
        reason = (lines[-1] if lines else "unknown error").rsplit(": ", 1)[-1]  # drop the address
        if source.is_remote and "403" in reason:
            hint = (
                "The site refuses direct requests for this address (HTTP 403). Sites like "
                "YouTube do this to programs other than their player."
            )
        elif source.is_remote:
            hint = (
                "Check that the address still works in a browser. Links from some sites "
                "expire quickly or only work from the page itself."
            )
        else:
            hint = "The file may be damaged, still downloading, or not a video."
        raise ProbeError(f"ffprobe could not read {describe(source)}: {reason}", hint, reason)
    return json.loads(out)


@dataclass
class Summary:
    """What is in the source, across all its inputs."""

    video_input: int
    video_codec: str
    pix_fmt: str
    audio_input: Optional[int]
    audio_index: int
    audio_codec: Optional[str]
    duration: Optional[float]


def summarize(infos: List[Dict[str, Any]]) -> Summary:
    """Pick the video and audio streams to use from the probed inputs."""
    video = next(
        ((i, s) for i, info in enumerate(infos) for s in info["streams"]
         if s["codec_type"] == "video" and s.get("disposition", {}).get("attached_pic") != 1),
        None,
    )  # fmt: skip
    if video is None:
        raise UserError(
            "this source has no video stream.",
            "It may be audio only. This tool plays video.",
        )
    audio = None
    for i, info in enumerate(infos):
        tracks = [s for s in info["streams"] if s["codec_type"] == "audio"]
        if tracks:
            pick = next(
                (n for n, s in enumerate(tracks) if s.get("tags", {}).get("language") in ("eng", "en")),
                0,
            )  # fmt: skip
            audio = (i, pick, tracks[pick])
            break
    durations = [float(i["format"]["duration"]) for i in infos if i["format"].get("duration")]
    return Summary(
        video_input=video[0],
        video_codec=video[1]["codec_name"],
        pix_fmt=video[1].get("pix_fmt", ""),
        audio_input=audio[0] if audio else None,
        audio_index=audio[1] if audio else 0,
        audio_codec=audio[2]["codec_name"] if audio else None,
        duration=max(durations) if durations else None,
    )


def container_mimes(location: str) -> tuple:
    """Media types a renderer would list for this file's container."""
    return CONTAINER_MIMES.get(Path(urlparse(location).path).suffix.lower(), ())


def conversion_reasons(
    summary: Summary,
    location: str,
    sink: Set[str],
    video_ok: Optional[Set[str]] = None,
    audio_ok: Optional[Set[str]] = None,
) -> List[str]:
    """Why this source cannot be sent to the renderer as it is; empty means it can.

    ``video_ok`` and ``audio_ok`` are the codecs the TV decodes (a :class:`Capabilities`'
    ``video`` and ``audio``); by default, what DLNA renderers generally play.
    """
    sink = sink or DEFAULT_SINK
    video_ok = video_ok or DIRECT_VIDEO
    audio_ok = audio_ok or AUDIO_COPY
    reasons = []
    mimes = container_mimes(location)
    if not any(m in sink for m in mimes):
        suffix = Path(urlparse(location).path).suffix or "this container"
        reasons.append(f"the TV does not list {suffix} files")
    if summary.video_codec not in video_ok:
        reasons.append(f"video is {summary.video_codec}")
    elif summary.pix_fmt not in OK_PIXEL_FORMATS.get(summary.video_codec, {summary.pix_fmt}):
        reasons.append(f"video is {summary.pix_fmt} {summary.video_codec}")
    if summary.audio_codec and summary.audio_codec not in audio_ok:
        reasons.append(f"audio is {summary.audio_codec}")
    return reasons


def matching_mime(location: str, sink: Set[str]) -> str:
    """The media type to announce for a plain file, as the renderer spells it."""
    mimes = container_mimes(location)
    return next((m for m in mimes if m in sink), mimes[0] if mimes else "video/mp4")


@dataclass
class StreamPlan:
    """How to turn the inputs into one MPEG-TS stream."""

    inputs: List[Input]
    summary: Summary
    copy_video: bool
    copy_audio: bool
    burn: Optional[Path] = None  # an SRT file drawn onto the picture (forces re-encoding)

    @property
    def description(self) -> str:
        """What will happen, in words."""
        video = "copying video" if self.copy_video else "converting video to H.264"
        if self.burn is not None:
            video = "converting video to H.264 with the subtitles drawn on"
        if not self.summary.audio_codec:
            return video
        audio = "copying audio" if self.copy_audio else "converting audio to AAC"
        return f"{video}, {audio}"


def make_plan(
    inputs: List[Input],
    summary: Summary,
    force_convert: bool = False,
    video_ok: Optional[Set[str]] = None,
    audio_ok: Optional[Set[str]] = None,
) -> StreamPlan:
    """Decide stream by stream what can be copied.

    ``video_ok`` / ``audio_ok`` narrow what may be copied to what the TV decodes (by
    default anything that fits in MPEG-TS); everything else is converted to H.264 / AAC.
    """
    video_ok = VIDEO_COPY if video_ok is None else VIDEO_COPY & set(video_ok)
    audio_ok = AUDIO_COPY if audio_ok is None else AUDIO_COPY & set(audio_ok)
    copy_video = (
        not force_convert
        and summary.video_codec in video_ok
        and summary.pix_fmt in OK_PIXEL_FORMATS.get(summary.video_codec, {summary.pix_fmt})
    )
    copy_audio = not force_convert and summary.audio_codec in audio_ok
    return StreamPlan(inputs, summary, copy_video, copy_audio)


@functools.lru_cache(maxsize=1)
def video_encoder() -> str:
    """The best H.264 encoder this ffmpeg has: hardware on a Mac, else libx264."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10
            ).stdout
            if "h264_videotoolbox" in out:
                return "h264_videotoolbox"
        except (OSError, subprocess.SubprocessError):
            pass
    return "libx264"


def _base_command(plan: StreamPlan, start: float) -> List[str]:
    """ffmpeg inputs, stream selection and codec choices, shared by every output."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    for source in plan.inputs:
        if start > 0:
            cmd += ["-ss", f"{start:.3f}"]
        if source.is_remote:
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        cmd += [*_header_args(source.headers), "-i", source.location]
    summary = plan.summary
    cmd += ["-map", f"{summary.video_input}:v:0"]
    if summary.audio_input is not None:
        cmd += ["-map", f"{summary.audio_input}:a:{summary.audio_index}"]
    if plan.burn is not None:
        cmd += ["-vf", f"subtitles=filename={_filter_escape(str(plan.burn))}"]
    if plan.copy_video and plan.burn is None:
        cmd += ["-c:v", "copy"]
        if summary.video_codec in ("h264", "hevc"):
            cmd += ["-bsf:v", f"{summary.video_codec}_mp4toannexb"]
    elif video_encoder() == "h264_videotoolbox":
        cmd += ["-c:v", "h264_videotoolbox", "-b:v", "10M", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    if summary.audio_input is not None:
        if plan.copy_audio:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "256k", "-ac", "2"]
    return cmd


def _filter_escape(text: str) -> str:
    """Escape a value for an ffmpeg filter option, as ffmpeg's filtergraph docs describe.

    Two levels: first the option value (``\\``, ``'`` and ``:`` are special there), then the
    filtergraph around it (``\\``, ``'``, ``[``, ``]``, ``,`` and ``;``).
    """
    for char in "\\':":
        text = text.replace(char, "\\" + char)
    for char in "\\'[],;":
        text = text.replace(char, "\\" + char)
    return text


@functools.lru_cache(maxsize=1)
def can_burn_subtitles() -> bool:
    """Whether this ffmpeg has the ``subtitles`` filter (it needs ffmpeg built with libass)."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[1:2] == ["subtitles"] for line in out.splitlines() if line.strip())


BURN_HINT = (
    "This ffmpeg was built without libass, which drawing subtitles needs. On a Mac: "
    "`brew uninstall ffmpeg && brew tap homebrew-ffmpeg/ffmpeg && brew install "
    "homebrew-ffmpeg/ffmpeg/ffmpeg`, or any ffmpeg build that lists `subtitles` in "
    "`ffmpeg -filters`."
)


def stream_command(plan: StreamPlan, start: float = 0.0) -> List[str]:
    """Build the ffmpeg command that writes the MPEG-TS stream to stdout."""
    return _base_command(plan, start) + [
        "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", "pipe:1",
    ]  # fmt: skip


def live_hls_command(
    plan: StreamPlan, directory: Path, start: float = 0.0, segment_seconds: int = 6
) -> List[str]:
    """Build the ffmpeg command that writes HLS segments into ``directory`` as it goes.

    The playlist is an EVENT playlist: it grows while ffmpeg works and gets its
    ``#EXT-X-ENDLIST`` when the source is finished, so playback can start right away.
    """
    return _base_command(plan, start) + [
        "-f", "hls", "-hls_time", str(segment_seconds), "-hls_list_size", "0",
        "-hls_playlist_type", "event",
        "-hls_segment_filename", str(directory / "seg%05d.ts"),
        str(directory / "index.m3u8"),
    ]  # fmt: skip


class StreamServer:
    """HTTP server that converts to MPEG-TS while the renderer downloads.

    ``requests`` counts what the renderer asked for, which tells "the TV never connected"
    (a firewall) from "it connected but could not play it" (a format problem).
    """

    def __init__(
        self,
        plan: StreamPlan,
        address: str,
        start: float = 0.0,
        subtitle: Any = None,
        port: int = 0,
    ) -> None:
        """Initialize a new StreamServer."""
        self._plan = plan
        self._address = address
        self._port = port
        self._start = start
        self._subtitle = subtitle
        self._srt = b""
        self._runner: Optional[web.AppRunner] = None
        self.subtitle_url: Optional[str] = None
        self.requests = 0
        self.last_error: Optional[str] = None
        self.mime = TS_MIMES[0]
        self.description = ""  # what ffmpeg will do, for the CLI to show

    async def _stream(self, request: web.Request) -> web.StreamResponse:
        self.requests += 1
        _LOGGER.debug("%s %s from %s", request.method, request.path, request.remote)
        headers = {
            "Content-Type": self.mime,
            "transferMode.dlna.org": "Streaming",
            "contentFeatures.dlna.org": STREAM_FLAGS,
        }
        if self.subtitle_url:
            headers["CaptionInfo.sec"] = self.subtitle_url
        response = web.StreamResponse(headers=headers)
        response.enable_chunked_encoding()
        await response.prepare(request)
        if request.method == "HEAD":
            return response
        proc = await asyncio.create_subprocess_exec(
            *stream_command(self._plan, self._start),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        errors = asyncio.ensure_future(proc.stderr.read())
        try:
            while chunk := await proc.stdout.read(65536):
                await response.write(chunk)
            await response.write_eof()
        except ConnectionError:
            _LOGGER.debug("renderer closed the stream")
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            text = (await errors).decode(errors="replace").strip()
            if text and proc.returncode not in (0, -9):
                self.last_error = " | ".join(text.splitlines()[-2:])
                _LOGGER.error("ffmpeg: %s", self.last_error)
        return response

    async def _subtitle_file(self, request: web.Request) -> web.Response:
        _LOGGER.debug("subtitle requested by %s", request.remote)
        return web.Response(body=self._srt, content_type="text/srt", charset="utf-8")

    async def start(self) -> str:
        """Start serving and return the stream URL."""
        app = web.Application()
        allow_any_origin(app)
        app.router.add_route("*", "/stream.ts", self._stream)
        if self._subtitle is not None:
            self._srt = await self._subtitle.srt(self._start)
            app.router.add_get("/subs.srt", self._subtitle_file)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._address, self._port)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        base = f"http://{self._address}:{port}"
        if self._subtitle is not None:
            self.subtitle_url = f"{base}/subs.srt"
        return f"{base}/stream.ts"

    async def stop(self) -> None:
        """Stop serving."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
