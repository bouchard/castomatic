"""Finding, choosing and remembering the TV."""

from types import SimpleNamespace

import pytest
from pyatv.const import Protocol

from castomatic import config, dlna, targets
from castomatic.errors import UserError
from castomatic.targets import TV, AirPlayInfo


def renderer(host="10.0.0.5", name="Living Room TV"):
    return dlna.Renderer(f"http://{host}:9197/dmr", name, "uuid:1", f"http://{host}:9197/ctl")


def airplay(host="10.0.0.5", paired=True, identifier="AA:BB:CC"):
    return AirPlayInfo(identifier, host, 7000, object() if paired else None)


def tv(host="10.0.0.5", name="Living Room TV", dlna_on=True, air=None):
    return TV(name, host, renderer(host, name) if dlna_on else None, air)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(dlna, "local_addresses", lambda: ["10.0.0.99"])


def _discover(monkeypatch, *found):
    async def discover(credentials=None):
        return list(found)

    monkeypatch.setattr(targets, "discover_tvs", discover)


def test_labels_and_protocols():
    assert tv().label() == "Living Room TV (10.0.0.5) [DLNA]"
    both = tv(air=airplay())
    assert both.protocols == ["DLNA", "AirPlay"]
    assert tv(dlna_on=False, air=airplay(paired=False)).protocols == ["AirPlay (not paired)"]


@pytest.mark.parametrize("spec", ["10.0.0.5", "living", " LIVING room ", "aa:bb:cc", "http://10.0.0.5:9197/dmr"])
def test_matches_address_name_id_and_location(spec):
    assert targets.matches(tv(air=airplay()), spec)


def test_matches_rejects_other_tvs():
    assert not targets.matches(tv(air=airplay()), "bedroom")
    assert not targets.matches(tv(dlna_on=False), "http://10.0.0.5:9197/dmr")


def test_ask_which_retries_then_gives_up(capsys):
    tvs = [tv("10.0.0.5", "One"), tv("10.0.0.6", "Two")]
    answers = iter(["", "7", "2"])
    assert targets.ask_which(tvs, lambda prompt: next(answers)).name == "Two"
    assert "1) One" in capsys.readouterr().out
    with pytest.raises(UserError, match="no TV chosen"):
        targets.ask_which(tvs, lambda prompt: "nope")


async def test_discover_merges_dlna_and_airplay_and_skips_this_machine(monkeypatch):
    async def renderers(timeout):
        return [renderer("10.0.0.5", "Samsung"), renderer("10.0.0.99", "My Mac")]

    async def scan(credentials):
        info = airplay("10.0.0.5")
        info.name = "Samsung AirPlay"
        other = airplay("10.0.0.7", identifier="ZZ")
        other.name = "Apple TV"
        mac = airplay("10.0.0.99")
        mac.name = "My Mac"
        return [info, other, mac]

    monkeypatch.setattr(dlna, "discover_renderers", renderers)
    monkeypatch.setattr(targets, "_scan_airplay", scan)
    found = await targets.discover_tvs()
    assert [(t.name, t.host, t.protocols) for t in found] == [
        ("Apple TV", "10.0.0.7", ["AirPlay"]),
        ("Samsung", "10.0.0.5", ["DLNA", "AirPlay"]),
    ]
    assert await targets.list_tvs() is not None


async def test_scan_airplay_reads_names_and_credentials(monkeypatch):
    class Storage:
        async def load(self):
            pass

        async def get_settings(self, conf):
            return SimpleNamespace(protocols=SimpleNamespace(airplay=SimpleNamespace(credentials=conf.creds)))

    class Conf:
        def __init__(self, name, address, creds, service):
            self.name, self.address, self.creds, self.identifier = name, address, creds, "ID-" + name
            self._service = service

        def get_service(self, protocol):
            assert protocol == Protocol.AirPlay
            return self._service

    configs = [
        Conf("Paired", "10.0.0.5", "creds", SimpleNamespace(port=7000)),
        Conf("Unpaired", "10.0.0.6", None, SimpleNamespace(port=7001)),
        Conf("NoAirPlay", "10.0.0.7", None, None),
    ]

    async def scan(loop, **kwargs):
        return configs

    monkeypatch.setattr(targets.pyatv, "scan", scan)
    monkeypatch.setattr(targets.FileStorage, "default_storage", staticmethod(lambda loop: Storage()))
    monkeypatch.setattr(targets, "parse_credentials", lambda text: f"parsed:{text}")
    found = await targets._scan_airplay(None)
    assert [(i.name, i.address, i.port, i.credentials) for i in found] == [
        ("Paired", "10.0.0.5", 7000, "parsed:creds"),
        ("Unpaired", "10.0.0.6", 7001, None),
    ]
    override = await targets._scan_airplay("given")
    assert override[1].credentials == "parsed:given"


async def test_scan_airplay_failure_means_none_found(monkeypatch):
    async def scan(loop, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr(targets.pyatv, "scan", scan)
    monkeypatch.setattr(targets.FileStorage, "default_storage", staticmethod(lambda loop: SimpleNamespace(load=_noop)))
    assert await targets._scan_airplay(None) == []


async def _noop():
    return None


async def test_single_tv_is_used_and_remembered(monkeypatch, capsys):
    _discover(monkeypatch, tv())
    target = await targets.resolve_target(interactive=False)
    assert (target.protocol, target.tv.host) == ("dlna", "10.0.0.5")
    saved = config.saved_tv()
    assert saved["host"] == "10.0.0.5" and saved["location"] == "http://10.0.0.5:9197/dmr"
    assert "Remembered Living Room TV" in capsys.readouterr().out


async def test_several_tvs_ask_once_and_remember_the_answer(monkeypatch):
    _discover(monkeypatch, tv("10.0.0.5", "One"), tv("10.0.0.6", "Two"))
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return "2"

    first = await targets.resolve_target(interactive=True, ask=ask)
    assert first.tv.name == "Two" and len(asked) == 1 and config.saved_tv()["host"] == "10.0.0.6"

    async def fast(location):
        assert location == "http://10.0.0.6:9197/dmr"
        return renderer("10.0.0.6", "Two")

    async def no_discovery(credentials=None):
        raise AssertionError("a remembered TV must not trigger a network search")

    monkeypatch.setattr(dlna, "find_renderer", lambda location: fast(location))
    monkeypatch.setattr(targets, "discover_tvs", no_discovery)
    second = await targets.resolve_target(interactive=True, ask=ask)
    assert second.tv.host == "10.0.0.6" and len(asked) == 1  # not asked again


async def test_several_tvs_without_a_terminal_list_the_options(monkeypatch):
    _discover(monkeypatch, tv("10.0.0.5", "One"), tv("10.0.0.6", "Two"))
    with pytest.raises(UserError, match="2 TVs were found") as info:
        await targets.resolve_target(interactive=False)
    assert "--tv 'One'" in info.value.hint and "--tv 'Two'" in info.value.hint
    assert config.saved_tv() is None


async def test_remembered_tv_found_by_search_when_its_address_stopped_answering(monkeypatch):
    config.save_tv({"name": "Two", "host": "10.0.0.6", "location": "http://10.0.0.6:9197/dmr"})

    async def dead(location):
        raise dlna.DlnaError("unreachable")

    monkeypatch.setattr(dlna, "find_renderer", dead)
    _discover(monkeypatch, tv("10.0.0.5", "One"), tv("10.0.0.6", "Two"))
    assert (await targets.resolve_target(interactive=False)).tv.name == "Two"


async def test_remembered_tv_missing_does_not_silently_switch(monkeypatch):
    config.save_tv({"name": "Two", "host": "10.0.0.6"})
    _discover(monkeypatch, tv("10.0.0.5", "One"))
    with pytest.raises(UserError, match="'Two' .* was not found") as info:
        await targets.resolve_target(interactive=False)
    assert "--choose" in info.value.hint and "One (10.0.0.5)" in info.value.hint


async def test_choose_ignores_the_remembered_tv(monkeypatch):
    config.save_tv({"name": "One", "host": "10.0.0.5", "location": "http://10.0.0.5:9197/dmr"})
    _discover(monkeypatch, tv("10.0.0.5", "One"), tv("10.0.0.6", "Two"))
    target = await targets.resolve_target(choose=True, interactive=True, ask=lambda prompt: "2")
    assert target.tv.name == "Two" and config.saved_tv()["host"] == "10.0.0.6"


async def test_explicit_tv_matches_by_name_and_is_not_remembered(monkeypatch):
    _discover(monkeypatch, tv("10.0.0.5", "One"), tv("10.0.0.6", "Two"))
    target = await targets.resolve_target("two", interactive=False)
    assert target.tv.name == "Two" and config.saved_tv() is None


async def test_explicit_tv_with_several_matches_asks(monkeypatch):
    _discover(monkeypatch, tv("10.0.0.5", "Samsung A"), tv("10.0.0.6", "Samsung B"))
    target = await targets.resolve_target("samsung", interactive=True, ask=lambda prompt: "1")
    assert target.tv.name == "Samsung A"


async def test_explicit_unknown_tv_lists_what_was_seen(monkeypatch):
    _discover(monkeypatch, tv("10.0.0.5", "One"))
    with pytest.raises(UserError, match=r"no TV matching 'kitchen'.*One \(10.0.0.5\)") as info:
        await targets.resolve_target("kitchen", interactive=False)
    assert "same Wi-Fi" in info.value.hint


async def test_explicit_description_url_works_without_discovery_hits(monkeypatch):
    _discover(monkeypatch)

    async def find(location):
        return renderer("10.0.0.8", "Direct")

    monkeypatch.setattr(dlna, "find_renderer", find)
    target = await targets.resolve_target("http://10.0.0.8:9197/dmr", interactive=False)
    assert target.tv.name == "Direct"

    async def dead(location):
        raise dlna.DlnaError("no")

    monkeypatch.setattr(dlna, "find_renderer", dead)
    with pytest.raises(UserError, match="no TV matching"):
        await targets.resolve_target("http://10.0.0.8:9197/dmr", interactive=False)


async def test_no_tvs_at_all_mentions_vpns_when_there_are_several_interfaces(monkeypatch):
    _discover(monkeypatch)
    monkeypatch.setattr(dlna, "local_addresses", lambda: ["10.0.0.9", "100.64.0.1"])
    with pytest.raises(UserError, match="no TV was found") as info:
        await targets.resolve_target(interactive=False)
    assert "VPN" in info.value.hint and "100.64.0.1" in info.value.hint
    monkeypatch.setattr(dlna, "local_addresses", lambda: ["10.0.0.9"])
    with pytest.raises(UserError) as single:
        await targets.resolve_target(interactive=False)
    assert "VPN" not in single.value.hint


@pytest.mark.parametrize(
    "the_tv,wanted,expected",
    [
        (tv(), "auto", "dlna"),
        (tv(air=airplay()), "auto", "dlna"),
        (tv(air=airplay()), "airplay", "airplay"),
        (tv(dlna_on=False, air=airplay()), "auto", "airplay"),
    ],
)
def test_protocol_choice(the_tv, wanted, expected):
    assert targets._pick_protocol(the_tv, wanted).protocol == expected


@pytest.mark.parametrize(
    "the_tv,wanted,message",
    [
        (tv(dlna_on=False, air=airplay(paired=False)), "auto", "not paired"),
        (tv(dlna_on=False, air=airplay()), "dlna", "did not answer as a DLNA renderer"),
        (tv(), "airplay", "does not offer AirPlay"),
        (tv(dlna_on=False), "auto", "offers no way to play video"),
    ],
)
def test_protocol_errors_say_how_to_fix_it(the_tv, wanted, message):
    with pytest.raises(UserError, match=message):
        targets._pick_protocol(the_tv, wanted)


def test_pairing_hint_includes_the_command():
    with pytest.raises(UserError) as info:
        targets._pick_protocol(tv(dlna_on=False, air=airplay(paired=False, identifier="F0:70")), "auto")
    assert "atvremote --id F0:70 --protocol airplay pair" in info.value.hint
