"""The seat (key, id), the desk plan, snapshots, usage from the log, and the IT report —
pure host-side logic, no hardware."""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dockpilot"))
import dockpilot_core as core  # noqa: E402

HAVE_ALL = lambda t: True
HAVE_NONE = lambda t: False


def test_seat_phrase_round_trip_and_id(tmp_path):
    ph = core.seat_phrase_new()
    assert ph.count("-") == 5 and all(w in core.SEAT_WORDS for w in ph.split("-"))
    assert core.seat_phrase_clean("  Copper LANTERN orbit, seven maple tide ") == "copper-lantern-orbit-seven-maple-tide"
    assert core.seat_phrase_clean("copper lantern orbit") is None
    assert core.seat_phrase_clean("copper-lantern-orbit-seven-maple-xyzzy") is None
    sid = core.seat_id("copper-lantern-orbit-seven-maple-tide")
    assert len(sid) == 16 and int(sid, 16) >= 0
    assert sid == core.seat_id("copper-lantern-orbit-seven-maple-tide")      # stable: the other laptop gets the same seat
    assert sid != core.seat_id("copper-lantern-orbit-seven-maple-tiger")
    p = str(tmp_path / "seat_key")
    a = core.seat_load(p)
    assert core.seat_load(p) == a                                            # kept
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert core.seat_save("copper-lantern-orbit-seven-maple-tide", p) and core.seat_load(p) == "copper-lantern-orbit-seven-maple-tide"


def test_desk_plan_applies_and_undoes_options_and_rules():
    desk = {"tag": "Desk 22", "opt": {"vlan": "30", "mtu": "1500", "addr": "10.3.22.5/24 10.3.22.1", "dns": "10.3.0.53,10.3.0.54",
                                       "domain": "lon.corp", "ntp": "10.3.0.1", "tz": "Europe/London", "proxy": "proxy.lon.corp:3128", "vpn": "vpn.corp.com"}}
    seat = {"rules": {"wifi_off": 1, "audio_dock": 1, "lock_undock": 1, "mute_undock": 1, "touchpad_off": 1, "vpn": 1, "proxy": 1, "tile": 1}}
    steps, applied, skipped = core.desk_plan(desk, seat, True, "enx0", HAVE_ALL)
    notes = [n for n, _ in steps]
    assert applied == ["vlan", "mtu", "addr", "dns", "tz", "proxy", "vpn"] and skipped == ["ntp"]
    assert any("VLAN 30" in n for n in notes) and any("proxy" in n for n in notes) and "Wi-Fi off" in notes
    assert "lock the screen" not in notes and "mute" not in notes                 # undock-only
    joined = " ".join(x for _, x in steps if isinstance(x, str))
    assert "opendock-vlan-30" in joined and "ipv4.dns \"10.3.0.53 10.3.0.54\"" in joined and "set-timezone \"Europe/London\"" in joined
    steps, applied, skipped = core.desk_plan(desk, seat, False, "enx0", HAVE_ALL)
    notes = [n for n, _ in steps]
    assert "lock the screen" in notes and "mute" in notes and "Wi-Fi on" in notes and "VLAN removed" in notes and "proxy off" in notes
    # without the tools, options are skipped and said so; the tile rule never runs anything
    steps, applied, skipped = core.desk_plan(desk, seat, True, "enx0", HAVE_NONE)
    assert applied == [] and set(skipped) >= {"vlan", "mtu", "addr", "dns", "tz", "proxy", "vpn", "ntp"} and steps == []
    # proxy and vpn only by rule
    steps, applied, skipped = core.desk_plan(desk, {"rules": {}}, True, "enx0", HAVE_ALL)
    assert "proxy" not in applied and "vpn" not in applied
    # no desk at all
    assert core.desk_plan(None, None, True, "enx0", HAVE_ALL) == ([], [], [])


def test_snapshot_words_and_restore_plan():
    text = json.dumps({"screens": 2, "sink": "alsa_output.usb-dock.analog-stereo", "volume": 40, "keyboard": "gb", "vpn": ["corp"]})
    assert core.snapshot_words(text) == "2 screens · sound: dock · 40 % · keyboard: gb · VPN: corp"
    assert core.snapshot_words("") == "nothing saved yet" and core.snapshot_words("{bad") == "unreadable"
    steps = core.restore_plan(text, HAVE_ALL)
    assert [n for n, _ in steps] == ["sound output", "volume 40 %", "keyboard gb", "VPN corp"]
    assert core.restore_plan(text, HAVE_NONE) == [] and core.restore_plan("{bad", HAVE_ALL) == []


def test_take_snapshot_is_small_json():
    text = core.take_snapshot()
    d = json.loads(text)
    assert isinstance(d, dict) and len(text.encode("utf-8")) <= 400


def test_usage_counts_days_sessions_and_open_session():
    now = 1762200000
    day = now - now % 86400
    ents = [{"i": 1, "t": None, "ev": "boot"},
            {"i": 2, "t": day - 86400 * 2 + 9 * 3600, "ev": "dock"}, {"i": 3, "t": day - 86400 * 2 + 17 * 3600, "ev": "undock"},
            {"i": 4, "t": day + 8 * 3600, "ev": "dock"}, {"i": 5, "t": day + 12 * 3600, "ev": "undock"},
            {"i": 6, "t": day + 13 * 3600, "ev": "dock"}]
    u = core.dock_usage(ents, days=14, now=day + 15 * 3600, docked_now=True, docked_s=7200)
    assert u["per_day"][-3] == 8 * 3600 and u["per_day"][-1] == 4 * 3600 + 2 * 3600
    assert u["sessions"] == 3 and u["today_s"] == 6 * 3600 and u["last_used"] == day + 15 * 3600


def test_report_schema_2_and_key():
    snap = {"identity": {"serial": "OD1-A", "uid": "x", "model": "opendock-1", "fw": "0.6.0", "proto": 1},
            "status": {"name": "Desk 22", "docked": True, "docked_s": 10, "temp_c": 31, "time_synced": True, "log_entries": 5, "log_volatile": False},
            "caps": {"log": True, "health": True}, "health": {"ok": True}, "stats": {"docks": 3},
            "desk": {"tag": "Desk 22", "opt": {"vlan": "30", "mtu": ""}, "keys": 2, "seats": 1}}
    rep = core.dock_report(snap, host="laptop")
    assert rep["schema"] == "opendock-report/2" and rep["tag"] == "Desk 22" and rep["options"] == {"vlan": "30"}
    assert rep["keys"] == 2 and rep["seats"] == 1 and rep["capabilities"] == ["health", "log"]
    k1 = core.report_key(rep)
    snap["desk"]["opt"]["vlan"] = "31"
    assert core.report_key(core.dock_report(snap, host="laptop")) != k1
    rep2 = core.dock_report({"identity": {}, "status": {}}, host="l")
    assert rep2["options"] is None and rep2["keys"] is None


def test_health_words():
    assert core.health_words({"ok": True, "faults": {"since_boot": 0}, "flash": {"writes_left": 30}, "clock": "synced"}).startswith("OK")
    assert "watchdog" in core.health_words({"ok": False, "reset": "watchdog", "faults": {}, "log": {}, "flash": {}})


def test_rule_now_plan_only_touches_that_rule():
    """A rule switched while docked: desk_plan with only that rule and, for proxy/vpn, only that option."""
    desk = {"opt": {"vlan": "30", "proxy": "proxy.lon.corp:3128", "vpn": "vpn.corp.com"}}
    steps, applied, _ = core.desk_plan({"opt": {"proxy": desk["opt"]["proxy"]}}, {"rules": {"proxy": 1}}, True, "enx0", HAVE_ALL)
    assert applied == ["proxy"] and [n for n, _ in steps] == ["proxy proxy.lon.corp:3128"]
    steps, applied, _ = core.desk_plan({"opt": {}}, {"rules": {"touchpad_off": 1}}, False, "enx0", HAVE_ALL)
    assert applied == [] and [n for n, _ in steps] == ["touchpad on"]
    steps, _, _ = core.desk_plan({"opt": {}}, {"rules": {"lock_undock": 1}}, True, "enx0", HAVE_ALL)
    assert steps == []                                                       # waits for undock
