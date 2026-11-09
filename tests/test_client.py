"""opendock.py against the real firmware 0.6 code.py, run under stubbed CircuitPython in-process:
the desk, keys, export/import, the seat. No hardware, no pyserial port — a fake pipe."""
import json
import os
import sys
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dockpilot"))
import opendock  # noqa: E402

FW_DIR = os.environ.get("OPENDOCK_FIRMWARE_DIR") or os.path.join(HERE, "..", "..", "firmware-0.6")
pytestmark = pytest.mark.skipif(not os.path.exists(os.path.join(FW_DIR, "test_firmware.py")), reason="firmware tree not beside the app")


class FakePort:
    """Looks like serial.Serial to opendock.Dock; drives the firmware's Dock directly."""

    def __init__(self, fwdock):
        self.fwdock = fwdock
        self.lock = threading.Lock()

    def write(self, b):
        with self.lock:
            self.fwdock.serial.host_send(b)
            self.fwdock.read_lines()
            self.fwdock.poll()
        return len(b)

    def read(self, n):
        with self.lock:
            out = bytes(self.fwdock.serial.tx[:n])
            del self.fwdock.serial.tx[:n]
        if not out:
            time.sleep(0.01)
        return out

    def close(self):
        pass


@pytest.fixture
def dock(tmp_path, monkeypatch):
    sys.path.insert(0, FW_DIR)
    import test_firmware as tf
    tf.reset_state()
    fw = tf.load_fw(str(tmp_path))
    fwd = fw.Dock()
    fwd.serial.lines()
    port = FakePort(fwd)
    monkeypatch.setattr(opendock, "serial", type("S", (), {"Serial": lambda *a, **k: port, "SerialException": Exception}))
    d = opendock.Dock("/dev/fake", timeout=2.0)
    d.open(hello_wait=0.05)
    yield d, fw, fwd
    d.close()


def test_open_identify_caps_desk(dock):
    d, fw, fwd = dock
    assert d.identity["fw"] == fw.FW and d.has_desk()
    desk = d.get_desk()
    assert desk["tag"] == "" and desk["keys"] == 0 and set(desk["opt"]) == set(fw.OPT_IDS)
    assert [o["id"] for o in d.get_schema()] == list(fw.OPT_IDS)
    assert [r["id"] for r in d.get_rules()] == list(fw.RULE_IDS)


def test_options_keys_export_import(dock):
    d, fw, fwd = dock
    fwd.tokens = 1000
    assert d.set_tag("Desk 22, 3rd floor") and d.set_opt("vlan", "30") and d.set_opt("proxy", "proxy.lon.corp:3128")
    with pytest.raises(opendock.DockRefused):
        d.set_opt("vlan", "70000")
    with pytest.raises(opendock.DockRefused):
        d.set_opt("proxy", "x" * 65)
    assert d.set_key("badge_reader", "10.3.22.40") and d.set_key("quote", 'say "hi" \\ done')
    assert d.get_keys() == {"badge_reader": "10.3.22.40", "quote": 'say "hi" \\ done'}     # keys are one line; escapes round-trip
    lines = d.export()
    assert lines[0] == 'SET TAG "Desk 22, 3rd floor"' and 'SET OPT vlan "30"' in lines and 'SET KEY quote "say \\"hi\\" \\\\ done"' in lines
    fw.UP += 11
    assert d.del_key("badge_reader") and d.get_keys() == {"quote": 'say "hi" \\ done'}
    fw.UP += 11
    assert d.import_lines(["# a comment", 'SET TAG "Desk 7"', 'SET OPT mtu "1400"', 'SET KEY a "1"', "garbage line", ""]) is True
    desk = d.get_desk()
    assert desk["tag"] == "Desk 7" and desk["opt"]["mtu"] == "1400" and desk["opt"]["vlan"] == "" and d.get_keys() == {"a": "1"}


def test_seat(dock):
    d, fw, fwd = dock
    fwd.tokens = 1000
    sid = "3f9c1e2a7b0d4c55"
    assert d.seat(sid) and d.get_seat()["new"] is True
    assert d.set_rule("wifi_off", True) and d.set_cmd("dock", ["mount /mnt/nas", "", "firefox &"]) and d.set_snap('{"screens":2}') and d.set_autosave(True)
    s = d.get_seat()
    assert s["rules"] == {"wifi_off": 1} and s["cmd"]["dock"] == ["mount /mnt/nas", "firefox &"] and s["snap"] == '{"screens":2}' and s["autosave"] == 1
    with pytest.raises(opendock.DockRefused):
        d.set_rule("battery", True)
    with pytest.raises(opendock.DockRefused):
        d.set_snap("x" * 401)
    assert d.forget_seat() and d.get_seat()["new"] is True
    assert d.seat(None) and d.get_seat() == {"seat": None}


def test_link_reads_desk_and_seat(dock, tmp_path):
    d, fw, fwd = dock
    d.close()
    fwd.tokens = 1000
    link = opendock.DockLink(str(tmp_path / "cfg"), seat_id="aaaaaaaaaaaaaaaa")
    link.RESCAN_S = 0
    opendock.find_docks = lambda on_progress=None: [("/dev/fake", {"proto": 1})]
    link.poll()
    snap = link.snapshot()
    assert snap["connected"] and snap["desk"]["tag"] == "" and snap["seat"]["seat"] == "aaaaaaaaaaaaaaaa" and len(snap["rules"]) == 8
    link.desk_action("set_rule", "tile", True)
    assert link.snapshot()["seat"]["rules"] == {"tile": 1}
    link.desk_action("set_key", "room", "22")
    assert link.snapshot()["keys"] == {"room": "22"} and link.snapshot()["desk"]["keys"] == 1
    link.close()
