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


@pytest.fixture(autouse=True)
def no_real_cast_discovery(monkeypatch):
    """Chromecast (mDNS) and Roku (SSDP) searches never touch the real network in tests.

    Tests of those searches patch in their own fakes on top of this.
    """
    from castomatic.backends import chromecast, roku

    async def none(*args, **kwargs):
        return []

    monkeypatch.setattr(chromecast, "discover", none)
    monkeypatch.setattr(roku, "discover", none)
