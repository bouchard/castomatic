"""Remembered settings, kept in ``~/.config/castomatic/config.json``.

Stored: the chosen TV (its name and where to find it), and which video codecs a TV was
seen not to show although its protocol allows them (learnt by trying; see
:mod:`castomatic.backends.chromecast`). Pairing credentials stay in pyatv's own storage and
are never copied here.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

from .errors import UserError

_LOGGER = logging.getLogger(__name__)
APP_DIR = "castomatic"
LEGACY_APP_DIR = "pyatv-cast"  # Castomatic's old name; its remembered TV is carried over


def _config_root() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) if base else Path.home() / ".config"


def config_path() -> Path:
    """Where the config file lives (honours ``XDG_CONFIG_HOME``)."""
    return _config_root() / APP_DIR / "config.json"


def legacy_config_path() -> Path:
    """Where pyatv-cast (Castomatic's former name) kept its config."""
    return _config_root() / LEGACY_APP_DIR / "config.json"


def is_first_run() -> bool:
    """True when neither Castomatic nor pyatv-cast has ever saved a config."""
    return not config_path().exists() and not legacy_config_path().exists()


def load() -> Dict[str, Any]:
    """Read the config; a missing or damaged file is treated as empty.

    The first time Castomatic runs after being renamed, a pyatv-cast config is read instead
    (and copied over on the next save), so the remembered TV is not lost.
    """
    path = config_path()
    if not path.exists() and legacy_config_path().exists():
        path = legacy_config_path()
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as ex:
        _LOGGER.warning("ignoring unreadable config %s: %s", path, ex)
        return {}


def saved_tv() -> Optional[Dict[str, Any]]:
    """The remembered TV, if any."""
    tv = load().get("tv")
    return tv if isinstance(tv, dict) and tv.get("host") else None


def save_tv(tv: Dict[str, Any]) -> None:
    """Remember ``tv`` (a dict with at least ``name`` and ``host``)."""
    path = config_path()
    data = load()
    data["tv"] = tv
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as ex:
        _LOGGER.warning("could not remember the TV in %s: %s", path, ex)


def _write(data: Dict[str, Any]) -> None:
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as ex:
        _LOGGER.warning("could not save %s: %s", path, ex)


def cannot_show(device: str) -> FrozenSet[str]:
    """Video codecs the TV identified by ``device`` was seen not to show."""
    entry = load().get("devices", {}).get(device, {})
    codecs = entry.get("cannot_show", []) if isinstance(entry, dict) else []
    return frozenset(c for c in codecs if isinstance(c, str))


def remember_cannot_show(device: str, codec: str) -> None:
    """Remember that the TV identified by ``device`` does not show ``codec`` video."""
    data = load()
    devices = data.setdefault("devices", {})
    if not isinstance(devices, dict):
        devices = data["devices"] = {}
    entry = devices.setdefault(device, {})
    if not isinstance(entry, dict):
        entry = devices[device] = {}
    entry["cannot_show"] = sorted(set(entry.get("cannot_show", [])) | {codec})
    _write(data)


def forget_tv() -> bool:
    """Remove the remembered TV. Returns whether there was one."""
    data = load()
    if "tv" not in data:
        return False
    del data["tv"]
    try:
        config_path().parent.mkdir(parents=True, exist_ok=True)
        config_path().write_text(json.dumps(data, indent=2) + "\n")
    except OSError as ex:
        raise UserError(f"could not update {config_path()}: {ex.strerror or ex}.") from ex
    return True
