"""User-facing error text and the remembered-TV file."""

import json

import pytest

from castomatic import config
from castomatic.errors import UPNP_HINTS, UserError


def test_error_renders_message_and_hint():
    err = UserError("it broke", "try this\nor this")
    assert err.render() == "error: it broke\n  hint: try this\n        or this"
    assert UserError("plain").render() == "error: plain"
    assert str(err) == "it broke"


def test_upnp_hints_cover_the_common_codes():
    assert "firewall" in UPNP_HINTS[716] and "busy" in UPNP_HINTS[705]


def test_config_lives_under_dot_config(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(config.Path, "home", lambda: tmp_path)
    assert config.config_path() == tmp_path / ".config" / "castomatic" / "config.json"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.config_path() == tmp_path / "xdg" / "castomatic" / "config.json"


def test_save_load_and_forget(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.saved_tv() is None and config.load() == {}
    assert config.forget_tv() is False

    config.save_tv({"name": "TV", "host": "10.0.0.2"})
    assert config.saved_tv() == {"name": "TV", "host": "10.0.0.2"}
    assert json.loads(config.config_path().read_text())["tv"]["host"] == "10.0.0.2"

    assert config.forget_tv() is True
    assert config.saved_tv() is None


def test_damaged_or_odd_config_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = config.config_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert config.load() == {}
    path.write_text("[1, 2]")
    assert config.load() == {}
    path.write_text(json.dumps({"tv": {"name": "no host"}}))
    assert config.saved_tv() is None


def test_unwritable_config_only_warns(monkeypatch, tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocker))  # cannot create a directory inside a file
    config.save_tv({"name": "TV", "host": "h"})
    assert "could not remember" in caplog.text


def test_forget_reports_an_unwritable_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.save_tv({"name": "TV", "host": "h", "location": "l", "airplay_id": None})

    def refuse(self, *args, **kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(config.Path, "write_text", refuse)
    with pytest.raises(UserError, match="could not update"):
        config.forget_tv()


def test_legacy_pyatv_cast_config_is_carried_over(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.is_first_run()
    legacy = tmp_path / "pyatv-cast" / "config.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"tv": {"name": "Old", "host": "10.0.0.9"}}))
    assert not config.is_first_run()
    assert config.saved_tv() == {"name": "Old", "host": "10.0.0.9"}
    config.save_tv({"name": "New", "host": "10.0.0.8"})
    assert json.loads(config.config_path().read_text())["tv"]["name"] == "New"
    assert json.loads(legacy.read_text())["tv"]["name"] == "Old"  # never modified


def test_forget_with_only_a_legacy_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    legacy = tmp_path / "pyatv-cast" / "config.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"tv": {"name": "Old", "host": "10.0.0.9"}}))
    assert config.forget_tv() is True
    assert config.saved_tv() is None
