"""A copy of a video with the subtitles inside it, for TVs that ignore a subtitle link.

Some DLNA renderers never fetch the subtitle file they are offered (castomatic notices: the
video is fetched and the subtitles are not), but do show a subtitle track inside the video.
For them the video is remuxed, never re-encoded, with the subtitles as its default track.
A plain file comes out, so the TV can still seek in it. From an internal drive this takes
seconds even for hours of video.
"""

import asyncio
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Collection, List, Optional

_LOGGER = logging.getLogger(__name__)

MKV_MIMES = ("video/x-matroska", "video/x-mkv", "video/mkv")
MP4_MIMES = ("video/mp4",)
MP4_SUFFIXES = (".mp4", ".m4v", ".mov")
PREFIX = "castomatic-subs-"
OWNER = "owner.pid"
ORPHAN_AGE = 3600.0  # a folder with no readable owner is left alone this long (it may be new)


class EmbedError(Exception):
    """The copy could not be made."""


@dataclass
class Embedded:
    """The copy, the type to announce it as, and the folder to delete when done."""

    path: Path
    mime: str
    folder: tempfile.TemporaryDirectory

    def close(self) -> None:
        """Delete the copy."""
        try:
            self.folder.cleanup()
        except OSError as ex:  # pragma: no cover - a file still open on some systems
            _LOGGER.debug("could not delete %s: %s", self.folder.name, ex)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # someone else's process
        return True
    return True


def sweep(folder: Optional[str] = None) -> List[Path]:
    """Delete copies left by castomatic runs that were killed before they could tidy up.

    A copy is the size of the video, so one left behind matters. Each folder names the
    process that made it; a folder whose process is gone is removed.
    """
    removed = []
    for path in Path(folder or tempfile.gettempdir()).glob(f"{PREFIX}*"):
        try:
            pid = int((path / OWNER).read_text().strip())
            orphan = not _alive(pid)
        except (OSError, ValueError):
            try:
                orphan = time.time() - path.stat().st_mtime > ORPHAN_AGE
            except OSError:  # pragma: no cover - removed while we looked
                continue
        if orphan:
            shutil.rmtree(path, ignore_errors=True)
            _LOGGER.info("deleted a subtitle copy left by an earlier run: %s", path)
            removed.append(path)
    return removed


def container_for(source: Path, sink: Collection[str]) -> Optional[tuple]:
    """``(suffix, mime, subtitle codec)`` for the copy, or None if the TV takes neither.

    Matroska first: it holds SRT as it is and needs no second pass. MP4 (subtitles as
    ``mov_text``) for a TV that does not list Matroska, when the video is MP4 already.
    """
    mkv = next((m for m in MKV_MIMES if m in sink), None)
    if mkv:
        return ".mkv", mkv, "srt"
    if source.suffix.lower() in MP4_SUFFIXES and "video/mp4" in sink:
        return ".mp4", "video/mp4", "mov_text"
    return None


def command(source: Path, srt: Path, out: Path, codec: str, language: str = "") -> List[str]:
    """The ffmpeg command: every video and audio track copied, the subtitles added as default."""
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-progress", "pipe:1",
        "-i", str(source), "-i", str(srt),
        "-map", "0:v", "-map", "0:a?", "-map", "1:0", "-map_metadata", "0",
        "-c", "copy", "-c:s", codec, "-disposition:s:0", "default",
    ]  # fmt: skip
    if language:
        cmd += ["-metadata:s:s:0", f"language={language}"]
    if out.suffix == ".mp4":
        cmd += ["-movflags", "+faststart"]  # the index first, so the TV can start at once
    return cmd + [str(out)]


async def embed(
    source: Path,
    srt: bytes,
    sink: Collection[str],
    duration: Optional[float] = None,
    language: str = "",
    on_progress: Optional[Callable[[float], None]] = None,
    folder: Optional[str] = None,
) -> Embedded:
    """Copy ``source`` with ``srt`` inside it. ``on_progress`` gets the fraction done."""
    found = container_for(source, sink)
    if found is None:
        raise EmbedError("the TV takes neither Matroska nor this MP4 as a file")
    suffix, mime, codec = found
    sweep(folder)
    work = tempfile.TemporaryDirectory(prefix=PREFIX, dir=folder)
    done = False
    try:
        (Path(work.name) / OWNER).write_text(str(os.getpid()))
        subs = Path(work.name) / "subs.srt"
        subs.write_bytes(srt)
        out = Path(work.name) / f"video{suffix}"
        cmd = command(source, subs, out, codec, language)
        _LOGGER.debug("adding subtitles: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        errors = asyncio.ensure_future(proc.stderr.read())
        try:
            async for line in proc.stdout:
                key, _, value = line.decode(errors="replace").strip().partition("=")
                if key == "out_time_us" and value.isdigit() and duration and on_progress:
                    on_progress(min(1.0, int(value) / 1e6 / duration))
            await proc.wait()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        message = (await errors).decode(errors="replace").strip()
        if proc.returncode != 0:
            raise EmbedError(message.splitlines()[-1] if message else f"ffmpeg exited with {proc.returncode}")
        done = True
        return Embedded(out, mime, work)
    finally:
        if not done:
            work.cleanup()
