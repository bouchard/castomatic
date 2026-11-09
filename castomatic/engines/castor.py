"""castor (github.com/stupside/castor), used when it is installed and only where it helps.

castor is a separate program (Go, MIT licence). It opens a web page in headless Chrome to
find the video playing there, which works on pages yt-dlp has no extractor for. Castomatic
uses it in two ways, both through its command line:

* :func:`resolve_page`: ``castor cast player URL --dry-run`` prints the streams it found,
  best first (``bitrate<TAB>url[<TAB>last resort]``). Castomatic then plays the best one
  itself, so buffering, conversion and every protocol work exactly as for any other address.
* :func:`cast`: hand the whole job to castor (``--engine castor``, or when castor's stream
  only plays from inside castor). The TV is passed in castor's own environment settings
  (``CASTOR_DEVICE__TYPE``/``HOST``/``NAME``), so no castor config file is needed.

castor is never required, and nothing here runs unless it is on the PATH.
"""

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Callable, List, Optional

from castomatic.errors import UserError

_LOGGER = logging.getLogger(__name__)

RESOLVE_TIMEOUT = 180.0  # castor starts Chrome and waits for the page's player
VERSION_TIMEOUT = 10.0
# castor's device types; it has no AirPlay
DEVICE_TYPES = {"dlna": "dlna", "chromecast": "chromecast", "roku": "roku"}
INSTALL_HINT = (
    "castor is optional. Install it with `brew install --cask stupside/tap/castor` (it also "
    "needs Google Chrome), see https://github.com/stupside/castor"
)


@dataclass
class Stream:
    """One stream castor found on a page."""

    url: str
    bitrate: Optional[int]
    last_resort: bool = False


def find() -> Optional[str]:
    """The castor program's path, or None if it is not installed."""
    return shutil.which("castor")


async def _run(args: List[str], timeout: float) -> tuple:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def version() -> Optional[str]:
    """castor's version line, or None if it is missing or will not say."""
    path = find()
    if path is None:
        return None
    try:
        code, out, err = await _run([path, "--version"], VERSION_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return None
    text = (out or err).strip().splitlines()
    return text[0] if code == 0 and text else "installed (version unknown)"


def parse_streams(text: str) -> List[Stream]:
    """Read ``castor cast ... --dry-run`` output: one ``bitrate<TAB>url`` per line."""
    found = []
    for line in text.splitlines():
        fields = line.strip().split("\t")
        if len(fields) < 2 or not fields[1].startswith(("http://", "https://")):
            continue
        bitrate = int(fields[0]) if fields[0].isdigit() else None
        found.append(Stream(fields[1], bitrate, len(fields) > 2 and "last resort" in fields[2]))
    return found


async def resolve_page(url: str, timeout: float = RESOLVE_TIMEOUT) -> List[Stream]:
    """The streams castor finds on the page at ``url``, best first (empty if none)."""
    path = find()
    if path is None:
        return []
    try:
        code, out, err = await _run([path, "cast", "player", url, "--dry-run"], timeout)
    except asyncio.TimeoutError:
        _LOGGER.debug("castor did not finish within %.0fs", timeout)
        return []
    except OSError as ex:
        _LOGGER.debug("could not run castor: %s", ex)
        return []
    if code != 0:
        _LOGGER.debug("castor exited with %s: %s", code, err.strip()[-500:])
        return []
    streams = parse_streams(out)
    # A "last resort" stream is one castor itself would only try after the others
    return sorted(streams, key=lambda s: s.last_resort)


def device_env(protocol: str, host: str, name: str) -> dict:
    """castor's environment settings that point it at one TV."""
    kind = DEVICE_TYPES.get(protocol)
    if kind is None:
        raise UserError(
            f"castor cannot play over {protocol}.",
            "castor plays to DLNA, Chromecast and Roku TVs. Leave out --engine castor, or "
            "pick one of those with --protocol.",
        )
    return {
        "CASTOR_DEVICE__TYPE": kind,
        "CASTOR_DEVICE__HOST": host,
        "CASTOR_DEVICE__NAME": name or host,
    }


def clock(seconds: float) -> str:
    """Seconds as castor's ``--at`` wants them (``1h12m3s``)."""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return (f"{hours}h" if hours else "") + (f"{minutes}m" if minutes or hours else "") + f"{secs}s"


def cast_command(path: str, source: str, start: float = 0.0) -> List[str]:
    """The castor command line that casts ``source`` (a web page or a stream address)."""
    is_stream = source.split("?")[0].lower().endswith(
        (".m3u8", ".mpd", ".mp4", ".mkv", ".webm", ".mov", ".ts")
    )
    cmd = [path, "cast", "url" if is_stream else "player", source]
    if start > 0:
        cmd += ["--at", clock(start)]
    return cmd


async def cast(
    source: str,
    protocol: str,
    host: str,
    name: str,
    start: float = 0.0,
    debug: bool = False,
    on_line: Callable[[str], None] = print,
) -> None:
    """Let castor play ``source`` on the TV, showing its output, until it finishes."""
    path = find()
    if path is None:
        raise UserError("castor is not installed.", INSTALL_HINT)
    cmd = cast_command(path, source, start)
    if debug:
        cmd.append("--debug")
    env = {**os.environ, **device_env(protocol, host, name)}
    _LOGGER.debug("castor: %s (device %s at %s)", cmd, protocol, host)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
    )
    tail: List[str] = []
    try:
        assert proc.stdout is not None
        while line := await proc.stdout.readline():
            text = line.decode(errors="replace").rstrip()
            tail = (tail + [text])[-5:]
            on_line(f"  castor: {text}")
        code = await proc.wait()
    except BaseException:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
        raise
    if code != 0:
        raise UserError(
            f"castor stopped with an error (exit status {code}).",
            "Its last words are above. Run again with -v to see castor's debug output.",
        )
