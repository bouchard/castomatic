"""Live HLS: start playing a remote video while ffmpeg is still fetching and cutting it.

The just-in-time HLS server (``hls.JitHlsServer``) needs the whole file's keyframe list up
front, which for a remote file would mean downloading all of it first. Here ffmpeg instead
writes HLS segments into a temporary folder as it downloads, and the playlist grows with
them. Playback starts once the first couple of segments exist.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from castomatic import stream
from castomatic.errors import UserError

_LOGGER = logging.getLogger(__name__)
READY_SEGMENTS = 2
START_TIMEOUT = 180.0
POLL = 0.25


class LiveHls:
    """One ffmpeg process turning a (remote) source into growing HLS in ``directory``."""

    def __init__(self, plan: stream.StreamPlan, directory: Path, start: float = 0.0) -> None:
        """Initialize a new LiveHls."""
        self._plan = plan
        self._directory = directory
        self._start = start
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stderr: Optional["asyncio.Task[bytes]"] = None
        self._watch: Optional["asyncio.Task[None]"] = None
        self.failed = asyncio.Event()
        self.error: Optional[str] = None

    @property
    def playlist(self) -> Path:
        """The growing playlist."""
        return self._directory / "index.m3u8"

    def segments(self) -> int:
        """How many segments have been written so far."""
        try:
            return self.playlist.read_text().count("#EXTINF")
        except FileNotFoundError:
            return 0

    def finished(self) -> bool:
        """Whether the whole source has been converted."""
        try:
            return "#EXT-X-ENDLIST" in self.playlist.read_text()
        except FileNotFoundError:
            return False

    async def start(self, timeout: float = START_TIMEOUT) -> Path:
        """Start ffmpeg and wait until there is enough to begin playback."""
        self._directory.mkdir(parents=True, exist_ok=True)
        cmd = stream.live_hls_command(self._plan, self._directory, self._start)
        _LOGGER.debug("live HLS: %s", cmd)
        self._proc = await asyncio.create_subprocess_exec(
            *cmd, stderr=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL
        )
        self._stderr = asyncio.ensure_future(self._proc.stderr.read())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.segments() < READY_SEGMENTS and not self.finished():
            if self._proc.returncode not in (None, 0):
                raise UserError(
                    f"could not prepare the video: {await self._reason()}",
                    "The address may have expired or the source may be unreadable. "
                    "Try again, or use `--download-first`.",
                )
            if loop.time() > deadline:
                await self.stop()
                raise UserError(
                    f"the video did not start flowing within {timeout:.0f} seconds.",
                    "The source may be slow or blocked. Try again, or use `--download-first`.",
                )
            await asyncio.sleep(POLL)
        self._watch = asyncio.ensure_future(self._watch_process())
        return self.playlist

    async def _reason(self) -> str:
        text = (await self._stderr).decode(errors="replace").strip() if self._stderr else ""
        return " | ".join(text.splitlines()[-2:]) or f"ffmpeg exited with {self._proc.returncode}"

    async def _watch_process(self) -> None:
        """Flag a failure if ffmpeg stops before the whole source was converted."""
        assert self._proc is not None
        code = await self._proc.wait()
        if code not in (0, -9, -15) and not self.finished():
            self.error = await self._reason()
            self.failed.set()

    async def stop(self) -> None:
        """Stop ffmpeg."""
        if self._watch:
            self._watch.cancel()
            self._watch = None
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
        if self._proc:
            await self._proc.wait()
