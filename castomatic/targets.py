"""Finding the TV: discovery, matching what the user typed, asking once, remembering.

DLNA renderers (SSDP) and AirPlay receivers (pyatv) found at the same address are one TV.
"""

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

import pyatv
from pyatv.auth.hap_pairing import HapCredentials, parse_credentials
from pyatv.const import Protocol
from pyatv.storage.file_storage import FileStorage

from castomatic import config, dlna
from castomatic.errors import UserError

_LOGGER = logging.getLogger(__name__)
DLNA_TIMEOUT = 3.0
AIRPLAY_TIMEOUT = 5.0


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
    """One television, reachable over DLNA, AirPlay or both."""

    name: str
    host: str
    renderer: Optional[dlna.Renderer] = None
    airplay: Optional[AirPlayInfo] = None

    @property
    def protocols(self) -> List[str]:
        """Human-readable list of how it can be reached."""
        found = []
        if self.renderer:
            found.append("DLNA")
        if self.airplay:
            found.append("AirPlay" if self.airplay.credentials else "AirPlay (not paired)")
        return found

    def label(self) -> str:
        """``Name (address) [protocols]``."""
        return f"{self.name} ({self.host}) [{', '.join(self.protocols)}]"


@dataclass
class Target:
    """The TV and the protocol chosen to talk to it."""

    tv: TV
    protocol: str  # "dlna" or "airplay"


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


async def discover_tvs(credentials: Optional[str] = None) -> List[TV]:
    """Find every TV on the network, merging DLNA and AirPlay by address."""
    renderers, airplay = await asyncio.gather(
        dlna.discover_renderers(DLNA_TIMEOUT), _scan_airplay(credentials)
    )
    mine = set(dlna.local_addresses())  # this machine's own AirPlay Receiver is not a TV
    renderers = [r for r in renderers if r.host not in mine]
    airplay = [a for a in airplay if a.address not in mine]
    tvs = {}
    for renderer in renderers:
        tvs[renderer.host] = TV(renderer.friendly_name or renderer.host, renderer.host, renderer)
    for info in airplay:
        tv = tvs.setdefault(info.address, TV(info.name or info.address, info.address))
        tv.airplay = info
    return sorted(tvs.values(), key=lambda t: t.name.lower())


def matches(tv: TV, spec: str) -> bool:
    """Whether what the user typed (address, name or id) refers to ``tv``."""
    needle = spec.strip().lower()
    return (
        needle == tv.host
        or needle in tv.name.lower()
        or (tv.airplay is not None and needle == tv.airplay.identifier.lower())
        or (tv.renderer is not None and needle == tv.renderer.location.lower())
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
        }
    )
    print(f"Remembered {tv.name} as your TV. Use --choose to pick a different one.")


def no_tv_error(found: List[TV], spec: Optional[str]) -> UserError:
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
    ]
    if len(addresses) > 1:
        hints.append(
            "This computer has several network interfaces "
            f"({', '.join(addresses)}). A VPN or tunnel can hide the TV; try turning it off."
        )
    return UserError(message, "\n".join(hints))


def _pick_protocol(tv: TV, wanted: str) -> Target:
    if wanted in ("auto", "dlna") and tv.renderer:
        return Target(tv, "dlna")
    if wanted in ("auto", "airplay") and tv.airplay:
        if tv.airplay.credentials:
            return Target(tv, "airplay")
        raise UserError(
            f"{tv.name} supports AirPlay, but this computer is not paired with it.",
            f"Pair once: `atvremote --id {tv.airplay.identifier} --protocol airplay pair` "
            "and enter the PIN shown on the TV. DLNA needs no pairing, but this TV did "
            "not answer as a DLNA renderer.",
        )
    if wanted == "dlna":
        raise UserError(
            f"{tv.name} did not answer as a DLNA renderer.",
            "Try `--protocol airplay` (needs one-time pairing), and check the TV's "
            "settings for a DLNA / media sharing option.",
        )
    if wanted == "airplay":
        raise UserError(
            f"{tv.name} does not offer AirPlay.",
            "Try `--protocol dlna`, or leave the protocol on its default.",
        )
    raise UserError(f"{tv.name} offers neither DLNA nor AirPlay.")


async def _saved_fast_path(saved: dict, wanted: str) -> Optional[Target]:
    """Reuse the remembered TV without a network-wide search, when its address still works."""
    if wanted == "airplay" or not saved.get("location"):
        return None
    try:
        renderer = await dlna.find_renderer(location=saved["location"])
    except dlna.DlnaError:
        return None
    return Target(TV(saved.get("name") or renderer.friendly_name, renderer.host, renderer), "dlna")


async def resolve_target(
    spec: Optional[str] = None,
    protocol: str = "auto",
    choose: bool = False,
    credentials: Optional[str] = None,
    interactive: Optional[bool] = None,
    ask: Callable[[str], str] = input,
) -> Target:
    """Work out which TV to use and how to talk to it."""
    if interactive is None:
        interactive = sys.stdin.isatty()
    saved = None if choose else config.saved_tv()

    if not spec and saved:
        fast = await _saved_fast_path(saved, protocol)
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
    target = _pick_protocol(tv, protocol)
    if remember:
        _remember(tv)
    return target


async def list_tvs(credentials: Optional[str] = None) -> List[TV]:
    """Discover TVs for ``--list``."""
    return await discover_tvs(credentials)
