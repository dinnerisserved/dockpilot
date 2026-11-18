"""The VPN side of DockPilot: what NetworkManager has, importing WireGuard files, making the seat's
pick true, telling the dock, and acting at once on a change made on the dock's page.
No hardware and no NetworkManager: fake_nm.py stands in for nmcli."""
import json
import os
import sys
import zipfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dockpilot"))
sys.path.insert(0, HERE)
import dockpilot_core as core  # noqa: E402
from fake_nm import FakeNM, wg_conf  # noqa: E402


@pytest.fixture(autouse=True)
def _own_files(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "VPN_FILE", str(tmp_path / "vpn.json"))
    monkeypatch.setattr(core, "DESK_STATE_FILE", str(tmp_path / "desk_applied.json"))


# ---------------------------------------------------------------- reading nmcli
def test_nm_split_undoes_the_escaping():
    assert core.nm_split("a:b:c") == ["a", "b", "c"]
    assert core.nm_split(r"Office\: 2nd floor:uuid:wireguard:yes") == ["Office: 2nd floor", "uuid", "wireguard", "yes"]
    assert core.nm_split(r"back\\slash:x") == ["back\\slash", "x"]
    assert core.nm_split("") == [""]


def test_only_vpns_are_listed_and_names_with_colons_survive():
    nm = FakeNM()
    nm.add("se-sto-wg-001"); nm.add("Office: 2nd floor", typ="vpn", active=True)
    cons = core.vpn_connections(nm.run)
    assert [(c["name"], c["type"], c["active"]) for c in cons] == [("se-sto-wg-001", "wireguard", False), ("Office: 2nd floor", "vpn", True)]
    assert core.vpn_connections(lambda a, timeout=0: (8, "Error: NetworkManager is not running.")) == []


def test_listing_groups_imported_first_then_by_hand_then_the_rest():
    cons = [{"name": n, "uuid": u, "type": "wireguard", "active": False} for n, u in
            (("work", "u1"), ("se-sto", "u2"), ("de-fra", "u3"), ("home", "u4"), ("se-sto", "u5"))]
    rows = core.vpn_listing(cons, {"u2": "Mullvad", "u3": "Mullvad", "u4": core.VPN_GROUP_HAND})
    assert rows == [("Mullvad", "de-fra"), ("Mullvad", "se-sto"), (core.VPN_GROUP_HAND, "home"), (core.VPN_GROUP_HERE, "work")]
    many = [{"name": "s%03d" % i, "uuid": "u%d" % i, "type": "wireguard", "active": False} for i in range(260)]
    assert len(core.vpn_listing(many, {})) == 200


def test_group_name_from_a_providers_zip():
    assert core.vpn_group_name("/x/mullvad_wireguard_linux_se_all.zip") == "Mullvad"
    assert core.vpn_group_name("ProtonVPN_server_configs.zip") == "ProtonVPN"
    assert core.vpn_group_name("wireguard-configs.zip") == "wireguard-configs"
    assert core.vpn_group_name("my vpn files.zip") == "my vpn files"
    assert core.vpn_group_name(".zip") == "Imported"


# ---------------------------------------------------------------- importing
def test_import_a_single_conf(tmp_path):
    nm = FakeNM()
    f = tmp_path / "se-sto-wg-001.conf"; f.write_text(wg_conf())
    res = core.vpn_import([str(f)], run_fn=nm.run)
    assert res == {"added": ["se-sto-wg-001"], "skipped": [], "groups": [core.VPN_GROUP_HAND]}
    c = nm.cons[0]
    assert c["name"] == "se-sto-wg-001" and c["autoconnect"] is False and c["active"] is False   # the import left it up; it was taken down
    assert json.load(open(core.VPN_FILE))["groups"] == {c["uuid"]: core.VPN_GROUP_HAND}
    path, mode, text = nm.seen_files[0]
    assert mode == 0o600 and "PrivateKey" in text and not os.path.exists(path) and not os.path.exists(os.path.dirname(path))   # the copy is private and gone
    assert f.read_text() == wg_conf()                                                              # the person's own file is untouched


def test_import_a_providers_zip_groups_its_servers(tmp_path):
    nm = FakeNM()
    z = tmp_path / "mullvad_wireguard_linux_all.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for n in ("se-sto-wg-001", "se-got-wg-002", "de-fra-wg-005"):
            zf.writestr("mullvad_config_linux/%s.conf" % n, wg_conf())
        zf.writestr("mullvad_config_linux/README.txt", "hello")
        zf.writestr("mullvad_config_linux/ovpn/se.ovpn", "client\n")
    res = core.vpn_import([str(z)], run_fn=nm.run)
    assert res["added"] == ["de-fra-wg-005", "se-got-wg-002", "se-sto-wg-001"] and res["skipped"] == [] and res["groups"] == ["Mullvad"]
    assert core.vpn_listing(core.vpn_connections(nm.run), core.vpn_file_load()["groups"]) == [("Mullvad", n) for n in sorted(res["added"])]
    assert nm.active() == []


def test_a_name_linux_would_refuse_as_an_interface_still_imports_under_its_own_name(tmp_path):
    nm = FakeNM()
    f = tmp_path / "Sweden – Stockholm (fast) 01.conf"; f.write_text(wg_conf())
    res = core.vpn_import([str(f)], run_fn=nm.run)
    assert res["added"] == ["Sweden – Stockholm (fast) 01"]
    assert nm.cons[0]["name"] == "Sweden – Stockholm (fast) 01" and nm.cons[0]["ifname"].startswith("wg-") and len(nm.cons[0]["ifname"]) <= 15
    g = tmp_path / "Sweden – Göteborg (fast) 02.conf"; g.write_text(wg_conf())
    core.vpn_import([str(g)], run_fn=nm.run)
    assert nm.cons[0]["ifname"] != nm.cons[1]["ifname"]


def test_what_import_leaves_out_and_says_why(tmp_path):
    nm = FakeNM(); nm.add("already")
    (tmp_path / "already.conf").write_text(wg_conf())
    (tmp_path / "notes.conf").write_text("server = 10.0.0.1\n")
    (tmp_path / "nokey.conf").write_text("[Interface]\nPrivateKey =\n[Peer]\nPublicKey = x\n")
    (tmp_path / "client.ovpn").write_text("client\n")
    (tmp_path / "big.conf").write_text("x" * 70000)
    (tmp_path / "bad.zip").write_text("not a zip")
    with zipfile.ZipFile(tmp_path / "empty.zip", "w") as zf:
        zf.writestr("readme.txt", "x")
    res = core.vpn_import([str(tmp_path / n) for n in ("already.conf", "notes.conf", "client.ovpn", "big.conf", "bad.zip", "empty.zip", "missing.conf")], run_fn=nm.run)
    why = dict(res["skipped"])
    assert res["added"] == []
    assert why["already.conf"] == "already there" and why["notes.conf"] == "not a WireGuard file" and why["client.ovpn"] == "not a .conf or a .zip"
    assert why["big.conf"] == "too large to be a WireGuard file" and why["empty.zip"] == "no WireGuard .conf files inside"
    assert why["bad.zip"].startswith("cannot be read") and why["missing.conf"].startswith("cannot be read")
    assert nm.names() == ["already"]


def test_nmcli_refusing_a_file_is_reported_in_its_own_words(tmp_path):
    def run(args, timeout=0):
        if "import" in args:
            return 1, "Error: failed to import '/tmp/x.conf': Missing or invalid PrivateKey."
        return 0, ""
    (tmp_path / "x.conf").write_text(wg_conf())
    res = core.vpn_import([str(tmp_path / "x.conf")], run_fn=run)
    assert res["added"] == [] and res["skipped"] == [("x.conf", "failed to import '/tmp/x.conf': Missing or invalid PrivateKey.")]


def test_no_more_than_the_dock_can_show(tmp_path):
    nm = FakeNM()
    for i in range(198):
        nm.add("old-%03d" % i)
    z = tmp_path / "many.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for i in range(5):
            zf.writestr("s%d.conf" % i, wg_conf())
    res = core.vpn_import([str(z)], run_fn=nm.run)
    assert res["added"] == ["s0", "s1"] and len(res["skipped"]) == 3 and "the dock shows 200" in res["skipped"][0][1]


# ---------------------------------------------------------------- the link from the dock's page
def test_only_named_actions_are_read_from_a_link():
    assert core.url_action("opendock://vpn-import") == "vpn-import"
    assert core.url_action("opendock:vpn-import") == "vpn-import"
    assert core.url_action("OPENDOCK://VPN-Import?file=/etc/shadow#x") == "vpn-import"
    assert core.url_action("opendock://vpn-import/../../run?cmd=rm") == "vpn-import"      # the rest is never read
    assert core.url_action("opendock://open") == "open"
    for bad in ("opendock://run?cmd=ls", "opendock://", "opendock://vpn-import-all", "opendock://apply?profile=x", "http://vpn-import", "vpn-import", ""):
        assert core.url_action(bad) is None, bad


def test_what_the_file_window_says_afterwards():
    assert "did not answer" in core.vpn_import_words(None)
    one = core.vpn_import_words({"ok": True, "added": ["a"], "skipped": [], "groups": ["Added by hand"]})
    assert one == "VPN added manually to dock & to Computer"
    many = core.vpn_import_words({"ok": True, "added": ["a", "b"], "skipped": [["x.conf", "already there"]] * 10, "groups": ["Mullvad"]})
    assert many.startswith("2 VPNs added to dock & to Computer (Mullvad)") and "10 files left out:" in many and "… and 2 more" in many
    assert core.vpn_import_words({"ok": True, "added": [], "skipped": [["x", "y"]], "groups": []}).startswith("Nothing was added.")


# ---------------------------------------------------------------- making the pick true
class FakeDock:
    """What the engine needs of opendock.Dock to report VPNs."""
    def __init__(self):
        self.published, self.states, self.vpn = [], [], True

    def has_vpn(self):
        return self.vpn

    def vpn_publish(self, entries):
        self.published.append(list(entries))
        return len(entries)

    def vpn_state(self, state, name="", message=""):
        self.states.append((state, name, message))
        return True


class FakeLink:
    def __init__(self, dock):
        self.dock = dock
        self.connected = True

    def snapshot(self):
        return {}


@pytest.fixture
def eng(monkeypatch):
    e = core.Engine(role="service", automation=True, opendock_link=False)
    nm, dock = FakeNM(), FakeDock()
    for n in ("se-sto", "de-fra", "broken-nl", "work"):
        nm.add(n)
    core.vpn_file_save({"groups": {nm.cons[0]["uuid"]: "Mullvad", nm.cons[1]["uuid"]: "Mullvad", nm.cons[2]["uuid"]: "Mullvad"}, "mine": ""})
    e.vpn_run, e.odlink = nm.run, FakeLink(dock)
    e.logged = []
    e.log = e.logged.append

    def seat(vpn="", on=0, docked=True, rules=None):
        e.od_snap = {"connected": True, "status": {"docked": docked}, "desk": {"opt": {}}, "seat": {"vpn": vpn, "vpn_on": on, "rules": rules or {}}}
    e.seat, e.nm, e.dock = seat, nm, dock
    seat()
    return e


def test_the_list_goes_to_the_dock_once_and_again_only_when_it_changes(eng):
    eng._vpn_step()
    assert eng.dock.published == [[("Mullvad", "broken-nl"), ("Mullvad", "de-fra"), ("Mullvad", "se-sto"), (core.VPN_GROUP_HERE, "work")]]
    assert eng.dock.states == [("down", "", "")]
    eng._vpn_step(); eng._vpn_step()
    assert len(eng.dock.published) == 1 and len(eng.dock.states) == 1          # nothing new: nothing sent
    eng.nm.add("home")
    eng._vpn_step()
    assert len(eng.dock.published) == 2 and (core.VPN_GROUP_HERE, "home") in eng.dock.published[1]


def test_the_pick_comes_up_when_docked(eng):
    eng.seat("se-sto", 1)
    eng._vpn_request("apply"); eng._vpn_step()
    assert eng.nm.active() == ["se-sto"]
    assert eng.dock.states[-2:] == [("connecting", "se-sto", ""), ("up", "se-sto", "")]
    assert core.vpn_file_load()["mine"] == "se-sto"


def test_choosing_another_server_takes_the_first_down(eng):
    eng.seat("se-sto", 1); eng._vpn_request("apply"); eng._vpn_step()
    eng.seat("de-fra", 1); eng._vpn_request("apply"); eng._vpn_step()
    assert eng.nm.active() == ["de-fra"] and eng.dock.states[-1] == ("up", "de-fra", "")
    ups = [c for c in eng.nm.calls if c[1:3] == ["connection", "up"]]
    assert len(ups) == 2                                                       # one command each, not a storm


def test_switched_off_brings_it_down_and_keeps_quiet_after(eng):
    eng.seat("se-sto", 1); eng._vpn_request("apply"); eng._vpn_step()
    eng.seat("se-sto", 0); eng._vpn_request("apply"); eng._vpn_step()
    assert eng.nm.active() == [] and eng.dock.states[-1] == ("down", "", "") and core.vpn_file_load()["mine"] == ""
    n = len(eng.nm.calls)
    eng._vpn_step()
    assert [c for c in eng.nm.calls[n:] if c[1:3] in (["connection", "up"], ["connection", "down"])] == []


def test_not_docked_nothing_is_connected(eng):
    eng.seat("se-sto", 1, docked=False)
    eng._vpn_request("apply"); eng._vpn_step()
    assert eng.nm.active() == []


def test_a_vpn_that_is_already_up_is_left_alone_and_adopted(eng):
    eng.nm.cons[0]["active"] = True                                            # se-sto, brought up by hand
    eng.seat("se-sto", 1); eng._vpn_request("apply"); eng._vpn_step()
    assert not [c for c in eng.nm.calls if c[1:3] == ["connection", "up"]]
    assert eng.dock.states[-1] == ("up", "se-sto", "") and core.vpn_file_load()["mine"] == "se-sto"


def test_a_vpn_the_person_started_by_hand_is_not_taken_down_by_a_switch_off(eng):
    eng.nm.cons[3]["active"] = True                                            # "work", nothing to do with the pick
    eng.seat("se-sto", 0); eng._vpn_request("apply"); eng._vpn_step()
    assert eng.nm.active() == ["work"] and eng.dock.states[-1] == ("up", "work", "")


def test_a_pick_that_will_not_come_up_says_why_and_is_tried_again_later(eng, monkeypatch):
    eng.seat("broken-nl", 1); eng._vpn_request("apply"); eng._vpn_step()
    assert eng.nm.active() == []
    assert eng.dock.states[-1] == ("failed", "broken-nl", "Connection activation failed: The peer did not answer the handshake")
    n = len([c for c in eng.nm.calls if c[1:3] == ["connection", "up"]])
    eng._vpn_step()
    assert len([c for c in eng.nm.calls if c[1:3] == ["connection", "up"]]) == n            # not hammered
    monkeypatch.setattr(core.time, "time", lambda t=core.time.time(): t + 31)
    eng._vpn_step()
    assert len([c for c in eng.nm.calls if c[1:3] == ["connection", "up"]]) == n + 1        # half a minute on: once more


def test_a_pick_this_laptop_does_not_have(eng):
    eng.seat("somewhere-else", 1); eng._vpn_request("apply"); eng._vpn_step()
    assert eng.dock.states[-1] == ("failed", "somewhere-else", "this laptop has no VPN called that")


def test_undock_with_the_rule_drops_it_and_without_the_rule_leaves_it(eng):
    eng.seat("se-sto", 1); eng._vpn_request("apply"); eng._vpn_step()
    eng.od_snap = {"connected": False}                                         # the cable is out
    eng._vpn_step()
    assert eng.nm.active() == ["se-sto"]                                       # no rule: the VPN stays with you
    eng._vpn_request("drop"); eng._vpn_step()
    assert eng.nm.active() == [] and core.vpn_file_load()["mine"] == ""


def test_a_dock_without_the_vpn_feature_is_told_nothing(eng):
    eng.dock.vpn = False
    eng.seat("se-sto", 1); eng._vpn_request("apply"); eng._vpn_step()
    assert eng.dock.published == [] and eng.dock.states == [] and eng.nm.active() == []


def test_import_through_the_engine_puts_the_new_servers_on_the_dock(eng, tmp_path):
    eng._vpn_step()
    f = tmp_path / "ch-zrh-wg-001.conf"; f.write_text(wg_conf())
    res = eng.vpn_import_files([str(f)])
    assert res["added"] == ["ch-zrh-wg-001"] and "list" in eng._vpn_todo
    eng._vpn_step()
    assert (core.VPN_GROUP_HAND, "ch-zrh-wg-001") in eng.dock.published[-1]
    assert any("VPN import: 1 added" in l for l in eng.logged)


# ---------------------------------------------------------------- a change made on the dock's page is acted on now
def test_changes_in_the_dock_are_acted_on_at_once(eng, monkeypatch):
    ran, did = [], []
    monkeypatch.setattr(core, "run_commands", lambda cmds, log=None, env_extra=None: ran.extend(cmds))
    monkeypatch.setattr(core, "have", lambda b: True)
    eng.do = lambda args, note=None, after=None: did.append(args)
    eng.odlink = None                                                          # _od_changed takes od_snap as it is
    eng.desk_applied = {"desk": {"opt": {}}, "seat": {"rules": {}}}

    def state(opt=None, rules=None, vpn=("", 0)):
        eng.od_snap = {"connected": True, "status": {"docked": True}, "desk": {"opt": opt or {}},
                       "seat": {"rules": rules or {}, "vpn": vpn[0], "vpn_on": vpn[1]}}
    state(); eng._od_seen = eng._od_view()

    state(rules={"wifi_off": 1}); eng._od_changed()                            # a rule switched on the page
    assert did == [["nmcli", "radio", "wifi", "off"]]
    state(rules={}); eng._od_changed()                                         # and off again
    assert did[-1] == ["nmcli", "radio", "wifi", "on"]

    state(opt={"proxy": "proxy.lon.corp:3128"}, rules={"proxy": 1}); eng._od_changed()      # the proxy set and switched on
    assert len(ran) == 1 and "proxy.http host \"proxy.lon.corp\"" in ran[0] and "mode manual" in ran[0]
    assert eng.desk_applied["desk"]["opt"]["proxy"] == "proxy.lon.corp:3128" and eng.desk_applied["seat"]["rules"]["proxy"] == 1
    state(opt={}, rules={}); eng._od_changed()                                 # switched off: reads blank
    assert ran[-1] == "gsettings set org.gnome.system.proxy mode none" and eng.desk_applied["desk"]["opt"]["proxy"] == ""

    n = len(did) + len(ran)
    state(rules={"lock_undock": 1}); eng._od_changed()                         # an undock rule does nothing now
    assert len(did) + len(ran) == n and eng.desk_applied["seat"]["rules"]["lock_undock"] == 1

    eng._vpn_todo.clear()
    state(rules={"lock_undock": 1}, vpn=("se-sto", 1)); eng._od_changed()      # the VPN chosen on the page
    assert "apply" in eng._vpn_todo
    eng._vpn_todo.clear(); eng._od_changed()                                   # chosen again (a nudge): looked at again
    assert "apply" in eng._vpn_todo
    eng._vpn_todo.clear()
    state(rules={"lock_undock": 1}, vpn=("se-sto", 0)); eng._od_changed()
    assert "apply" in eng._vpn_todo


def test_nothing_is_run_for_a_change_while_not_docked(eng, monkeypatch):
    ran, did = [], []
    monkeypatch.setattr(core, "run_commands", lambda cmds, log=None, env_extra=None: ran.extend(cmds))
    eng.do = lambda args, note=None, after=None: did.append(args)
    eng.odlink = None
    eng.od_snap = {"connected": True, "status": {"docked": False}, "desk": {"opt": {}}, "seat": {"rules": {}}}
    eng._od_seen = eng._od_view()
    eng.od_snap = {"connected": True, "status": {"docked": False}, "desk": {"opt": {"proxy": "p:1"}}, "seat": {"rules": {"wifi_off": 1, "proxy": 1}}}
    eng._od_changed()
    assert ran == [] and did == []
