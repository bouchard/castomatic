"""Just-in-time HLS: serve a media file as HLS without writing any intermediate files.

A VOD playlist is built up front from the source's keyframes. Each ``.ts`` segment is
produced by ffmpeg only when the receiver asks for it. Streams the TV decodes are copied
untouched (remuxed); anything else is transcoded, and only that stream. By default only
H.264 video and AAC audio are copied, what every HLS player takes.

Segments are MPEG-TS, except when HEVC video is copied: HLS defines HEVC only in fragmented
MP4, and a Chromecast given HEVC in MPEG-TS showed no picture, or showed it half a second
behind the sound. fMP4 segments are cut from one continuous ffmpeg run, which also writes
their shared init segment (``init.mp4``): see :class:`FragmentRun` for why.
"""

import asyncio
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, List, Optional, Set, Tuple, Union

from aiohttp import web

from castomatic.backends.base import allow_any_origin

_LOGGER = logging.getLogger(__name__)

PLAYLIST_NAME = "index.m3u8"
MASTER_NAME = "master.m3u8"
SUBTITLE_PLAYLIST_NAME = "subs.m3u8"
SUBTITLE_NAME = "subs.vtt"
INIT_NAME = "init.mp4"
# Video codecs HLS carries only in fragmented MP4 segments
FMP4_VIDEO = frozenset({"hevc"})
# fMP4 timestamps cannot go below zero, and copied B-frame video and AAC's priming start a
# little before it: everything is moved this much later (on top of ts_shift)
FMP4_PAD = 0.1
SEGMENT_TARGET_SECONDS = 6.0
CACHE_SEGMENTS = 6
MAX_FFMPEG = 2
BOUNDARY_NUDGE = 0.5  # seconds: how far into a segment a start that falls on its edge goes
# What is copied when the caller does not say what the TV decodes
DEFAULT_VIDEO_COPY = frozenset({"h264"})
DEFAULT_AUDIO_COPY = frozenset({"aac"})
# Pixel formats each copied video codec may have
COPY_PIXEL_FORMATS = {"h264": {"yuv420p"}, "hevc": {"yuv420p", "yuv420p10le"}}


@dataclass
class Segment:
    """One planned HLS segment."""

    start: float
    duration: float
    # where copied video stops: just before the next keyframe's *decode* time, so
    # B-frame reordering cannot make neighbouring segments overlap (audio runs to
    # ``duration``)
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
    video_codec: str = "h264"  # the source's, as ffprobe names it
    fmp4: bool = False  # segments are fragmented MP4 (with INIT_NAME) instead of MPEG-TS
    bandwidth: int = 8_000_000
    resolution: Optional[str] = None
    codecs: Optional[str] = None  # only known when the streams are copied as they are
    subtitle: Optional[Any] = None  # a castomatic.subtitles.Subtitle
    # Added to every segment's timestamps. The first video packet has a negative decode
    # time, which the muxer would otherwise shift for segment 0 only.
    ts_shift: float = 0.0
    segments: List[Segment] = field(default_factory=list)
    # The cuts are estimates (the index had no decode times); each is measured by
    # settle_cut just before its segment is made. ``settled`` holds the ones done.
    cuts_guessed: bool = False
    settled: Set[int] = field(default_factory=set)

    def playable_start(self, seconds: float) -> float:
        """``seconds``, moved off the very start of a segment.

        Asked to start exactly where a segment begins, some receivers
        can fetch that segment over and over without ever playing it.
        """
        for seg in self.segments:
            if seconds > 0 and abs(seconds - seg.start) < BOUNDARY_NUDGE:
                return seg.start + min(BOUNDARY_NUDGE, seg.duration / 2)
        return seconds

    @property
    def time_offset(self) -> float:
        """How far the segments' timestamps are ahead of the playlist's timeline."""
        return self.ts_shift + (FMP4_PAD if self.fmp4 else 0.0)

    @property
    def segment_suffix(self) -> str:
        """The segments' file extension."""
        return ".m4s" if self.fmp4 else ".ts"

    def segment_name(self, index: int) -> str:
        """The name segment ``index`` is served as."""
        return f"seg{index:05d}{self.segment_suffix}"

    @property
    def version(self) -> int:
        """The playlists' EXT-X-VERSION: 7 is the first with fMP4 segments."""
        return 7 if self.fmp4 else 3

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
            f"#EXTM3U\n#EXT-X-VERSION:{self.version}\n"
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

        Our segments start at ``time_offset`` seconds rather than 0, and without this
        header the cues would run that much early.
        """
        header, _, rest = vtt.partition(b"\n")
        stamp = f"X-TIMESTAMP-MAP=MPEGTS:{round(self.time_offset * 90000)},LOCAL:00:00:00.000"
        return header + b"\n" + stamp.encode() + b"\n" + rest

    def playlist(self) -> str:
        """Render the VOD media playlist."""
        target = int(max(s.duration for s in self.segments) + 0.999)
        lines = [
            "#EXTM3U",
            f"#EXT-X-VERSION:{self.version}",
            f"#EXT-X-TARGETDURATION:{target}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
        ]
        if self.fmp4:
            lines.append(f'#EXT-X-MAP:URI="{INIT_NAME}"')
        for i, seg in enumerate(self.segments):
            lines += [f"#EXTINF:{seg.duration:.6f},", self.segment_name(i)]
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


def hevc_codec_string(profile: Optional[str], level: Optional[int]) -> str:
    """Return an RFC 6381 ``hvc1`` string, e.g. ``hvc1.2.4.L120.90`` for Main 10 at level 4.

    ffprobe gives the profile and level but not the tier or constraint flags; main tier
    and progressive, frame-only pictures (``90``) are what TV-ready files have.
    """
    name = (profile or "main").lower()
    if "10" in name:
        idc, compatible = 2, 1 << 2
    elif name == "main":
        idc, compatible = 1, (1 << 1) | (1 << 2)  # a Main stream is Main 10 too
    else:  # format range extensions
        idc, compatible = 4, 1 << 4
    return f"hvc1.{idc}.{compatible:X}.L{level or 120}.90"


_CODEC_STRINGS = {"h264": h264_codec_string, "hevc": hevc_codec_string}


async def probe(source: Union[Path, str]) -> dict:
    """Return stream and format info for ``source`` via ffprobe."""
    out = await _run(
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(source),
    )  # fmt: skip
    return json.loads(out)


async def keyframe_delay(source: Union[Path, str], at: float) -> Optional[float]:
    """How far before the keyframe at ``at`` seconds a cut must go to come before it.

    With B-frames a keyframe is decoded a few frames before it is shown, and how many
    can change from one keyframe to the next. This is that lead plus half a frame,
    0.0 for a keyframe decoded when it is shown, or None if it could not be read.
    """
    out = await _run(
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", f"{at:.6f}%+#8",
        "-show_entries", "packet=pts_time,dts_time,flags", "-of", "csv=p=0", str(source),
    )  # fmt: skip
    packets = [line.split(",") for line in out.decode().splitlines()]
    packets = [f for f in packets if len(f) >= 3 and f[0] not in ("", "N/A")]
    if not packets or not packets[0][2].startswith("K") or abs(float(packets[0][0]) - at) > 0.002:
        return None
    # After a seek the demuxer may leave the first decode times unknown. They step one
    # frame at a time, so the keyframe's is counted back from the first two it gave.
    known = [(i, float(f[1])) for i, f in enumerate(packets) if f[1] not in ("", "N/A")]
    if len(known) < 2:
        return None
    (i, dts), (j, after) = known[0], known[1]
    frame = (after - dts) / (j - i)
    delay = float(packets[0][0]) - (dts - i * frame)
    if delay <= frame / 4:
        return 0.0
    # Half a frame more, so a cut made this far before a keyframe falls between the
    # frame decoded before it and the keyframe itself, whatever the rounding.
    return round(delay + frame / 2, 6)


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
    source: Union[Path, str],
    subtitle: Optional[Any] = None,
    index: Optional[Any] = None,
    video_ok: Optional[Iterable[str]] = None,
    audio_ok: Optional[Iterable[str]] = None,
) -> MediaPlan:
    """Probe ``source`` and decide what has to be converted.

    ``subtitle`` is an optional :class:`castomatic.subtitles.Subtitle` to offer with it.
    ``index`` is a :class:`castomatic.mp4index.Mp4Index`; with it the keyframes come from
    the file's index instead of ffprobe reading every packet, which a slow or partly
    copied file cannot afford. ``video_ok`` and ``audio_ok`` are the codecs the TV decodes
    inside HLS segments, which are copied rather than transcoded.
    """
    video_ok = DEFAULT_VIDEO_COPY if video_ok is None else frozenset(video_ok)
    audio_ok = DEFAULT_AUDIO_COPY if audio_ok is None else frozenset(audio_ok)
    info = await probe(source)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise ValueError(f"{source} has no video stream")
    audio, audio_index = _pick_audio(info["streams"])
    duration = float(info["format"]["duration"])
    result = MediaPlan(
        source=source,
        duration=duration,
        copy_video=video["codec_name"] in video_ok
        and video.get("pix_fmt") in COPY_PIXEL_FORMATS.get(video["codec_name"], ()),
        copy_audio=audio is not None and audio["codec_name"] in audio_ok,
        video_codec=video["codec_name"],
        has_audio=audio is not None,
        audio_index=audio_index,
        bandwidth=int(info["format"].get("bit_rate", 0)) or 8_000_000,
        resolution=f"{video['width']}x{video['height']}",
        subtitle=subtitle,
    )
    result.fmp4 = result.copy_video and result.video_codec in FMP4_VIDEO
    # The TV's player decides what it can play from CODECS. Left out, it assumes H.264
    # and fails on anything else, so name what the segments really hold whenever it is known.
    makes_aac = audio is None or not result.copy_audio or audio["codec_name"] == "aac"
    video_string = _CODEC_STRINGS.get(video["codec_name"]) if result.copy_video else None
    if video_string is not None and makes_aac:
        codecs = [video_string(video.get("profile"), video.get("level"))]
        if audio is not None:
            codecs.append("mp4a.40.2")
        result.codecs = ",".join(codecs)
    keys = index.keyframes if index is not None else await keyframes(source)
    if keys and all(pts == dts for pts, dts in keys):
        # The index had only presentation times (Matroska's has no others). Cutting on
        # those would end each segment a few frames into the next one. One keyframe's
        # lead is a first guess for all of them; settle_cut measures each before use.
        delay = (await keyframe_delay(source, keys[1][0]) if len(keys) > 1 else None) or 0.0
        keys = [(pts, pts - delay) for pts, _ in keys]
        result.cuts_guessed = True
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


async def settle_cut(media: MediaPlan, index: int) -> None:
    """Measure where segment ``index`` must end, if the plan could only guess it.

    Stream copy stops at a decode time, and the next keyframe's is only known by
    reading a few packets there. A cut left a frame too late repeats frames at the
    join, and one too early drops them: either shows as a stutter.
    """
    if not media.cuts_guessed or index in media.settled or index + 1 >= len(media.segments):
        return
    seg, after = media.segments[index], media.segments[index + 1]
    lead = await keyframe_delay(media.source, after.start)
    if lead is not None:
        cut = after.start - lead - seg.start - 0.001
        if abs(cut - seg.cut) > 0.001:
            _LOGGER.debug("segment %d: cut %.3f s, not the guessed %.3f s", index, cut, seg.cut)
        media.segments[index] = replace(seg, cut=cut)
    media.settled.add(index)


def _inputs(media: MediaPlan, seg: Segment, video_end: List[str], audio_end: List[str]) -> List[str]:
    """Input and -map options: the source opened once for video and once for audio.

    Copied video has to stop just before the next keyframe's decode time (``Segment.cut``),
    which with B-frames is earlier than where the next segment starts. Audio cut there
    too would leave a hole at every join, so it gets its own input and runs to the
    next segment's start. ``video_end``/``audio_end`` are the input options for each.
    """
    args = [*video_end, "-ss", f"{seg.start:.6f}", "-i", str(media.source)]
    if not media.has_audio:
        return args + ["-map", "0:v:0"]
    args += [*audio_end, "-ss", f"{seg.start:.6f}", "-i", str(media.source)]
    return args + ["-map", "0:v:0", "-map", f"1:a:{media.audio_index}"]


def segment_command(media: MediaPlan, index: int) -> List[str]:
    """Build the ffmpeg command that writes segment ``index`` to stdout."""
    seg = media.segments[index]
    # A decoded stream is trimmed by presentation time, so converted video can run to the
    # next keyframe; copied packets are trimmed by decode time (see Segment.cut).
    video_end = seg.cut if media.copy_video else seg.duration
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    cmd += _inputs(media, seg, ["-t", f"{video_end:.6f}"], ["-t", f"{seg.duration:.6f}"])
    if media.copy_video:
        # With B-frames, ffmpeg seeks a little before -ss, which from a cut exactly on a
        # keyframe lands on the keyframe before it. A decoded stream drops what comes
        # before -ss; -copypriorss 0 makes the copied one drop it too, or the segment
        # would repeat the whole previous one.
        cmd += ["-copypriorss", "0", "-c:v", "copy", "-bsf:v", f"{media.video_codec}_mp4toannexb"]
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


def run_command(media: MediaPlan, first: int) -> List[str]:
    """One ffmpeg run writing fMP4 from segment ``first`` to the end (see :class:`FragmentRun`).

    The MP4 muxer starts every track of every file at zero unless told the fragments are
    discontinuous (``frag_discont``), and then gives each fragment its real decode time
    (``tfdt``). So timestamps are kept as they are in the source (``-copyts``) and moved by
    ``time_offset``, the same in every run.
    """
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-copyts",
        "-ss", f"{media.segments[first].start:.6f}", "-i", str(media.source),
        "-map", "0:v:0",
    ]  # fmt: skip
    if media.has_audio:
        cmd += ["-map", f"0:a:{media.audio_index}"]
    # Only copied video is sent as fMP4; see segment_command for -copypriorss
    tag = "hvc1" if media.video_codec == "hevc" else "avc1"
    cmd += ["-copypriorss", "0", "-c:v", "copy", "-tag:v", tag]
    if media.has_audio:
        cmd += ["-c:a", "copy"] if media.copy_audio else ["-c:a", "aac", "-b:a", "256k", "-ac", "2"]
    cmd += [
        "-output_ts_offset", f"{media.time_offset:.6f}", "-avoid_negative_ts", "disabled",
        "-video_track_timescale", "90000", "-use_editlist", "0",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof+frag_discont",
        "-f", "mp4", "pipe:1",
    ]  # fmt: skip
    return cmd


def fragment_start(moof: bytes) -> Optional[float]:
    """When the first video frame of an fMP4 fragment is shown, in seconds.

    The video is track 1 at a 90 kHz timescale (``run_command``): its ``tfdt`` decode time
    plus the first sample's composition offset.
    """
    offset, end, decode, shown = 8, len(moof), None, 0
    while offset + 8 <= end:
        size = int.from_bytes(moof[offset : offset + 4], "big")
        if size < 8:
            break
        if moof[offset + 4 : offset + 8] == b"traf":
            box = offset + 8
            track = None
            while box + 8 <= offset + size:
                inner = int.from_bytes(moof[box : box + 4], "big")
                kind = moof[box + 4 : box + 8]
                if inner < 8:
                    break
                if kind == b"tfhd":
                    track = int.from_bytes(moof[box + 12 : box + 16], "big")
                elif kind == b"tfdt" and track == 1:
                    wide = moof[box + 8] == 1
                    decode = int.from_bytes(moof[box + 12 : box + (20 if wide else 16)], "big")
                elif kind == b"trun" and track == 1:
                    shown = _first_composition_offset(moof[box : box + inner])
                box += inner
        offset += size
    return None if decode is None else (decode + shown) / 90000


def _first_composition_offset(trun: bytes) -> int:
    flags = int.from_bytes(trun[9:12], "big")
    if not flags & 0x800 or int.from_bytes(trun[12:16], "big") == 0:
        return 0
    at = 16 + (4 if flags & 0x1 else 0) + (4 if flags & 0x4 else 0)
    at += sum(4 for bit in (0x100, 0x200, 0x400) if flags & bit)
    return int.from_bytes(trun[at : at + 4], "big", signed=trun[8] == 1)


class FragmentRun:
    """One ffmpeg run, cut into HLS segments at its fragment boundaries.

    Audio converted segment by segment starts each time with the encoder's priming frame,
    so neighbouring segments overlap or leave gaps. A Chromecast keeps time by the audio,
    and the video stuttered at every join. Cut from one continuous run, the audio flows on
    from one segment to the next. Each fragment starts on a keyframe and goes to the
    segment its first frame falls in.

    ``want(index)`` lets the run work ``AHEAD`` segments past what was asked for; then
    ffmpeg waits (its output is not read), so a run left behind by a seek costs nothing
    until it is stopped.
    """

    AHEAD = 2

    def __init__(self, media: MediaPlan, first: int, segment, failed) -> None:
        """Initialize a new FragmentRun; ``segment(index, data)`` gets each one made."""
        self._media = media
        self.first = first
        self.next = first  # the segment being made
        self._segment = segment
        self._failed = failed
        self._limit = first + self.AHEAD
        self._go = asyncio.Event()
        self._go.set()
        self.init: "asyncio.Future[bytes]" = asyncio.get_running_loop().create_future()
        self._task: Optional[asyncio.Task] = None
        self._proc: Any = None

    @property
    def done(self) -> bool:
        """Whether this run has finished or failed."""
        return self._task is not None and self._task.done()

    def reaches(self, index: int) -> bool:
        """Whether segment ``index`` is on its way from this run."""
        return not self.done and self.next <= index <= self.next + self.AHEAD

    def want(self, index: int) -> None:
        """Let the run go on until ``AHEAD`` segments past ``index``."""
        self._limit = max(self._limit, index + self.AHEAD)
        self._go.set()

    def start(self, to_the_end: bool = False) -> None:
        """Start ffmpeg; ``to_the_end`` runs it through without waiting to be asked."""
        if to_the_end:
            self._limit = len(self._media.segments)
        self._task = asyncio.ensure_future(self._read())

    async def wait(self) -> None:
        """Wait for the run to finish."""
        if self._task is not None:
            await asyncio.shield(self._task)

    async def stop(self) -> None:
        """Stop ffmpeg."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _index_of(self, start: float) -> int:
        at = start - self._media.time_offset + 0.001
        index = self.first
        while index + 1 < len(self._media.segments) and self._media.segments[index + 1].start <= at:
            index += 1
        return index

    async def _read(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *run_command(self._media, self.first),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        errors = asyncio.ensure_future(self._proc.stderr.read())
        buffer, head, parts = b"", b"", []
        try:
            while True:
                if self.next > self._limit:
                    self._go.clear()
                    await self._go.wait()
                chunk = await self._proc.stdout.read(1 << 16)
                if not chunk:
                    break
                buffer += chunk
                while len(buffer) >= 8:
                    size = int.from_bytes(buffer[:4], "big")
                    if size < 8:
                        raise RuntimeError("ffmpeg wrote a broken MP4 box")
                    if len(buffer) < size:
                        break
                    box, buffer = buffer[:size], buffer[size:]
                    kind = box[4:8]
                    if kind == b"moof":
                        if not self.init.done():
                            self.init.set_result(head)
                        index = self._index_of(fragment_start(box) or 0.0)
                        if parts and index != self.next:
                            self._segment(self.next, b"".join(parts))
                            parts = []
                        self.next = index
                        parts.append(box)
                    elif parts:
                        parts.append(box)
                    else:
                        head += box
            await self._proc.wait()
            if self._proc.returncode != 0 or not parts:
                text = (await errors).decode(errors="replace").strip()
                raise RuntimeError(f"ffmpeg failed: {text[-500:] or 'it wrote no media fragment'}")
            self._segment(self.next, b"".join(parts))
            self.next += 1
        except asyncio.CancelledError:
            raise
        except RuntimeError as ex:
            if not self.init.done():
                self.init.set_exception(ex)
                self.init.exception()  # retrieved: whoever waits for it is told by failed()
            self._failed(ex)
        finally:
            if self._proc.returncode is None:
                self._proc.kill()
            errors.cancel()
            # A paused run has a full pipe that asyncio stopped reading; wait() would
            # never return until it is read to its end
            await self._proc.stdout.read()
            await self._proc.wait()


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
        self._init: Optional[bytes] = None  # fMP4 only: the init segment, once one is made
        self._run: Optional[FragmentRun] = None  # fMP4 only: the ffmpeg run making segments
        self._waiting: dict = {}  # fMP4 only: segment index -> future for whoever asked
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
        if self._run is not None:
            old, self._run = self._run, None
            asyncio.ensure_future(old.stop())
            if self._waiting:
                self._start_run(min(self._waiting))

    async def _segment_bytes(self, index: int) -> bytes:
        if index in self._cache:
            self._cache.move_to_end(index)
            return self._cache[index]
        if self._media.fmp4:
            return await self._from_run(index)
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
            await settle_cut(self._media, index)
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
        self._keep(index, data)
        return data

    def _keep(self, index: int, data: bytes) -> None:
        self._cache[index] = data
        while len(self._cache) > CACHE_SEGMENTS:
            self._cache.popitem(last=False)

    def _start_run(self, first: int) -> "FragmentRun":
        if self._run is not None:
            asyncio.ensure_future(self._run.stop())
        _LOGGER.debug("ffmpeg run from segment %d", first)
        self._run = FragmentRun(self._media, first, self._made, self._run_failed)
        self._run.start()
        return self._run

    async def _from_run(self, index: int) -> bytes:
        """fMP4: a segment from the current run, or from a new run starting at it."""
        future = self._waiting.get(index)
        if future is None:
            future = self._waiting[index] = asyncio.get_running_loop().create_future()
        run = self._run
        if run is None or not run.reaches(index):
            run = self._start_run(index)
        run.want(index)
        return await asyncio.shield(future)

    def _made(self, index: int, data: bytes) -> None:
        if self._init is None and self._run is not None and self._run.init.done():
            self._init = self._run.init.result()
        self._keep(index, data)
        future = self._waiting.pop(index, None)
        if future is not None and not future.done():
            future.set_result(data)

    def _run_failed(self, error: Exception) -> None:
        waiting, self._waiting = self._waiting, {}
        for future in waiting.values():
            if not future.done():
                future.set_exception(error)

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

    async def _init_segment(self, request: web.Request) -> web.Response:
        if self._init is None:  # every run writes it first: from the one under way, or a new one
            run = self._run
            if run is None or run.done:
                run = self._start_run(self._index_at(self.reached))
            try:
                self._init = await asyncio.shield(run.init)
            except RuntimeError as ex:
                _LOGGER.error("init segment failed: %s", ex)
                raise web.HTTPInternalServerError() from ex
        return web.Response(body=self._init, content_type="video/mp4")

    def _index_at(self, seconds: float) -> int:
        return max(i for i, seg in enumerate(self._media.segments) if seg.start <= seconds + 0.001)

    async def _segment(self, request: web.Request) -> web.Response:
        index = int(request.match_info["index"])
        if request.match_info["suffix"] != self._media.segment_suffix:
            raise web.HTTPNotFound()
        if not 0 <= index < len(self._media.segments):
            raise web.HTTPNotFound()
        self.reached = self._media.segments[index].start
        try:
            data = await self._segment_bytes(index)
        except RuntimeError as ex:
            _LOGGER.error("segment %d failed: %s", index, ex)
            raise web.HTTPInternalServerError() from ex
        _LOGGER.debug("served segment %d (%d bytes)", index, len(data))
        kind = "video/iso.segment" if self._media.fmp4 else "video/mp2t"
        return web.Response(body=data, content_type=kind)

    async def start(self) -> str:
        """Start serving and return the playlist URL."""

        @web.middleware
        async def count(request: web.Request, handler):
            self.requests += 1
            return await _log_requests(request, handler)

        app = web.Application(middlewares=[count])
        allow_any_origin(app)
        app.router.add_get(f"/{PLAYLIST_NAME}", self._playlist)
        if self._media.subtitle:
            app.router.add_get(f"/{MASTER_NAME}", self._master)
            app.router.add_get(f"/{SUBTITLE_PLAYLIST_NAME}", self._subtitle_playlist)
            app.router.add_get(f"/{SUBTITLE_NAME}", self._subtitle)
        if self._media.fmp4:
            app.router.add_get(f"/{INIT_NAME}", self._init_segment)
        app.router.add_get(r"/seg{index:\d+}{suffix:\.(?:ts|m4s)}", self._segment)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._address, self._port)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        return f"http://{self._address}:{port}/{self._media.entry_name}"

    async def stop(self) -> None:
        """Stop serving and drop cached segments."""
        if self._run is not None:
            await self._run.stop()
            self._run = None
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
    if media.fmp4:
        await _run_to_directory(media, directory)
    else:
        await _segments_to_directory(media, directory)
    playlist = directory / PLAYLIST_NAME
    playlist.write_text(media.playlist())
    if media.subtitle:
        (directory / SUBTITLE_NAME).write_bytes(media.webvtt(await media.subtitle.webvtt()))
        (directory / SUBTITLE_PLAYLIST_NAME).write_text(media.subtitle_playlist())
        playlist = directory / MASTER_NAME
        playlist.write_text(media.master())
    return playlist


async def _run_to_directory(media: MediaPlan, directory: Path) -> None:
    """fMP4: every segment from one ffmpeg run (see :class:`FragmentRun`)."""
    failures: List[Exception] = []
    run = FragmentRun(
        media, 0, lambda i, data: (directory / media.segment_name(i)).write_bytes(data), failures.append
    )
    run.start(to_the_end=True)
    try:
        (directory / INIT_NAME).write_bytes(await run.init)
    finally:
        await run.wait()
    if failures:
        raise failures[0]


async def _segments_to_directory(media: MediaPlan, directory: Path) -> None:
    slots = asyncio.Semaphore(MAX_FFMPEG)

    async def one(index: int) -> None:
        async with slots:
            await settle_cut(media, index)
            data = await _run(*segment_command(media, index))
        (directory / media.segment_name(index)).write_bytes(data)

    await asyncio.gather(*(one(i) for i in range(len(media.segments))))


class StaticHlsServer:
    """Serve an existing HLS directory (a local ``.m3u8`` and its segments)."""

    def __init__(self, playlist: Path, address: str, port: int = 0) -> None:
        """Initialize a new StaticHlsServer."""
        self._playlist = playlist
        self._address = address
        self._port = port
        self._runner: Optional[web.AppRunner] = None
        self.requests = 0  # what the receiver asked for: 0 means it never connected

    async def _serve(self, request: web.Request) -> web.StreamResponse:
        self.requests += 1
        root = self._playlist.parent.resolve()
        path = (root / request.match_info["name"]).resolve()
        if root not in path.parents or not path.is_file():
            raise web.HTTPNotFound()
        kind = {
            ".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t", ".m4s": "video/iso.segment",
        }.get(path.suffix)  # fmt: skip
        headers = {"Cache-Control": "no-cache"} if kind else {}
        return web.FileResponse(path, headers={**headers, **({"Content-Type": kind} if kind else {})})

    async def start(self) -> str:
        """Start serving and return the playlist URL."""
        app = web.Application(middlewares=[_log_requests])
        allow_any_origin(app)
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
