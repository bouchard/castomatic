"""Turn whatever the user typed (a path, a media URL, a web page) into a playable source.

A local file or a direct media URL is used as is. Any other URL, such as a YouTube page,
is handed to yt-dlp, which finds the real media address.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import unquote, urlparse

import aiohttp

from castomatic.errors import UserError

_LOGGER = logging.getLogger(__name__)

MEDIA_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m2ts", ".mpg", ".mpeg",
    ".wmv", ".flv", ".3gp", ".m3u8",
}  # fmt: skip
HLS_TYPES = ("application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl")
# Prefer H.264 + AAC so the stream can be copied instead of re-encoded, up to 1080p
DEFAULT_YTDLP_FORMAT = (
    "bv*[height<=1080][vcodec^=avc1]+ba[acodec^=mp4a]"
    "/bv*[height<=1080]+ba/b[height<=1080]/b"
)


@dataclass
class Input:
    """One thing ffmpeg can read: a path or URL, plus any HTTP headers it needs."""

    location: str
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def is_remote(self) -> bool:
        """Whether this is fetched over the network."""
        return self.location.startswith(("http://", "https://"))


@dataclass
class MediaSource:
    """What to play."""

    title: str
    inputs: List[Input]
    path: Optional[Path] = None  # set for local files
    duration: Optional[float] = None
    via_ytdlp: bool = False
    # What yt-dlp needs to fetch the same video again, for a full download
    page_url: Optional[str] = None
    ytdlp_format: str = DEFAULT_YTDLP_FORMAT
    cookies_from_browser: Optional[str] = None
    size_estimate: Optional[int] = None  # bytes, when the site says
    is_live: bool = False  # a live stream: no end, no known length
    relay: Any = None  # the ytstream.YtdlpBuffer this is being streamed through, once started

    @property
    def is_local(self) -> bool:
        """Whether this is a file on this machine."""
        return self.path is not None

    @property
    def location(self) -> str:
        """The first input's path or URL."""
        return self.inputs[0].location

    @property
    def is_hls_playlist(self) -> bool:
        """Whether this is an HLS playlist the TV could fetch itself."""
        return len(self.inputs) == 1 and urlparse(self.location).path.lower().endswith(".m3u8")


def _title_from_url(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).stem
    return name or urlparse(url).netloc


async def is_direct_media(url: str) -> bool:
    """Whether ``url`` is a media file (or HLS playlist) rather than a web page."""
    if Path(urlparse(url).path.lower()).suffix in MEDIA_EXTENSIONS:
        return True
    try:
        async with aiohttp.ClientSession() as http:
            async with http.head(
                url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                kind = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError) as ex:
        _LOGGER.debug("could not check %s: %s", url, ex)
        return False
    return kind.startswith(("video/", "audio/")) or kind in HLS_TYPES


def _clean(message: str) -> str:
    """The useful part of a yt-dlp error: no colour codes, no ``ERROR:`` prefix."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", message).strip()
    return re.sub(r"^ERROR:\s*", "", text.splitlines()[-1] if text else "no details")


def _extract(url: str, fmt: str, cookies_from_browser: Optional[str], verbose: bool) -> dict:
    """Run yt-dlp (as a library) without downloading anything."""
    import yt_dlp  # imported here: it is large, and only needed for web pages

    options: Dict[str, Any] = {
        "format": fmt, "noplaylist": True, "skip_download": True,
        "quiet": not verbose, "no_warnings": not verbose,
    }  # fmt: skip
    if cookies_from_browser:
        options["cookiesfrombrowser"] = (cookies_from_browser,)
    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


async def from_ytdlp(
    url: str,
    ytdlp_format: str = DEFAULT_YTDLP_FORMAT,
    cookies_from_browser: Optional[str] = None,
    verbose: bool = False,
) -> MediaSource:
    """Ask yt-dlp for the real media address(es) behind ``url``."""
    import yt_dlp.utils

    _LOGGER.info("asking yt-dlp about %s", url)
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(
            None, _extract, url, ytdlp_format, cookies_from_browser, verbose
        )
    except yt_dlp.utils.DownloadError as ex:
        raise UserError(
            f"yt-dlp could not get a video from {url}: {_clean(str(ex))}",
            "If the site needs you to be logged in, use your browser's cookies: "
            "`--cookies-from-browser chrome` (or firefox, safari, ...).\n"
            "Sites change often, so upgrading yt-dlp (`pip install -U yt-dlp`, or "
            "reinstall this tool) fixes many failures. Run with -v for the full output.",
        ) from ex
    try:
        if info.get("_type") == "playlist":
            info = next(entry for entry in info["entries"] if entry)
        formats = info.get("requested_formats") or [info]
        inputs = [Input(f["url"], dict(f.get("http_headers") or {})) for f in formats]
    except (StopIteration, KeyError, TypeError, AttributeError) as ex:
        raise UserError(
            f"yt-dlp found {url} but returned nothing playable.",
            "Try a different page, or check what it offers with `yt-dlp -F URL`.",
        ) from ex
    size = sum(f.get("filesize") or f.get("filesize_approx") or 0 for f in formats)
    return MediaSource(
        info.get("title") or _title_from_url(url), inputs,
        duration=info.get("duration"), via_ytdlp=True, page_url=url,
        ytdlp_format=ytdlp_format, cookies_from_browser=cookies_from_browser,
        size_estimate=size or None, is_live=bool(info.get("is_live")),
    )  # fmt: skip


async def resolve(
    text: str,
    ytdlp_format: str = DEFAULT_YTDLP_FORMAT,
    cookies_from_browser: Optional[str] = None,
    verbose: bool = False,
) -> MediaSource:
    """Work out what ``text`` refers to."""
    path = Path(text).expanduser()
    if path.is_dir():
        raise UserError(
            f"{text} is a folder, not a video.",
            "Give the path of one video file inside it.",
        )
    if path.exists():
        resolved = path.resolve()
        return MediaSource(resolved.stem, [Input(str(resolved))], resolved)
    if urlparse(text).scheme in ("http", "https"):
        if await is_direct_media(text):
            return MediaSource(_title_from_url(text), [Input(text)])
        return await from_ytdlp(text, ytdlp_format, cookies_from_browser, verbose)
    if urlparse(text).scheme:
        raise UserError(
            f"{text}: only http:// and https:// addresses are supported.",
            "For a file on this computer, give its path instead.",
        )
    raise UserError(
        f"{text}: no such file, and it does not look like a URL.",
        "Check the spelling, and quote paths that contain spaces or brackets: "
        '`castomatic "My Movie (2026).mkv"`.',
    )


# -- downloading ---------------------------------------------------------------------------

# (bytes so far, total bytes if known, bytes per second if known)
ProgressFunc = Callable[[int, Optional[int], Optional[float]], None]


def format_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def check_space(directory: Path, needed: Optional[int]) -> None:
    """Refuse early if the temporary folder cannot hold the video."""
    if not needed:
        return
    free = shutil.disk_usage(directory).free
    if free < needed * 1.2 + 50 * 1024 * 1024:
        raise UserError(
            f"not enough free space in {directory}: the video needs about {format_size(needed)} "
            f"and only {format_size(free)} is free.",
            "Free up some space, or put the temporary files on a bigger disk with "
            "`--workdir /path/to/folder`.",
        )


def _download_failed(source: "MediaSource", reason: str) -> UserError:
    return UserError(
        f"downloading {source.title!r} failed: {reason}",
        "Check your connection and that the video is still available (links can expire), "
        "then try again. Free disk space and `--workdir` matter too for big videos.",
    )


async def _download_http(
    source: "MediaSource", directory: Path, on_progress: Optional[ProgressFunc]
) -> Path:
    first = source.inputs[0]
    suffix = Path(urlparse(first.location).path).suffix or ".mp4"
    target = directory / f"video{suffix}"
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(first.location, headers=first.headers) as resp:
                if resp.status != 200:
                    raise _download_failed(source, f"the server answered HTTP {resp.status}")
                total = resp.content_length
                check_space(directory, total)
                done, started = 0, time.monotonic()
                with target.open("wb") as out:
                    async for chunk in resp.content.iter_chunked(262144):
                        out.write(chunk)
                        done += len(chunk)
                        if on_progress:
                            elapsed = time.monotonic() - started
                            on_progress(done, total, done / elapsed if elapsed else None)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as ex:
        raise _download_failed(source, str(ex) or type(ex).__name__) from ex
    return target


def _ytdlp_options(source: "MediaSource", directory: Path, verbose: bool) -> Dict[str, Any]:
    options: Dict[str, Any] = {
        "format": source.ytdlp_format, "noplaylist": True, "overwrites": True,
        "outtmpl": str(directory / "video.%(ext)s"), "merge_output_format": "mkv",
        "quiet": not verbose, "no_warnings": not verbose, "noprogress": True,
    }  # fmt: skip
    if source.cookies_from_browser:
        options["cookiesfrombrowser"] = (source.cookies_from_browser,)
    return options


def _ytdlp_download(
    source: "MediaSource",
    directory: Path,
    on_progress: Optional[ProgressFunc],
    verbose: bool,
    cancel: threading.Event,
) -> Path:
    """Download the video with yt-dlp's own downloader (handles DASH, HLS and merging)."""
    import yt_dlp
    import yt_dlp.utils

    finished = 0  # bytes of the parts already completed (video, then audio)

    def hook(data: Dict[str, Any]) -> None:
        nonlocal finished
        if cancel.is_set():
            raise yt_dlp.utils.DownloadCancelled()
        if data["status"] == "finished":
            finished += data.get("downloaded_bytes") or data.get("total_bytes") or 0
        elif data["status"] == "downloading" and on_progress:
            done = finished + (data.get("downloaded_bytes") or 0)
            on_progress(done, source.size_estimate, data.get("speed"))

    options = _ytdlp_options(source, directory, verbose)
    options["progress_hooks"] = [hook]
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(source.page_url, download=True)
        done = info.get("requested_downloads") or []
        return Path(done[0]["filepath"]) if done else Path(ydl.prepare_filename(info))


def _ytdlp_subtitles(source: "MediaSource", directory: Path, verbose: bool) -> None:
    """Fetch just the English subtitles, next to the video as ``video.en.srt``."""
    import yt_dlp

    options = _ytdlp_options(source, directory, verbose)
    options.update(
        skip_download=True, writesubtitles=True, writeautomaticsub=True,
        retries=1, extractor_retries=1, socket_timeout=20,
        subtitleslangs=["en", "en-orig"], subtitlesformat="srt/vtt/best",
        postprocessors=[{"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"}],
    )  # fmt: skip
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.extract_info(source.page_url, download=True)


def _in_daemon_thread(func: Callable[..., Any], *args: Any) -> "asyncio.Future[Any]":
    """Run blocking ``func`` in a daemon thread and await its result.

    Unlike ``run_in_executor``, a thread stuck inside yt-dlp (say, backing off from a
    rate limit) can never keep the program from exiting after Ctrl-C.
    """
    loop = asyncio.get_running_loop()
    future: "asyncio.Future[Any]" = loop.create_future()

    def deliver(setter: Callable[[Any], None], value: Any) -> None:
        if not future.done():
            setter(value)

    def work() -> None:
        try:
            result = func(*args)
        except BaseException as ex:  # pylint: disable=broad-except
            loop.call_soon_threadsafe(deliver, future.set_exception, ex)
        else:
            loop.call_soon_threadsafe(deliver, future.set_result, result)

    threading.Thread(target=work, daemon=True).start()
    return future


SUBTITLE_RETRY_DELAYS = (5.0, 20.0, 60.0)  # seconds to wait before each retry
DONE_MARKER = "done.json"
SUBTITLE_GRACE = 20.0  # how long to keep waiting for subtitles once the video is ready


async def _run_ytdlp_download(source, directory, on_progress, verbose) -> Path:
    cancel = threading.Event()
    try:
        return await _in_daemon_thread(
            _ytdlp_download, source, directory, on_progress, verbose, cancel
        )
    except asyncio.CancelledError:
        cancel.set()  # the worker thread stops at its next progress report
        raise


async def fetch_subtitles(
    source: "MediaSource",
    directory: Path,
    verbose: bool = False,
    on_note: Optional[Callable[[str], None]] = None,
    delays: Optional[tuple] = None,
) -> bool:
    """Fetch subtitles on their own, retrying when the site pushes back (HTTP 429).

    This never raises: subtitles are a bonus, so any failure ends as a note. Returns
    whether a subtitle file was written.
    """
    import yt_dlp.utils

    note = on_note or (lambda text: None)
    delays = SUBTITLE_RETRY_DELAYS if delays is None else delays
    for attempt in range(len(delays) + 1):
        try:
            await _in_daemon_thread(_ytdlp_subtitles, source, directory, verbose)
        except (yt_dlp.utils.DownloadError, OSError, ValueError) as ex:
            reason = _clean(str(ex))
            if attempt == len(delays):
                note(f"subtitles unavailable after {attempt + 1} tries ({reason}); playing without them")
                return False
            note(f"subtitles refused ({reason}); retrying in {delays[attempt]:.0f}s")
            await asyncio.sleep(delays[attempt])
            continue
        if any(directory.glob("video.*.srt")):
            return True
        note("this video has no English subtitles")
        return False
    return False


def cache_dir(source: "MediaSource") -> Path:
    """Where yt-dlp downloads of this video (in this format) are kept between runs."""
    key = hashlib.sha1(f"{source.page_url}\n{source.ytdlp_format}".encode()).hexdigest()[:16]
    owner = os.getuid() if hasattr(os, "getuid") else "user"  # one folder per user: /tmp is shared
    return Path(tempfile.gettempdir()) / f"castomatic-cache-{owner}" / key


def _cached_video(directory: Path) -> Optional[Path]:
    """The finished download in a cache folder, if there is one."""
    try:
        name = json.loads((directory / DONE_MARKER).read_text())["video"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(name, str) or Path(name).name != name:  # a bare file name only
        return None
    video = directory / name
    return video if video.is_file() else None


def _keep(path: Path, directory: Path, with_subtitles: bool) -> Path:
    """Move a finished download into the cache. Falls back to where it is.

    Subtitles come along only when their fetch has ended: a fetch still running may be
    half way through writing a file.
    """
    try:
        directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.mkdir(mode=0o700, exist_ok=True)
        if with_subtitles:
            for part in sorted(path.parent.glob("video.*.srt")):
                shutil.move(str(part), directory / part.name)
        kept = Path(shutil.move(str(path), directory / path.name))
        marker = directory / (DONE_MARKER + ".part")  # written last, and renamed into place
        marker.write_text(json.dumps({"video": kept.name}))
        marker.replace(directory / DONE_MARKER)
        return kept
    except OSError as ex:
        _LOGGER.warning("could not keep the download for next time: %s", ex)
        return path


async def download(
    source: "MediaSource",
    directory: Path,
    on_progress: Optional[ProgressFunc] = None,
    want_subtitles: bool = True,
    verbose: bool = False,
    on_note: Optional[Callable[[str], None]] = None,
) -> "MediaSource":
    """Fetch the whole video into ``directory`` and return it as a local source.

    For yt-dlp videos the subtitles are fetched separately and at the same time, with
    retries, so a site refusing them can never fail or hold up the video.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if not source.via_ytdlp:
        check_space(directory, source.size_estimate)
        path = await _download_http(source, directory, on_progress)
        return MediaSource(source.title, [Input(str(path))], path, source.duration)

    import yt_dlp.utils

    kept = cache_dir(source)
    path = _cached_video(kept)
    if path:
        if on_note:
            on_note(f"using the copy downloaded earlier ({path})")
        directory = kept  # any missing subtitles are fetched next to it
    else:
        check_space(directory, source.size_estimate)  # nothing to fetch on a cache hit
    subs = None
    if want_subtitles and not (path and any(kept.glob("video.*.srt"))):
        subs = asyncio.ensure_future(fetch_subtitles(source, directory, verbose, on_note))
    try:
        path = path or await _run_ytdlp_download(source, directory, on_progress, verbose)
    except BaseException as ex:  # including Ctrl-C: do not leave the subtitle task running
        if subs:
            subs.cancel()
        if isinstance(ex, yt_dlp.utils.DownloadError):
            raise _download_failed(source, _clean(str(ex))) from ex
        raise
    if subs:
        try:
            await asyncio.wait_for(asyncio.shield(subs), SUBTITLE_GRACE)
        except asyncio.TimeoutError:
            subs.cancel()
            if on_note:
                on_note("still waiting on subtitles: playing without them")
    if directory != kept:
        path = _keep(path, kept, with_subtitles=not subs or subs.done() and not subs.cancelled())
    return MediaSource(source.title, [Input(str(path))], path, source.duration)
