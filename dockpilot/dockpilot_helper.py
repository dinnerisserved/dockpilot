#!/usr/bin/python3
"""dockpilot_helper.py — the only thing DockPilot ever runs as root.

A short, fixed list of operations, each with its arguments checked before anything
runs. No shell, no eval, no free-form commands. DockPilot starts one of these per
session through pkexec (one password prompt), then sends it one request per line.

    dockpilot_helper.py VERB ARG...     one operation, exit code is the result
    dockpilot_helper.py --serve         read JSON lists from stdin, one per line,
                                        answer with the output then a line "__RC__<n>"

Operations (every argument must match; anything else is refused with exit 2):
    offload IFACE FEATURE on|off          ethtool -K
    speed IFACE auto                      ethtool -s ... autoneg on
    speed IFACE SPEED DUPLEX on|off       ethtool -s ... speed duplex autoneg
    wol IFACE g|d                         ethtool -s ... wol
    pause IFACE on|off                    ethtool -A ... rx tx
    renegotiate IFACE                     ethtool -r
    mac IFACE MAC                         ip link set down, address, up
    link-up IFACE                         ip link set up
    usb-authorize DEV 0|1                 /sys/bus/usb/devices/DEV/authorized
    usb-config DEV DRIVER IFNUM CFG       unbind DEV:IFNUM from DRIVER, then bConfigurationValue
    usb-ports                             uhubctl (list)
    usb-port LOC PORT on|off|cycle        uhubctl -l LOC -p PORT -a ACT
    install-udev-rule                     write the fixed OpenDock udev rule and reload udev
"""
import json
import os
import re
import subprocess
import sys
import time

USB_DEVICES = "/sys/bus/usb/devices"
USB_DRIVERS = "/sys/bus/usb/drivers"
NET = "/sys/class/net"

RE_IFACE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
RE_FEATURE = re.compile(r"^[a-z0-9-]{1,40}$")
RE_MAC = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
RE_DEV = re.compile(r"^[0-9]+-[0-9.]{1,24}$")
RE_DRIVER = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
RE_IFNUM = re.compile(r"^[0-9]{1,2}\.[0-9]{1,2}$")
RE_CFG = re.compile(r"^[0-9]{1,3}$")
RE_LOC = re.compile(r"^[0-9][0-9.-]{0,31}$")
RE_PORT = re.compile(r"^[0-9]{1,3}$")
SPEEDS = ("10", "100", "1000", "2500", "5000", "10000")
DUPLEX = ("half", "full")
ONOFF = ("on", "off")
PORT_ACTS = ("on", "off", "cycle")

DRY = False


class Refused(Exception):
    pass


def need(cond, why):
    if not cond:
        raise Refused(why)


def iface(s):
    need(RE_IFACE.match(s or ""), "bad interface name")
    need(s not in (".", "..") and os.path.isdir(os.path.join(NET, s)), "no such interface")
    return s


def usb_dev(s):
    need(RE_DEV.match(s or ""), "bad USB device")
    need(os.path.isdir(os.path.join(USB_DEVICES, s)), "no such USB device")
    return s


def sysfs_write(path, value):
    if DRY:
        print("write %s <- %s" % (path, value))
        return 0
    with open(path, "w") as f:
        f.write(value)
    return 0


def cmd(argv):
    if DRY:
        print("run " + " ".join(argv))
        return 0
    p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    out = (p.stdout or "") + (p.stderr or "")
    if out:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
    return p.returncode


def op_offload(a):
    need(len(a) == 3, "offload IFACE FEATURE on|off")
    need(RE_FEATURE.match(a[1]) and a[2] in ONOFF, "bad offload arguments")
    return cmd(["ethtool", "-K", iface(a[0]), a[1], a[2]])


def op_speed(a):
    if len(a) == 2 and a[1] == "auto":
        return cmd(["ethtool", "-s", iface(a[0]), "autoneg", "on"])
    need(len(a) == 4, "speed IFACE auto | speed IFACE SPEED DUPLEX on|off")
    need(a[1] in SPEEDS and a[2] in DUPLEX and a[3] in ONOFF, "bad speed arguments")
    return cmd(["ethtool", "-s", iface(a[0]), "speed", a[1], "duplex", a[2], "autoneg", a[3]])


def op_wol(a):
    need(len(a) == 2 and a[1] in ("g", "d"), "wol IFACE g|d")
    return cmd(["ethtool", "-s", iface(a[0]), "wol", a[1]])


def op_wol_get(a):
    need(len(a) == 1, "wol-get IFACE")
    return cmd(["ethtool", iface(a[0])])          # read-only: prints the Wake-on line among the rest


def op_eeprom_get(a):
    need(len(a) in (1, 2) and (len(a) == 1 or a[1] == "full"), "eeprom-get IFACE [full]")
    i = iface(a[0])
    if len(a) == 2:
        return cmd(["ethtool", "-e", i])                              # read-only hex dump, whole EEPROM
    return cmd(["ethtool", "-e", i, "offset", "0", "length", "64"])


POWER_SUPPLY = "/sys/class/power_supply"


def op_charge_limit(a):
    """charge-limit N: stop charging the laptop's battery at N % (50-100) while docked.
    Only on laptops whose firmware exposes the threshold; the dock is not involved."""
    need(len(a) == 1 and a[0].isdigit() and 50 <= int(a[0]) <= 100, "charge-limit 50..100")
    done = 0
    try:
        bats = sorted(d for d in os.listdir(POWER_SUPPLY) if d.startswith("BAT"))
    except OSError:
        bats = []
    for b in bats:
        p = os.path.join(POWER_SUPPLY, b, "charge_control_end_threshold")
        if DRY or os.path.exists(p):
            sysfs_write(p, a[0])
            done += 1
    need(done > 0, "this laptop has no charge threshold control")
    return 0


def op_pause(a):
    need(len(a) == 2 and a[1] in ONOFF, "pause IFACE on|off")
    return cmd(["ethtool", "-A", iface(a[0]), "rx", a[1], "tx", a[1]])


def op_renegotiate(a):
    need(len(a) == 1, "renegotiate IFACE")
    return cmd(["ethtool", "-r", iface(a[0])])


def op_mac(a):
    need(len(a) == 2, "mac IFACE MAC")
    mac = (a[1] or "").lower()
    need(RE_MAC.match(mac), "bad MAC address")
    need(int(mac[:2], 16) & 1 == 0, "multicast bit set")
    i = iface(a[0])
    rc = 1
    for attempt in range(4):                      # NetworkManager may re-up the link under us
        cmd(["ip", "link", "set", i, "down"])
        rc = cmd(["ip", "link", "set", i, "address", mac])
        if rc == 0 or DRY:
            break
        time.sleep(0.3 * (attempt + 1))
    cmd(["ip", "link", "set", i, "up"])
    return rc


def op_link_up(a):
    need(len(a) == 1, "link-up IFACE")
    return cmd(["ip", "link", "set", iface(a[0]), "up"])


def op_usb_authorize(a):
    need(len(a) == 2 and a[1] in ("0", "1"), "usb-authorize DEV 0|1")
    return sysfs_write(os.path.join(USB_DEVICES, usb_dev(a[0]), "authorized"), a[1])


def op_usb_config(a):
    need(len(a) == 4, "usb-config DEV DRIVER IFNUM CFG")
    dev = usb_dev(a[0])
    need(RE_DRIVER.match(a[1]) and os.path.isdir(os.path.join(USB_DRIVERS, a[1])), "no such USB driver")
    need(RE_IFNUM.match(a[2]) and RE_CFG.match(a[3]), "bad interface or configuration number")
    try:
        sysfs_write(os.path.join(USB_DRIVERS, a[1], "unbind"), "%s:%s" % (dev, a[2]))
    except OSError:
        pass                                   # not bound: nothing to unbind, carry on
    return sysfs_write(os.path.join(USB_DEVICES, dev, "bConfigurationValue"), a[3])


def op_usb_ports(a):
    need(len(a) == 0, "usb-ports")
    return cmd(["uhubctl"])


def op_usb_port(a):
    need(len(a) == 3, "usb-port LOC PORT on|off|cycle")
    need(RE_LOC.match(a[0]) and RE_PORT.match(a[1]) and a[2] in PORT_ACTS, "bad port arguments")
    return cmd(["uhubctl", "-l", a[0], "-p", a[1], "-a", a[2]])


UDEV_RULE_PATH = "/etc/udev/rules.d/70-opendock.rules"
UDEV_RULE = ('SUBSYSTEM=="tty", ATTRS{manufacturer}=="OpenDock", TAG+="uaccess", SYMLINK+="opendock%n"\n')


def op_install_udev_rule(a):
    need(len(a) == 0, "install-udev-rule")
    if DRY:
        print("write %s <- (rule)" % UDEV_RULE_PATH)
        return 0
    with open(UDEV_RULE_PATH, "w") as f:
        f.write("# written by DockPilot: the OpenDock serial port belongs to the logged-in user\n" + UDEV_RULE)
    os.chmod(UDEV_RULE_PATH, 0o644)
    cmd(["udevadm", "control", "--reload"])
    return cmd(["udevadm", "trigger", "--subsystem-match=tty"])


OPS = {
    "offload": op_offload, "speed": op_speed, "wol": op_wol, "wol-get": op_wol_get, "eeprom-get": op_eeprom_get, "charge-limit": op_charge_limit, "pause": op_pause,
    "renegotiate": op_renegotiate, "mac": op_mac, "link-up": op_link_up,
    "usb-authorize": op_usb_authorize, "usb-config": op_usb_config,
    "usb-ports": op_usb_ports, "usb-port": op_usb_port,
    "install-udev-rule": op_install_udev_rule,
}


def run_one(args):
    """Returns the exit code. Refusals print the reason and return 2."""
    try:
        need(args and isinstance(args, list) and all(isinstance(x, str) for x in args), "bad request")
        need(args[0] in OPS, "unknown operation %r" % (args[0],))
        return OPS[args[0]](args[1:])
    except Refused as e:
        print("refused: %s" % e)
        return 2
    except subprocess.TimeoutExpired:
        print("timed out")
        return 1
    except OSError as e:
        print("error: %s" % e)
        return 1


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            req = None
        rc = run_one(req)
        sys.stdout.write("__RC__%d\n" % rc)
        sys.stdout.flush()


def main(argv):
    global DRY, USB_DEVICES, USB_DRIVERS, NET, POWER_SUPPLY
    if argv and argv[0] == "--dry-run":          # tests only: print instead of doing, under a fake /sys
        DRY = True
        argv = argv[1:]
        if len(argv) >= 2 and argv[0] == "--sysfs":
            USB_DEVICES = os.path.join(argv[1], "bus/usb/devices")
            USB_DRIVERS = os.path.join(argv[1], "bus/usb/drivers")
            NET = os.path.join(argv[1], "class/net")
            POWER_SUPPLY = os.path.join(argv[1], "class/power_supply")
            argv = argv[2:]
    if argv == ["--serve"]:
        serve()
        return 0
    if not argv or argv[0].startswith("-"):
        print(__doc__.strip())
        return 2
    return run_one(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
