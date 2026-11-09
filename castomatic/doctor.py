"""``castomatic doctor``: check everything playing depends on, and say what to fix.

Every check prints one line, ``ok``, ``warn`` or ``FAIL``, and anything not ok comes with
the fix. Nothing is changed. ``--save FILE`` also writes the report to a file to attach to
a bug report; it holds no credentials, and TV and network details only as found.
"""

import argparse
import asyncio
import importlib.metadata
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

import aiohttp
from aiohttp import web

from castomatic import __version__, banner, config, dlna, stream, targets
from castomatic.engines import castor

OK, WARN, FAIL = "ok", "warn", "FAIL"
FIREWALL = "/usr/libexec/ApplicationFirewall/socketfilterfw"


@dataclass
class Report:
    """What the checks found."""

    lines: List[str] = field(default_factory=list)
    problems: int = 0
    warnings: int = 0

    def add(self, status: str, what: str, detail: str = "", fix: str = "") -> None:
        """Record and print one check."""
        mark = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}[status]
        line = f"[{mark}] {what}" + (f": {detail}" if detail else "")
        self.lines.append(line)
        print(line)
        if fix:
            for number, text in enumerate(fix.strip().splitlines()):
                hint = ("         fix: " if number == 0 else "              ") + text
                self.lines.append(hint)
                print(hint)
        if status == FAIL:
            self.problems += 1
        elif status == WARN:
            self.warnings += 1

    def section(self, title: str) -> None:
        """Start a group of checks."""
        self.lines.append(f"\n{title}")
        print(f"\n{title}")


def _first_line(cmd: List[str]) -> Optional[str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or out.stderr).strip().splitlines()
    return text[0] if text else None


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def check_tools(report: Report) -> None:
    """ffmpeg, ffprobe, the encoder they will use, and the Python libraries."""
    report.section("Tools")
    report.add(OK, "castomatic", f"{__version__} on Python {platform.python_version()} ({sys.platform})")
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            report.add(
                FAIL, tool, "not found on PATH",
                "Install ffmpeg: `brew install ffmpeg` on a Mac, or your package manager.",
            )  # fmt: skip
        else:
            report.add(OK, tool, _first_line([tool, "-version"]) or "found")
    if shutil.which("ffmpeg"):
        encoder = stream.video_encoder()
        kind = "hardware" if encoder != "libx264" else "software"
        report.add(OK, "video encoder for conversions", f"{encoder} ({kind})")
        if stream.can_burn_subtitles():
            report.add(OK, "drawing subtitles (--burn-subtitles)", "available")
        else:
            report.add(
                WARN, "drawing subtitles (--burn-subtitles)",
                "this ffmpeg has no subtitles filter; only needed for TVs that take no subtitle file",
                stream.BURN_HINT,
            )  # fmt: skip
    libs = ", ".join(
        f"{name} {_package_version(name)}"
        for name in ("pyatv", "catt", "pychromecast", "rokuecp", "yt-dlp", "aiohttp")
    )
    report.add(OK, "libraries", libs)


async def check_castor(report: Report, version: Callable = castor.version) -> None:
    """castor is optional: present, or how to get it."""
    found = await version()
    if found:
        report.add(OK, "castor (optional)", found)
    else:
        report.add(
            OK, "castor (optional)", "not installed; only needed for pages yt-dlp cannot read",
        )  # fmt: skip


def _firewall_state() -> Optional[str]:
    if sys.platform != "darwin" or not Path(FIREWALL).exists():
        return None
    return _first_line([FIREWALL, "--getglobalstate"])


async def _serves_on(address: str) -> Optional[str]:
    """Start a tiny server on ``address`` and fetch from it; None if that worked."""

    async def hello(request: web.Request) -> web.Response:
        return web.Response(text="castomatic")

    app = web.Application()
    app.router.add_get("/", hello)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, address, 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://{address}:{port}/", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                return None if await resp.text() == "castomatic" else f"HTTP {resp.status}"
    except (OSError, aiohttp.ClientError, asyncio.TimeoutError) as ex:
        return str(ex) or type(ex).__name__
    finally:
        await runner.cleanup()


async def check_network(report: Report) -> List[str]:
    """This computer's addresses, VPNs, the firewall, and serving media from here."""
    report.section("Network")
    addresses = dlna.local_addresses()
    if not addresses:
        report.add(FAIL, "network", "this computer has no network address", "Connect to the TV's Wi-Fi or network.")
        return addresses
    report.add(OK, "addresses", ", ".join(addresses))
    if len(addresses) > 1:
        report.add(
            WARN, "several network interfaces",
            "a VPN, tunnel or second network can hide TVs or send the TV the wrong address",
            "If no TV is found, or the TV never fetches the video, try with the VPN off.",
        )  # fmt: skip
    for address in addresses:
        problem = await _serves_on(address)
        if problem:
            report.add(
                WARN, f"serving video from {address}", f"this computer could not fetch from itself ({problem})",
                "TVs fetch the video from this computer, so this may stop playback. Usual causes: "
                "the firewall's stealth mode,\na VPN that captures local traffic, or security "
                "software. If a TV then plays fine, ignore this.",
            )  # fmt: skip
        else:
            report.add(OK, f"serving video from {address}", "works")
    state = _firewall_state()
    if state and "enabled" in state.lower():
        report.add(
            WARN, "macOS firewall", "on",
            "When macOS asks whether Python may accept incoming connections, choose Allow: "
            "TVs fetch the video from this computer.\nIf a TV never fetches it: System "
            "Settings > Network > Firewall > Options, and allow Python.",
        )  # fmt: skip
    elif state:
        report.add(OK, "macOS firewall", "off")
    return addresses


async def _roku_access(host: str) -> Optional[str]:
    """None if the Roku answers control requests, else what went wrong."""
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(
                f"http://{host}:8060/query/device-info", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                return None if resp.status == 200 else f"HTTP {resp.status}"
    except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
        return str(ex) or type(ex).__name__


async def check_tvs(report: Report, credentials: Optional[str] = None) -> List[targets.TV]:
    """Search with every protocol, show what each found, and what each TV needs."""
    from castomatic.backends import roku

    report.section("Looking for TVs (all protocols at once) ...")
    tvs = await targets.discover_tvs(credentials)
    for scan in targets.LAST_SCAN:
        label = targets.LABELS.get(scan.protocol, scan.protocol)
        detail = f"{scan.found} found in {scan.seconds:.1f}s"
        if scan.error:
            report.add(WARN, f"{label} search", f"{detail}; {scan.error}")
        else:
            report.add(OK, f"{label} search", detail)
    if not tvs:
        error = targets.no_tv_error([], None)
        hint = "\n".join(line for line in (error.hint or "").splitlines() if "castomatic doctor" not in line)
        report.add(FAIL, "TVs", "none found", hint)
        return tvs
    saved = config.saved_tv()
    report.section(f"TVs ({len(tvs)})")
    for tv in tvs:
        mine = "  <- your TV" if saved and targets.matches(tv, saved["host"]) else ""
        report.add(OK, tv.name, f"{tv.host}, {', '.join(tv.protocols)}{mine}")
        if tv.airplay and not tv.airplay.credentials:
            others = [p for p in ("chromecast", "dlna", "roku") if tv.offers(p)]
            status = OK if others else WARN
            report.add(
                status, f"  {tv.name}: AirPlay", "not paired"
                + (" (not needed: it is reached another way)" if others else ""),
                "" if others else targets._pairing_hint(tv),  # noqa: SLF001
            )  # fmt: skip
        if tv.roku:
            problem = await _roku_access(tv.host)
            if problem:
                report.add(WARN, f"  {tv.name}: Roku control", problem, roku.ECP_HINT)
    return tvs


async def run(args: argparse.Namespace) -> int:
    """Run every check. Returns the exit status: 0 if nothing failed."""
    banner.show()
    report = Report()
    check_tools(report)
    await check_castor(report)
    await check_network(report)
    await check_tvs(report, getattr(args, "credentials", None))
    print()
    if report.problems:
        summary = f"{report.problems} problem(s) to fix, {report.warnings} warning(s). See the fixes above."
    elif report.warnings:
        summary = f"Ready to cast, with {report.warnings} warning(s) worth reading."
    else:
        summary = "Everything looks good. Try: castomatic movie.mkv"
    print(summary)
    report.lines.append("\n" + summary)
    if getattr(args, "save", None):
        Path(args.save).write_text("\n".join(report.lines) + "\n")
        print(f"Report saved to {args.save}")
    return 1 if report.problems else 0


def build_parser() -> argparse.ArgumentParser:
    """``castomatic doctor``'s options."""
    parser = argparse.ArgumentParser(
        prog="castomatic doctor",
        description="Check everything castomatic depends on, and say what to fix.",
    )
    parser.add_argument("--save", metavar="FILE", help="also write the report to FILE")
    parser.add_argument("--credentials", help="AirPlay credentials, overriding the stored ones")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="-v decisions, -vv everything")
    return parser
