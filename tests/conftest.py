import tempfile

import pytest

from castomatic import cache


@pytest.fixture(autouse=True)
def private_cache(monkeypatch, tmp_path_factory):
    """Keep yt-dlp downloads made by tests out of the real cache."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("tmp")))
    monkeypatch.setattr(cache, "default_cache_root", lambda: tmp_path_factory.mktemp("copies"))


@pytest.fixture(autouse=True)
def private_config(monkeypatch, tmp_path_factory):
    """Never read or write the real ~/.config during tests."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("config")))
