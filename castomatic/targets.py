"""Finding the TV: discovery, matching what the user typed, asking once, remembering.

Every protocol is searched for at the same time: DLNA renderers and Roku devices (SSDP),
Chromecasts (mDNS) and AirPlay receivers (pyatv). Whatever answers from the same address
is one TV. Each search has its own time limit and its own error handling, so one that fails
never hides what the others found; :data:`LAST_SCAN` keeps what each one did, for
``castomatic doctor`` and ``-v``.

When a TV can be reached in several ways, :func:`candidate_targets` puts them in order of
how reliably they play (see :data:`PROTOCOL_ORDER`), and the player tries the next one if
the first does not work.
"""

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import pyatv
from pyatv.auth.hap_pairing import HapCredentials, parse_credentials
from pyatv.const import Protocol
from pyatv.storage.file_storage import FileStorage

from castomatic import config, dlna
from castomatic.backends import chromecast as cast_backend
from castomatic.backends import roku as roku_backend
from castomatic.errors import UserError

_LOGGER = logging.getLogger(__name__)
DLNA_TIMEOUT = 3.0
AIRPLAY_TIMEOUT = 5.0
CHROMECAST_TIMEOUT = 4.0
ROKU_TIMEOUT = 3.0

PROTOCOLS = ("chromecast", "dlna", "roku", "airplay")
# Most reliable first, when the same TV offers several and the video plays as it is on
# each. The reasons, protocol by protocol:
# * Chromecast: the TV reports exactly what it is doing, seeks, and takes subtitles.
# * DLNA: no pairing, and the TV lists what it plays; but renderers vary in how well
#   they follow the standard.
# * Roku: reliable, but cannot be told where to start and takes no subtitle file.
# * AirPlay: needs one-time pairing and only takes HLS, so everything is repackaged.
PROTOCOL_ORDER = {name: rank for rank, name in enumerate(PROTOCOLS)}
LABELS = {"chromecast": "Chromecast", "dlna": "DLNA", "roku": "Roku", "airplay": "AirPlay"}


@dataclass
class AirPlayInfo:
    """An AirPlay receiver and, if this machine is paired with it, the credentials."""

    identifier: str
    address: str
    port: int
    credentials: Optional[HapCredentials]
    name: str = ""


@dataclass
class TV:
    """One television, reachable in one or more ways."""

    name: str
    host: str
    renderer: Optional[dlna.Renderer] = None
    airplay: Optional[AirPlayInfo] = None
    chromecast: Optional[cast_backend.ChromecastInfo] = None
    roku: Optional[roku_backend.RokuInfo] = None

    def offers(self, protocol: str) -> bool:
        """Whether the TV answered on ``protocol``."""
        return self._endpoint(protocol) is not None

    def _endpoint(self, protocol: str) -> Any:
        return {
            "dlna": self.renderer, "airplay": self.airplay,
            "chromecast": self.chromecast, "roku": self.roku,
        }.get(protocol)  # fmt: skip

    @property
    def protocols(self) -> List[str]:
        """Human-readable list of how it can be reached, most reliable first."""
        found = []
        for name in PROTOCOLS:
            if not self.offers(name):
                continue
            if name == "airplay" and not self.airplay.credentials:
                found.append("AirPlay (not paired)")
            else:
                found.append(LABELS[name])
        return found

    def label(self) -> str:
        """``Name (address) [protocols]``."""
        return f"{self.name} ({self.host}) [{', '.join(self.protocols)}]"


@dataclass
class Target:
    """The TV and the protocol chosen to talk to it."""

    tv: TV
    protocol: str  # one of PROTOCOLS


@dataclass
class ScanResult:
    """What one protocol's search did."""

    protocol: str
    found: int = 0
    seconds: float = 0.0
    error: Optional[str] = None


LAST_SCAN: List[ScanResult] = []


# -- discovery ------------------------------------------------------------------------------


async def _scan_airplay(credentials_override: Optional[str]) -> List[AirPlayInfo]:
    loop = asyncio.get_running_loop()
    try:
        storage = FileStorage.default_storage(loop)
        await storage.load()
        configs = await pyatv.scan(loop, timeout=AIRPLAY_TIMEOUT, storage=storage)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.debug("AirPlay scan failed: %s", ex)
        return []
    found = []
    for conf in configs:
        service = conf.get_service(Protocol.AirPlay)
        if service is None:
            continue
        creds = credentials_override
        if not creds:
            settings = await storage.get_settings(conf)
            creds = settings.protocols.airplay.credentials
        found.append(
            AirPlayInfo(
                conf.identifier, str(conf.address), service.port,
                parse_credentials(creds) if creds else None, conf.name,
            )
        )  # fmt: skip
    return found


async def _scan_chromecast() -> List[cast_backend.ChromecastInfo]:
    return await cast_backend.discover(CHROMECAST_TIMEOUT)


async def _scan_roku() -> List[roku_backend.RokuInfo]:
    return await roku_backend.discover(ROKU_TIMEOUT)


async def _timed(protocol: str, search: "asyncio.Future[List[Any]]", limit: float) -> List[Any]:
    """Run one search, recording how it went; a failure or overrun means "none found"."""
    began = time.monotonic()
    result = ScanResult(protocol)
    try:
        found = list(await asyncio.wait_for(search, limit))
    except asyncio.TimeoutError:
        found, result.error = [], f"no answer within {limit:.0f}s"
    except Exception as ex:  # pylint: disable=broad-except
        found, result.error = [], f"{type(ex).__name__}: {ex}"
    result.found, result.seconds = len(found), time.monotonic() - began
    if result.error:
        _LOGGER.debug("%s search failed: %s", protocol, result.error)
    LAST_SCAN.append(result)
    return found


async def discover_tvs(credentials: Optional[str] = None) -> List[TV]:
    """Find every TV on the network, merging what answers from the same address."""
    LAST_SCAN.clear()
    slack = 4.0  # beyond each protocol's own timeout, for answering slowly
    renderers, airplay, casts, rokus = await asyncio.gather(
        _timed("dlna", dlna.discover_renderers(DLNA_TIMEOUT), DLNA_TIMEOUT + slack),
        _timed("airplay", _scan_airplay(credentials), AIRPLAY_TIMEOUT + slack),
        _timed("chromecast", _scan_chromecast(), CHROMECAST_TIMEOUT + slack),
        _timed("roku", _scan_roku(), ROKU_TIMEOUT + slack),
    )
    mine = set(dlna.local_addresses())  # this machine's own receivers are not TVs
    tvs: Dict[str, TV] = {}

    def tv_at(host: str, name: str) -> TV:
        return tvs.setdefault(host, TV(name or host, host))

    for renderer in renderers:
        if renderer.host not in mine:
            tv_at(renderer.host, renderer.friendly_name).renderer = renderer
    for cast in casts:
        if cast.host not in mine:
            tv_at(cast.host, cast.name).chromecast = cast
    for device in rokus:
        if device.host not in mine:
            tv_at(device.host, device.name).roku = device
    for info in airplay:
        if info.address not in mine:
            tv_at(info.address, info.name).airplay = info
    return sorted(tvs.values(), key=lambda t: t.name.lower())


def matches(tv: TV, spec: str) -> bool:
    """Whether what the user typed (address, name or id) refers to ``tv``."""
    needle = spec.strip().lower()
    return (
        needle == tv.host
        or needle in tv.name.lower()
        or (tv.airplay is not None and needle == tv.airplay.identifier.lower())
        or (tv.renderer is not None and needle == tv.renderer.location.lower())
        or (tv.chromecast is not None and bool(tv.chromecast.uuid) and needle == tv.chromecast.uuid.lower())
        or (tv.roku is not None and bool(tv.roku.serial) and needle == tv.roku.serial.lower())
    )


def ask_which(tvs: List[TV], ask: Callable[[str], str] = input) -> TV:
    """Let the user pick one of several TVs."""
    print("More than one TV was found:")
    for number, tv in enumerate(tvs, 1):
        print(f"  {number}) {tv.label()}")
    for _ in range(3):
        answer = ask(f"Which one? [1-{len(tvs)}] ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(tvs):
            return tvs[int(answer) - 1]
        print(f"Please type a number from 1 to {len(tvs)}.")
    raise UserError("no TV chosen.", "Run again and pick a number, or use `--tv NAME`.")


def _remember(tv: TV) -> None:
    config.save_tv(
        {
            "name": tv.name,
            "host": tv.host,
            "location": tv.renderer.location if tv.renderer else None,
            "airplay_id": tv.airplay.identifier if tv.airplay else None,
            "chromecast": bool(tv.chromecast),
            "roku": bool(tv.roku),
        }
    )
    print(f"Remembered {tv.name} as your TV. Use --choose to pick a different one.")


def no_tv_error(found: List[TV], spec: Optional[str]) -> UserError:
    """Explain an empty or unmatched search, with what to check."""
    addresses = dlna.local_addresses()
    if spec:
        seen = ", ".join(f"{tv.name} ({tv.host})" for tv in found) or "none"
        message = f"no TV matching '{spec}' was found. TVs seen: {seen}."
    else:
        message = "no TV was found on the network."
    hints = [
        "Is the TV on and awake? Many TVs stop answering discovery in standby.",
        "Is this computer on the same Wi-Fi or network as the TV?",
        "If you know the TV's address, give it: `--tv 192.168.1.50`.",
        "Run `castomatic doctor` to see what each kind of search found.",
    ]
    if len(addresses) > 1:
        hints.append(
            "This computer has several network interfaces "
            f"({', '.join(addresses)}). A VPN or tunnel can hide the TV; try turning it off."
        )
    if sys.platform == "darwin":
        hints.append(
            "On macOS, the terminal app needs Local Network access: System Settings > "
            "Privacy & Security > Local Network."
        )
    return UserError(message, "\n".join(hints))


def _pairing_hint(tv: TV) -> str:
    return (
        f"Pair once: `atvremote --id {tv.airplay.identifier} --protocol airplay pair` "
        "and enter the PIN shown on the TV."
    )


def candidate_targets(tv: TV, wanted: str = "auto") -> List[Target]:
    """Every usable way to reach ``tv``, most reliable first.

    ``wanted`` is a protocol name, or ``auto`` for all of them. An unpaired AirPlay
    receiver is left out (with an error saying how to pair, if nothing else is left).
    """
    if wanted != "auto":
        return [_pick_protocol(tv, wanted)]
    found = [
        Target(tv, name) for name in PROTOCOLS
        if tv.offers(name) and (name != "airplay" or tv.airplay.credentials)
    ]  # fmt: skip
    if found:
        return found
    return [_pick_protocol(tv, "auto")]  # raises, explaining why


def _pick_protocol(tv: TV, wanted: str) -> Target:
    if wanted == "auto":
        for name in PROTOCOLS:
            if tv.offers(name) and (name != "airplay" or tv.airplay.credentials):
                return Target(tv, name)
    elif tv.offers(wanted) and (wanted != "airplay" or tv.airplay.credentials):
        return Target(tv, wanted)
    if wanted in ("auto", "airplay") and tv.airplay:
        others = "" if wanted == "airplay" else (
            " Nothing else answered from this TV: no DLNA renderer, Chromecast or Roku."
        )
        raise UserError(
            f"{tv.name} supports AirPlay, but this computer is not paired with it.",
            _pairing_hint(tv) + others,
        )
    if wanted == "dlna":
        raise UserError(
            f"{tv.name} did not answer as a DLNA renderer.",
            "Leave out --protocol to use whatever the TV offers, and check the TV's "
            "settings for a DLNA / media sharing option.",
        )
    if wanted in ("airplay", "chromecast", "roku"):
        raise UserError(
            f"{tv.name} does not offer {LABELS[wanted]}.",
            f"It offers: {', '.join(tv.protocols) or 'nothing usable'}. Leave out "
            "--protocol to use whatever the TV offers.",
        )
    raise UserError(f"{tv.name} offers no way to play video that castomatic knows.")


async def _saved_fast_path(saved: dict, wanted: str) -> Optional[Target]:
    """Reuse the remembered TV without a network-wide search, when its address still works."""
    targets = await _saved_fast_targets(saved, wanted)
    return targets[0] if targets else None


async def _saved_fast_targets(saved: dict, wanted: str) -> List[Target]:
    """Every remembered way to reach the saved TV that still answers, without searching.

    AirPlay is never on this path: its port changes between sessions, so it needs a scan.
    """
    if wanted == "airplay":
        return []
    host = saved["host"]
    tv = TV(saved.get("name") or host, host)
    if saved.get("location") and wanted in ("auto", "dlna"):
        try:
            tv.renderer = await dlna.find_renderer(location=saved["location"])
            tv.name = saved.get("name") or tv.renderer.friendly_name
            tv.host = tv.renderer.host
        except dlna.DlnaError:
            pass
    if saved.get("chromecast") and wanted in ("auto", "chromecast"):
        tv.chromecast = cast_backend.ChromecastInfo(tv.name, host)
    if saved.get("roku") and wanted in ("auto", "roku"):
        tv.roku = roku_backend.RokuInfo(tv.name, host)
    if not (tv.renderer or tv.chromecast or tv.roku):
        return []
    return candidate_targets(tv, wanted)


async def resolve_targets(
    spec: Optional[str] = None,
    protocol: str = "auto",
    choose: bool = False,
    credentials: Optional[str] = None,
    interactive: Optional[bool] = None,
    ask: Callable[[str], str] = input,
) -> List[Target]:
    """Work out which TV to use, and every way to talk to it, most reliable first."""
    if interactive is None:
        interactive = sys.stdin.isatty()
    saved = None if choose else config.saved_tv()

    if not spec and saved:
        fast = await _saved_fast_targets(saved, protocol)
        if fast:
            return fast

    tvs = await discover_tvs(credentials)
    _LOGGER.debug("found %d TV(s): %s", len(tvs), [tv.label() for tv in tvs])

    if spec:
        candidates = [tv for tv in tvs if matches(tv, spec)]
        if not candidates and spec.startswith(("http://", "https://")):
            try:
                renderer = await dlna.find_renderer(location=spec)
                candidates = [TV(renderer.friendly_name or renderer.host, renderer.host, renderer)]
            except dlna.DlnaError:
                pass
        if not candidates:
            raise no_tv_error(tvs, spec)
        remember = False
    elif saved:
        candidates = [tv for tv in tvs if matches(tv, saved["host"])]
        if not candidates:
            raise UserError(
                f"your TV '{saved.get('name', saved['host'])}' ({saved['host']}) was not found.",
                "Turn it on and make sure it is on this network, or pick another with "
                "`--choose`. Other TVs seen: "
                + (", ".join(f"{t.name} ({t.host})" for t in tvs) or "none"),
            )
        remember = False
    else:
        candidates = tvs
        remember = True

    if not candidates:
        raise no_tv_error(tvs, spec)
    if len(candidates) == 1:
        tv = candidates[0]
    elif interactive:
        tv = ask_which(candidates, ask)
    else:
        raise UserError(
            f"{len(candidates)} TVs were found and I cannot ask which one to use here.",
            "Choose one with `--tv`, for example:\n"
            + "\n".join(f"  --tv '{t.name}'    ({t.host})" for t in candidates),
        )
    found = candidate_targets(tv, protocol)
    if remember:
        _remember(tv)
    return found


async def resolve_target(*args: Any, **kwargs: Any) -> Target:
    """The most reliable way to reach the TV (see :func:`resolve_targets`)."""
    return (await resolve_targets(*args, **kwargs))[0]


async def list_tvs(credentials: Optional[str] = None) -> List[TV]:
    """Discover TVs for ``--list``."""
    return await discover_tvs(credentials)
