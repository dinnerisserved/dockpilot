"""test_helper.py — dockpilot_helper.py accepts exactly its allow-list and nothing else.

Runs the helper as a subprocess in --dry-run mode under a fake /sys, so nothing is
touched and no root is needed:   python3 -m pytest tests/test_helper.py -q
"""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "..", "dockpilot", "dockpilot_helper.py")


@pytest.fixture(scope="module")
def sysfs(tmp_path_factory):
    root = tmp_path_factory.mktemp("sys")
    for d in ("class/net/eth0", "class/net/enx00e04c680123", "bus/usb/devices/1-3.2",
              "bus/usb/devices/2-1", "bus/usb/drivers/r8152", "bus/usb/drivers/cdc_ncm", "class/power_supply/BAT0"):
        os.makedirs(root / d)
    return str(root)


def helper(sysfs, *args, stdin=None):
    p = subprocess.run([sys.executable, HELPER, "--dry-run", "--sysfs", sysfs] + list(args),
                       capture_output=True, text=True, input=stdin, timeout=30)
    return p.returncode, p.stdout.strip()


# ---- accepted -------------------------------------------------------------
GOOD = [
    (["offload", "eth0", "tcp-segmentation-offload", "on"], "run ethtool -K eth0 tcp-segmentation-offload on"),
    (["offload", "enx00e04c680123", "rx-checksumming", "off"], "run ethtool -K enx00e04c680123 rx-checksumming off"),
    (["speed", "eth0", "auto"], "run ethtool -s eth0 autoneg on"),
    (["speed", "eth0", "100", "full", "off"], "run ethtool -s eth0 speed 100 duplex full autoneg off"),
    (["speed", "eth0", "1000", "full", "on"], "run ethtool -s eth0 speed 1000 duplex full autoneg on"),
    (["wol", "eth0", "g"], "run ethtool -s eth0 wol g"),
    (["wol", "eth0", "d"], "run ethtool -s eth0 wol d"),
    (["wol-get", "eth0"], "run ethtool eth0"),
    (["eeprom-get", "eth0"], "run ethtool -e eth0 offset 0 length 64"),
    (["eeprom-get", "eth0", "full"], "run ethtool -e eth0"),
    (["pause", "eth0", "on"], "run ethtool -A eth0 rx on tx on"),
    (["renegotiate", "eth0"], "run ethtool -r eth0"),
    (["link-up", "eth0"], "run ip link set eth0 up"),
    (["usb-authorize", "1-3.2", "0"], "write {sys}/bus/usb/devices/1-3.2/authorized <- 0"),
    (["usb-authorize", "2-1", "1"], "write {sys}/bus/usb/devices/2-1/authorized <- 1"),
    (["usb-ports"], "run uhubctl"),
    (["usb-port", "1-3", "2", "cycle"], "run uhubctl -l 1-3 -p 2 -a cycle"),
    (["usb-port", "2-1.4", "12", "off"], "run uhubctl -l 2-1.4 -p 12 -a off"),
    (["install-udev-rule"], "write /etc/udev/rules.d/70-opendock.rules <- (rule)"),
    (["charge-limit", "80"], "write {sys}/class/power_supply/BAT0/charge_control_end_threshold <- 80"),
    (["charge-limit", "100"], "write {sys}/class/power_supply/BAT0/charge_control_end_threshold <- 100"),
]


@pytest.mark.parametrize("args,expect", GOOD)
def test_accepted(sysfs, args, expect):
    rc, out = helper(sysfs, *args)
    assert rc == 0, out
    assert out == expect.format(sys=sysfs)


def test_mac_sets_down_address_up(sysfs):
    rc, out = helper(sysfs, "mac", "eth0", "02:AB:cd:00:11:22")
    assert rc == 0
    assert out.splitlines() == ["run ip link set eth0 down",
                                "run ip link set eth0 address 02:ab:cd:00:11:22",
                                "run ip link set eth0 up"]


def test_usb_config_unbinds_then_switches(sysfs):
    rc, out = helper(sysfs, "usb-config", "1-3.2", "r8152", "1.0", "2")
    assert rc == 0
    assert out.splitlines() == ["write %s/bus/usb/drivers/r8152/unbind <- 1-3.2:1.0" % sysfs,
                                "write %s/bus/usb/devices/1-3.2/bConfigurationValue <- 2" % sysfs]


# ---- refused --------------------------------------------------------------
BAD = [
    [],
    ["bash", "-c", "id"],
    ["ethtool", "-K", "eth0", "tso", "on"],
    ["offload", "eth0", "tso"],                                 # missing on/off
    ["offload", "eth0", "tso", "maybe"],
    ["offload", "eth0; id", "tso", "on"],
    ["offload", "../../etc", "tso", "on"],
    ["offload", "wlan9", "tso", "on"],                          # interface does not exist
    ["offload", "eth0", "tso -K eth1", "on"],
    ["speed", "eth0", "999", "full", "on"],
    ["speed", "eth0", "100", "both", "on"],
    ["speed", "eth0"],
    ["wol", "eth0", "p"],
    ["wol-get", "eth0", "-s"],
    ["wol-get"],
    ["eeprom-get", "eth0", "raw"],
    ["eeprom-get", "eth0", "offset", "0", "length", "9999"],
    ["pause", "eth0", "rx"],
    ["renegotiate", "eth0", "extra"],
    ["mac", "eth0", "00:11:22:33:44"],                          # too short
    ["mac", "eth0", "01:11:22:33:44:55"],                       # multicast
    ["mac", "eth0", "zz:11:22:33:44:55"],
    ["link-up", "eth0", "eth1"],
    ["usb-authorize", "1-3.2", "2"],
    ["usb-authorize", "9-9", "0"],                              # device does not exist
    ["usb-authorize", "../../../etc/passwd", "0"],
    ["usb-config", "1-3.2", "nosuchdriver", "1.0", "2"],
    ["usb-config", "1-3.2", "r8152", "1.0;ls", "2"],
    ["usb-config", "1-3.2", "r8152", "1.0", "abc"],
    ["usb-port", "1-3", "2", "reboot"],
    ["usb-port", "1-3 -a off", "2", "on"],
    ["usb-port", "1-3", "x", "on"],
    ["usb-ports", "-a", "off"],
    ["rm", "-rf", "/"],
    ["--serve", "x"],
    ["install-udev-rule", "/etc/passwd"],
    ["charge-limit", "40"],
    ["charge-limit", "101"],
    ["charge-limit", "80", "BAT1"],
    ["charge-limit", "8o"],
]


@pytest.mark.parametrize("args", BAD, ids=[" ".join(a) or "(empty)" for a in BAD])
def test_refused(sysfs, args):
    rc, out = helper(sysfs, *args)
    assert rc == 2, out
    executed = [l for l in out.splitlines() if l.startswith("run ") or l.startswith("write ")]
    assert not executed, "something was executed: %s" % executed


# ---- serve protocol ---------------------------------------------------------
def test_serve_handles_many_requests_and_bad_lines(sysfs):
    lines = [json.dumps(["renegotiate", "eth0"]),
             "this is not json",
             json.dumps(["offload", "eth0", "gro", "on"]),
             json.dumps({"op": "offload"}),                       # not a list
             json.dumps(["mac", "eth0", "02:00:00:00:00:01"]),
             json.dumps(["bash", "-c", "id"])]
    rc, out = helper(sysfs, "--serve", stdin="\n".join(lines) + "\n")
    assert rc == 0
    assert out.splitlines() == [
        "run ethtool -r eth0", "__RC__0",
        "refused: bad request", "__RC__2",
        "run ethtool -K eth0 gro on", "__RC__0",
        "refused: bad request", "__RC__2",
        "run ip link set eth0 down", "run ip link set eth0 address 02:00:00:00:00:01",
        "run ip link set eth0 up", "__RC__0",
        "refused: unknown operation 'bash'", "__RC__2",
    ]


def test_no_shell_metacharacters_reach_a_command(sysfs):
    """Even a value that passes a regex is passed as one argv element, never through a shell."""
    rc, out = helper(sysfs, "usb-port", "1-3", "2", "on")
    assert rc == 0 and out == "run uhubctl -l 1-3 -p 2 -a on"
    src = open(HELPER).read()
    assert "shell=True" not in src and "os.system" not in src and "eval(" not in src
