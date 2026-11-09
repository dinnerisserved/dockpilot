#!/usr/bin/env python3
"""
dockpilot_core.py — everything DockPilot knows and does, with no window attached.

Reads the dock straight from /sys, names the chip (Realtek and ASIX tables are in here), drives
the privileged helper, runs the automation rules and macros, talks to an OpenDock over its serial
port, writes snapshots. The window (dockpilot.py) is a view onto this file.

Run directly, this file IS the background service:  python3 dockpilot_core.py
(the .deb installs that as `dockpilotd` and a systemd user unit). The service runs the rules
with no window open, keeps the OpenDock link, writes ~/.cache/dockpilot/status.json for the
top-bar tile, and answers the window on a Unix socket, one JSON object per line.
"""

import glob
import os
import re
import sys
import json
import platform
import time
import random
import shutil
import socket
import threading
import collections
import subprocess
import urllib.request

try:
    import opendock                    # docks that speak the DockPilot Dock Protocol (OpenDock firmware)
except ImportError:
    try:
        from . import opendock
    except ImportError:
        opendock = None

# --------------------------------------------------------------------------- #
#  config — beside the script only
# --------------------------------------------------------------------------- #
APP_VERSION = "1.2"
WEBSITE = "opendocktech.com"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
def _pick_config_dir():
    """Prefer ./config next to the script; fall back to ~/.config/dockpilot when the
    script directory isn't writable (e.g. a system-wide pip install into site-packages)."""
    local = os.path.join(SCRIPT_DIR, "config")
    try:
        if SCRIPT_DIR.startswith(("/usr/", "/opt/")):          # installed system-wide: never write there
            raise PermissionError(SCRIPT_DIR)
        os.makedirs(local, exist_ok=True)
        probe = os.path.join(local, ".writetest")
        with open(probe, "w") as f:
            f.write("")
        os.remove(probe)
        return local
    except Exception:
        home = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"), "dockpilot")
        try:
            os.makedirs(home, exist_ok=True)
        except Exception:
            pass
        return home


CONFIG_DIR = _pick_config_dir()
DEVICES_FILE = os.path.join(CONFIG_DIR, "devices.json")
STATE_FILE = os.path.join(CONFIG_DIR, "dock_state.json")
AUTOMATION_FILE = os.path.join(CONFIG_DIR, "automation.json")
DOCKMEM_FILE = os.path.join(CONFIG_DIR, "dock_memory.json")
SEAT_FILE = os.path.join(CONFIG_DIR, "seat_key")          # this laptop's seat key: six words
DESK_STATE_FILE = os.path.join(CONFIG_DIR, "desk_applied.json")   # what was applied, to undo at undock

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception:
        return False

def load_devices():
    d = load_json(DEVICES_FILE, {})
    return {k.lower(): v for k, v in d.items()} if isinstance(d, dict) else {}

# --------------------------------------------------------------------------- #
#  low-level helpers
# --------------------------------------------------------------------------- #
def run(args, timeout=25):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).rstrip()
    except Exception as e:
        return 1, f"[error: {e}]"

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dockpilot_helper.py")

def priv(args):
    """A privileged request: a verb and its arguments for dockpilot_helper.py (see that
    file for the full list). The helper is the only thing that ever runs as root, and
    it refuses anything outside its list. Returned as a pkexec-prefixed argv so the
    runner knows to send it to the session helper rather than run it directly."""
    if os.geteuid() == 0:
        return [sys.executable, HELPER] + list(args)
    return ["pkexec", HELPER] + list(args)


class PrivShell:
    """One helper process per session, started through pkexec (one password prompt),
    fed one request per line. A lock serialises access so concurrent callers can't
    interleave on the shared pipe."""
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()

    def _ensure(self):
        if self.proc and self.proc.poll() is None:
            return True
        try:
            self.proc = subprocess.Popen(
                ["pkexec", HELPER, "--serve"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1)
            return True
        except Exception:
            self.proc = None
            return False

    def run(self, args, timeout=30):
        """args: the verb list (without any pkexec prefix). Returns (rc, output)."""
        if args and args[0] == "pkexec":
            args = args[2:]
        if os.geteuid() == 0:
            return run([sys.executable, HELPER] + list(args))
        with self.lock:
            if not self._ensure():
                return 1, "pkexec unavailable / dismissed"
            try:
                self.proc.stdin.write(json.dumps(list(args)) + "\n")
                self.proc.stdin.flush()
                result = {}
                def reader():
                    out = []
                    for line in self.proc.stdout:
                        if line.startswith("__RC__"):
                            result["rc"] = int(line.strip()[6:] or "1")
                            result["out"] = "".join(out).rstrip()
                            return
                        out.append(line)
                    result["rc"] = 1
                    result["out"] = "".join(out).rstrip()
                t = threading.Thread(target=reader, daemon=True)
                t.start()
                t.join(timeout)
                if t.is_alive():
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
                    self.proc = None
                    return 1, f"(privileged command timed out after {timeout}s)"
                return result.get("rc", 1), result.get("out", "")
            except Exception as e:
                self.proc = None
                return 1, str(e)


def have(b):
    return shutil.which(b) is not None

def read_sys(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""

def _hms(seconds):
    """3605 -> '1h 0m 5s'."""
    try:
        s = int(seconds)
    except Exception:
        return "?"
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return ("%dh %dm %ds" % (h, m, s)) if h else ("%dm %ds" % (m, s)) if m else ("%ds" % s)


def human(n):
    n = float(n)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PB"

# ---- network interface (pure sysfs) ----
def list_ifaces():
    try:
        return [n for n in os.listdir("/sys/class/net") if n != "lo"]
    except Exception:
        return []

def iface_is_usb(name):
    dev = f"/sys/class/net/{name}/device"
    return "usb" in os.path.realpath(dev) if os.path.exists(dev) else False

def iface_is_wifi(name):
    return os.path.exists(f"/sys/class/net/{name}/wireless") or name.startswith(("wl", "wlan", "wlp"))

OPENDOCK_LINK_MAC = "02:0d:0c"     # the dock's own management link (firmware 1.0, CDC-NCM): not a dock NIC


def iface_is_opendock_link(name):
    """The OpenDock management board's network link, which serves http://opendock.local. It is
    a USB network interface too, but it is the dock talking, not the dock's Ethernet chip."""
    return net_mac(name).startswith(OPENDOCK_LINK_MAC)


def usb_eth_ifaces():
    return [n for n in list_ifaces() if iface_is_usb(n) and not iface_is_opendock_link(n)]

def in_dialout_group():
    """Serial/dock access needs the user in the 'dialout' group on most distros."""
    try:
        import grp
        if os.geteuid() == 0:
            return True
        return "dialout" in {grp.getgrgid(g).gr_name for g in os.getgroups()}
    except Exception:
        return True          # can't tell — don't cry wolf


def serial_ports_present():
    try:
        return any(n.startswith(("ttyACM", "ttyUSB")) for n in os.listdir("/dev"))
    except Exception:
        return False


def dialout_hint():
    """Hint string if serial ports exist but we lack permission to open them, else ''."""
    if serial_ports_present() and not in_dialout_group():
        return ("Serial device(s) are present, but your user isn't in the 'dialout' group — "
                "DockPilot can't open them.\n\n"
                "Fix:  sudo usermod -aG dialout $USER\n"
                "then log out and back in.")
    return ""


def find_dock_iface():
    return usb_eth_ifaces()[0] if usb_eth_ifaces() else ""

def primary_host_iface(exclude):
    for n in list_ifaces():
        if n != exclude and not iface_is_usb(n) and not iface_is_wifi(n):
            return n
    for n in list_ifaces():
        if n != exclude and iface_is_wifi(n):
            return n
    return ""

def net_mac(name):    return read_sys(f"/sys/class/net/{name}/address").lower()
def net_state(name):  return read_sys(f"/sys/class/net/{name}/operstate") or "?"
def net_carrier(name): return read_sys(f"/sys/class/net/{name}/carrier") == "1"

# ---- native-mode detection + tuning (ASIX / Realtek) ----
NIC_VENDORS = {"0b95": "ASIX", "0bda": "Realtek"}
NATIVE_DRIVERS = {"ax88179_178a", "ax88179a", "r8152", "r8153"}
CDC_DRIVERS = {"cdc_ncm", "cdc_ether", "cdc_eem", "cdc_mbim", "r8153_ecm"}   # r8153_ecm: Realtek's own ECM stand-in

# --------------------------------------------------------------------------- #
#  Realtek identification — everything below comes from Realtek's own r8152
#  driver source (v2.22.1, 2026): its USB device table, its named dock IDs,
#  its chip-version list and its MAC pass-through rules. Nothing here needs
#  that driver installed; it is knowledge, not code, and it lets DockPilot
#  name what it sees where the kernel only shows a vendor id.
# --------------------------------------------------------------------------- #
# Realtek NICs sold under other USB vendor ids (they still switch to r8152 the same way)
REALTEK_REBRANDS = {
    ("045e", "07ab"), ("045e", "07c6"), ("045e", "0927"), ("045e", "0c5e"),        # Microsoft
    ("04e8", "a101"),                                                               # Samsung
    ("17ef", "304f"), ("17ef", "3052"), ("17ef", "3054"), ("17ef", "3057"),        # Lenovo
    ("17ef", "3062"), ("17ef", "3069"), ("17ef", "3082"), ("17ef", "3098"),
    ("17ef", "7205"), ("17ef", "720a"), ("17ef", "720b"), ("17ef", "720c"),
    ("17ef", "7214"), ("17ef", "721e"), ("17ef", "8153"), ("17ef", "a359"), ("17ef", "a387"),
    ("13b1", "0041"),                                                               # Linksys
    ("0955", "09ff"),                                                               # Nvidia
    ("2357", "0601"),                                                               # TP-Link
    ("0b05", "1976"), ("0b05", "1d91"),                                             # ASUS
    ("2baf", "0012"),
}
# What a Realtek product id says by itself (the exact part comes from the firmware name, below)
REALTEK_PIDS = {
    "8152": "RTL8152 (10/100)", "8153": "RTL8153 (gigabit)",
    "8156": "RTL8156 (2.5 GbE)", "8157": "RTL8157 (5 GbE)",
    # generic ids Realtek uses for several parts (8053 with a hardware rev of e0.xx is the
    # three-configuration layout: NCM, ECM, then vendor mode as #3)
    "8050": "Realtek USB Ethernet (product id 8050)", "8053": "Realtek USB Ethernet (product id 8053)",
    "8155": "Realtek USB Ethernet (product id 8155)", "815a": "Realtek USB Ethernet (product id 815a)",
}
REBRAND_VENDORS = {"045e": "Microsoft", "04e8": "Samsung", "17ef": "Lenovo", "13b1": "Linksys",
                   "0955": "Nvidia", "2357": "TP-Link", "0b05": "ASUS"}
# Chip version codes the Realtek driver reads from the silicon, and what they are.
# The kernel's own r8152 names its firmware files after these ("rtl8153a-3", "rtl8156b-2"),
# so the file it loads — visible in `ethtool -i` and the kernel log — tells the exact variant.
REALTEK_VARIANTS = {
    "rtl8152b": "RTL8152B (10/100)", "rtl8153a": "RTL8153A (gigabit)", "rtl8153b": "RTL8153B (gigabit)",
    "rtl8153c": "RTL8153C (gigabit)", "rtl8156a": "RTL8156A (2.5 GbE)", "rtl8156b": "RTL8156B (2.5 GbE)",
    "rtl8157": "RTL8157 (5 GbE)", "rtl8159": "RTL8159 (10 GbE)",
}
# Docks Realtek's driver names outright, and the ones it knows carry the host's own MAC
# through from BIOS (Lenovo via ACPI \MACA; Dell docks do the same via \_SB.AMAC on
# RTL8153-AD/-BD/-BND silicon, which only the driver can tell apart).
DOCK_NAMES = {
    ("17ef", "3054"): "ThinkPad OneLink+ Dock", ("17ef", "3082"): "ThinkPad Thunderbolt 3 Dock Gen 2",
    ("17ef", "a387"): "ThinkPad USB-C Dock Gen 2", ("17ef", "3062"): "ThinkPad USB-C Dock Gen 3",
    ("17ef", "721e"): "Lenovo USB-C Travel Hub", ("17ef", "720c"): "ThinkPad USB-C dongle",
}
MAC_PASSTHRU_DOCKS = set(DOCK_NAMES)
USB_CLASS_NAMES = {0x02: "CDC (standard)", 0x0a: "CDC data", 0xff: "vendor (native)", 0x09: "hub", 0xe0: "wireless"}


def usb_configs(name):
    """Every USB configuration the device offers, from its raw descriptors in sysfs:
    [{"value": "1", "class": 0xff, "kind": "vendor (native)", "ifaces": 1}, ...].
    Realtek's driver finds native mode by class, not by number — "the vendor mode is
    not always config #1" — so DockPilot does the same."""
    try:
        with open(os.path.join(USB_ROOT, name, "descriptors"), "rb") as f:
            d = f.read()
    except Exception:
        return []
    out, i, cur = [], 0, None
    while i + 2 <= len(d):
        ln, typ = d[i], d[i + 1]
        if ln < 2:
            break
        if typ == 0x02 and ln >= 9:                       # configuration descriptor
            cur = {"value": str(d[i + 5]), "class": None, "kind": "?", "ifaces": d[i + 4]}
            out.append(cur)
        elif typ == 0x04 and ln >= 9 and cur is not None and cur["class"] is None and d[i + 3] == 0:
            cur["class"] = d[i + 5]                       # first interface's class
            cur["kind"] = USB_CLASS_NAMES.get(d[i + 5], "class 0x%02x" % d[i + 5])
        i += ln
    return out


def realtek_identity(vid, pid, driver="", fw_version="", parent_vid=""):
    """Name a Realtek NIC as precisely as what we can see allows.
    name: the best name we have (exact part if the firmware name told us, else the product id's)
    variant: the exact part from the firmware name, "" if unknown
    sold_as: "Lenovo" etc. for Realtek silicon behind another vendor's USB id, "" for 0bda
    dock: the dock Realtek's driver names for this id, "" otherwise
    mac_passthru: True if Realtek's driver would take the laptop's BIOS MAC for this dock"""
    if vid != "0bda" and (vid, pid) not in REALTEK_REBRANDS:
        return None
    sold_as = "" if vid == "0bda" else REBRAND_VENDORS.get(vid, "another vendor")
    variant = ""
    m = re.match(r"(rtl81\d\d[a-z]?)(?:-(\d+))?", fw_version or "")
    if m:
        variant = REALTEK_VARIANTS.get(m.group(1), m.group(1).upper())
        if m.group(2):
            variant += ", silicon rev %s" % m.group(2)
    if variant:
        name = variant
    elif sold_as:
        name = "Realtek silicon (exact part unknown)"
    else:
        name = REALTEK_PIDS.get(pid, "Realtek USB Ethernet (product id %s)" % pid)
    passthru = (vid, pid) in MAC_PASSTHRU_DOCKS or (vid == "0bda" and pid == "8153" and parent_vid == "17ef")
    return {"name": name, "variant": variant, "sold_as": sold_as,
            "dock": DOCK_NAMES.get((vid, pid), ""), "mac_passthru": passthru}


def ethtool_info(iface):
    """driver / version / firmware-version / bus-info from `ethtool -i`, or {}."""
    if not have("ethtool") or not iface:
        return {}
    rc, out = run(["ethtool", "-i", iface], timeout=5)
    info = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    return info


def kernel_log_firmware(iface):
    """The firmware file the kernel's r8152 loaded for this NIC, from the kernel log
    (e.g. "rtl8156b-2"). Empty if the log isn't readable or nothing was loaded."""
    for cmd in (["journalctl", "-k", "--no-pager", "-o", "cat", "-n", "4000"], ["dmesg"]):
        if not have(cmd[0]):
            continue
        rc, out = run(cmd, timeout=8)
        if rc != 0:
            continue
        found = ""
        for line in out.splitlines():
            if "r8152" in line and "rtl_nic/" in line:
                m = re.search(r"rtl_nic/(rtl81\w+(?:-\d+)?)\.fw", line)
                if m:
                    found = m.group(1)                    # keep the last one seen
        return found
    return ""

def nic_chip(iface):
    """Identify the dock NIC's chip vendor, driver, and USB config state."""
    if not iface:
        return None
    name = dock_nic_usb_name(iface)
    if not name:
        return None
    info = usb_info(name)
    vendor = NIC_VENDORS.get(info["vid"])
    if not vendor and (info["vid"], info["pid"]) in REALTEK_REBRANDS:
        vendor = "Realtek"
    configs = usb_configs(name)
    vendor_cfg = next((c["value"] for c in configs if c["class"] == 0xff), "")
    cdc_cfg = next((c["value"] for c in configs if c["class"] == 0x02), "")
    parent = os.path.basename(os.path.dirname(os.path.realpath(os.path.join(USB_ROOT, name))))
    parent_vid = _u(os.path.join(USB_ROOT, parent), "idVendor") if parent else ""
    return {
        "usb": name, "vid": info["vid"], "pid": info["pid"], "vendor": vendor,
        "product": info["product"] or "", "bcd": info["bcd"] or "",
        "nconfigs": int(read_sys(os.path.join(USB_ROOT, name, "bNumConfigurations") or "1") or 1),
        "config": read_sys(os.path.join(USB_ROOT, name, "bConfigurationValue")),
        "configs": configs, "vendor_config": vendor_cfg, "cdc_config": cdc_cfg,
        "parent_vid": parent_vid,
        "driver": net_driver(iface),
    }

def native_state(iface):
    """Returns (state, chip): state in {native, unlockable, unsupported}."""
    c = nic_chip(iface)
    if not c or c["vendor"] not in ("ASIX", "Realtek"):
        return "unsupported", c
    if c["driver"] in NATIVE_DRIVERS:
        return "native", c
    if c["driver"] in CDC_DRIVERS and c["nconfigs"] > 1 and (c["vendor_config"] or not c["configs"]):
        return "unlockable", c
    return "unsupported", c

def send_magic_packet(mac):
    """Pure-Python Wake-on-LAN: broadcast the 102-byte magic packet. No external tool."""
    clean = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(clean) != 12:
        return False, "MAC must be 12 hex digits (aa:bb:cc:dd:ee:ff)"
    try:
        payload = bytes.fromhex("ff" * 6 + clean * 16)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(payload, ("255.255.255.255", 9))
        s.close()
        return True, "magic packet sent"
    except Exception as e:
        return False, str(e)

def wol_state(iface, privshell=None):
    """'g' (armed), 'd' (disarmed) or '?'. Modern ethtool only reports Wake-on to an administrator,
    so with a helper session available the (read-only) wol-get verb is used."""
    if not have("ethtool"):
        return "?"
    out = run(["ethtool", iface])[1]
    m = re.search(r"^\s*Wake-on:\s*(\S+)", out, re.M)
    if not m and privshell is not None:
        rc, out = privshell.run(priv(["wol-get", iface]))
        m = re.search(r"^\s*Wake-on:\s*(\S+)", out or "", re.M)
    return m.group(1) if m else "?"


def wol_words(state):
    return {"g": "armed — wakes on magic packet", "d": "disarmed", "?": "not readable"}.get(state, state)

def ethtool_offloads(iface):
    if not have("ethtool"):
        return {}
    rc, out = run(["ethtool", "-k", iface])
    feats = {}
    for line in out.splitlines():
        m = re.match(r"([\w-]+):\s*(on|off)", line.strip())
        if m:
            feats[m.group(1)] = (m.group(2) == "on")
    return feats

def ethtool_stats(iface):
    if not have("ethtool"):
        return "ethtool not installed."
    rc, out = run(["ethtool", "-S", iface])
    return out if rc == 0 and out.strip() and "no stats" not in out.lower() else "No per-chip statistics exposed."

def ethtool_coalesce(iface):
    if not have("ethtool"):
        return None
    rc, out = run(["ethtool", "-c", iface])
    if "not supported" in out.lower() or rc != 0:
        return None
    return out


def eeprom_dump(iface, length=64, privshell=None):
    """Return (supported, all_ff, hex_lines). Reads only — never writes. Modern ethtool only
    lets an administrator read, so the helper's read-only eeprom-get verb is tried second."""
    if not have("ethtool"):
        return False, False, "ethtool not installed"
    rc, out = run(["ethtool", "-e", iface, "offset", "0", "length", str(length)])
    if rc != 0 and privshell is not None and "not permitted" in out.lower():
        rc, out = privshell.run(priv(["eeprom-get", iface]))
    if rc != 0 or "not supported" in out.lower() or "Cannot get" in out:
        return False, False, ""
    vals = re.findall(r"\b([0-9a-fA-F]{2})\b", out.split("Values", 1)[-1])
    all_ff = bool(vals) and all(v.lower() == "ff" for v in vals)
    return True, all_ff, out.strip()


def eeprom_backup(iface, privshell=None):
    """Save the whole EEPROM as an ethtool hex dump in the home folder. Returns (ok, path_or_msg)."""
    if not have("ethtool"):
        return False, "ethtool not installed"
    rc, out = run(["ethtool", "-e", iface])
    if rc != 0 and privshell is not None and "not permitted" in out.lower():
        rc, out = privshell.run(priv(["eeprom-get", iface, "full"]))
    if rc != 0 or "Cannot get" in out or not out.strip():
        return False, "this driver gives no EEPROM access"
    path = os.path.join(os.path.expanduser("~"), "dockpilot-eeprom-%s-%s.txt" % (iface, time.strftime("%Y%m%d-%H%M%S")))
    with open(path, "w") as f:
        f.write(out)
    return True, path



def net_speed(name):
    """Negotiated link speed. Prefer sysfs: some drivers (seen on RTL8156B) misreport
    advertised modes via ethtool while /sys/class/net/*/speed is correct."""
    s_val = read_sys(f"/sys/class/net/{name}/speed")
    if s_val and s_val not in ("-1", "0"):
        return f"{s_val} Mb/s"
    return "—"

def net_driver(name):
    link = f"/sys/class/net/{name}/device/driver"
    return os.path.basename(os.path.realpath(link)) if os.path.exists(link) else "—"

def nic_mode(name):
    """The NIC's operating-mode string (e.g. 'CDC NCM (NO ZLP)'), if ethtool is present."""
    if not have("ethtool"):
        return ""
    rc, out = run(["ethtool", "-i", name])
    m = re.search(r"^firmware-version:[ \t]*(.*)$", out, re.M)
    return m.group(1).strip() if m else ""

def net_stat(name, key):
    try:
        return int(read_sys(f"/sys/class/net/{name}/statistics/{key}") or "0")
    except Exception:
        return 0

def net_bytes(name):
    return net_stat(name, "rx_bytes"), net_stat(name, "tx_bytes")

def get_ipv4(name):
    # avoid needing `ip`: read from /proc? Simpler: use socket via getifaddrs is not stdlib.
    rc, out = run(["cat", f"/sys/class/net/{name}/operstate"])  # placeholder to keep pure
    # Fall back to `ip` only if present (it almost always is, part of iproute2/core)
    if have("ip"):
        rc, o = run(["ip", "-o", "-4", "addr", "show", "dev", name])
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", o)
        return m.group(1) if m else "—"
    return "—"

# ---- original MAC (so 'reset' always works, even with no ethtool) ----
def original_mac(iface, current):
    if have("ethtool"):
        rc, out = run(["ethtool", "-P", iface])
        m = re.search(r"([0-9a-f:]{17})", out)
        if m and m.group(1) != "00:00:00:00:00:00":
            return m.group(1)
    st = load_json(STATE_FILE, {})
    key = f"origmac:{iface}"
    if key not in st:
        st[key] = current
        save_json(STATE_FILE, st)
    return st.get(key, current)

def random_mac():
    o = [random.randint(0, 255) for _ in range(6)]
    o[0] = (o[0] & 0xFC) | 0x02
    return ":".join(f"{x:02x}" for x in o)

# --------------------------------------------------------------------------- #
#  USB tree via sysfs — this is what scopes everything to the DOCK only
# --------------------------------------------------------------------------- #
USB_ROOT = "/sys/bus/usb/devices"

def _u(path, attr):
    return read_sys(os.path.join(path, attr))

def usb_info(name):
    p = os.path.join(USB_ROOT, name)
    return {
        "name": name, "path": p,
        "vid": _u(p, "idVendor"), "pid": _u(p, "idProduct"),
        "product": _u(p, "product"), "manufacturer": _u(p, "manufacturer"),
        "class": _u(p, "bDeviceClass"),
        "maxchild": int(_u(p, "maxchild") or 0),
        "speed": _u(p, "speed"), "bcd": _u(p, "bcdDevice"),
        "busnum": _u(p, "busnum"), "devnum": _u(p, "devnum"),
    }

def usb_device_names():
    try:
        return [n for n in os.listdir(USB_ROOT) if ":" not in n and re.match(r"^\d+-\d+", n) or n.startswith("usb")]
    except Exception:
        return []

def dock_nic_usb_name(iface):
    """From the dock NIC interface, find its USB device dir name (e.g. 3-1.4.3)."""
    if not iface:
        return ""
    real = os.path.realpath(f"/sys/class/net/{iface}/device")
    b = os.path.basename(real)
    hops = 0
    while real and hops < 8 and not re.match(r"^\d+-\d+(\.\d+)*$", b):
        real = os.path.dirname(real)
        b = os.path.basename(real)
        hops += 1
    return b if re.match(r"^\d+-\d+(\.\d+)*$", b) else ""

def dock_top_hub(nic_name):
    """The dock's upstream hub attached to a root-hub port, e.g. 3-1.4.3 -> 3-1."""
    m = re.match(r"^(\d+-\d+)", nic_name)
    return m.group(1) if m else ""

def _internal_net_top_hubs():
    """Top-hub prefixes that belong to built-in NICs (so we can exclude the laptop's
    own internal hubs when guessing which external hub is 'the dock')."""
    tops = set()
    for n in list_ifaces():
        if iface_is_usb(n):
            continue
        # built-in NICs live on PCI, not USB — nothing to add; this is a placeholder
    return tops

def external_top_hubs():
    """Best-effort: top-level hubs (children of a root hub) that look like an external
    dock/hub rather than the laptop's own internals. Used when there's no NIC to anchor on.
    Returns a list of top-hub names like ['3-2']."""
    tops = []
    try:
        for n in os.listdir(USB_ROOT):
            if ":" in n or n.startswith("usb"):
                continue
            # top-level device = exactly one segment after bus (e.g. '3-2', not '3-2.1')
            if not re.match(r"^\d+-\d+$", n):
                continue
            info = usb_info(n)
            if info["class"] != "09":          # only hubs anchor a subtree
                continue
            # skip obvious built-in webcam/bluetooth hubs by having no downstream children
            # (a dock hub with something plugged in will have members; keep any hub though)
            tops.append(n)
    except Exception:
        pass
    # prefer hubs that actually have downstream devices (a used dock), but keep all
    tops.sort(key=lambda t: (len(dock_members(t)) == 1, t))
    return tops

def dock_members(top):
    """All USB device dirs that belong to the dock (its hub subtree)."""
    if not top:
        return []
    names = []
    try:
        for n in os.listdir(USB_ROOT):
            if ":" in n:
                continue
            if n == top or n.startswith(top + "."):
                names.append(n)
    except Exception:
        pass
    return sorted(names, key=lambda s: [int(x) for x in re.split(r"[-.]", s)])

def dock_leaf_devices(top):
    """Non-hub devices inside the dock (NIC, card reader, whatever's plugged in)."""
    out = []
    for n in dock_members(top):
        info = usb_info(n)
        if info["class"] != "09" and n != top:   # 09 = hub
            out.append(info)
    return out

def build_dock_tree(top):
    members = set(dock_members(top))
    def node(name, depth, port=None):
        info = usb_info(name)
        children = []
        for p in range(1, info["maxchild"] + 1):
            child = f"{name}.{p}"
            if child in members:
                children.append(node(child, depth + 1, p))
            else:
                children.append({"empty": True, "port": p, "depth": depth + 1})
        return {"info": info, "depth": depth, "port": port, "children": children, "empty": False}
    return node(top, 0) if top else None

def flatten_tree(node, out):
    if node is None:
        return
    if node.get("empty"):
        out.append((node["depth"], f"Port {node['port']} — empty", "empty"))
        return
    info = node["info"]
    label_name = info["product"] or f"{info['vid']}:{info['pid']}"
    if node["depth"] == 0:
        label = f"DOCK HUB · {label_name} · {info['maxchild']} ports"
        kind = "hub"
    else:
        is_hub = info["class"] == "09"
        label = f"Port {node['port']}: {label_name}"
        if is_hub:
            label += f"  ·  hub, {info['maxchild']} ports"
        kind = "hub" if is_hub else "dev"
    out.append((node["depth"], label, kind))
    for ch in node["children"]:
        flatten_tree(ch, out)

def usb_authorized_file(path):
    f = os.path.join(path, "authorized")
    return f if os.path.exists(f) else ""

# ---- reachability ----
def default_gw():
    if not have("ip"):
        return ""
    rc, out = run(["ip", "route", "show", "default"])
    m = re.search(r"default via (\S+)", out)
    return m.group(1) if m else ""

def ping_ms(host):
    if not host or not have("ping"):
        return None
    rc, out = run(["ping", "-c", "1", "-W", "1", "-n", host], timeout=3)
    m = re.search(r"time=([\d.]+)", out)
    return float(m.group(1)) if m else None

def dns_ms():
    t = time.time()
    try:
        socket.getaddrinfo("example.com", 80)
        return (time.time() - t) * 1000
    except Exception:
        return None

def public_ip():
    try:
        return urllib.request.urlopen("https://api.ipify.org", timeout=3).read().decode().strip()
    except Exception:
        return None

def lan_count():
    if not have("ip"):
        return 0
    rc, out = run(["ip", "-j", "neigh"])
    try:
        return len([x for x in json.loads(out) if x.get("lladdr") and ":" not in x.get("dst", "")])
    except Exception:
        return 0

# ---- audio (optional) ----
def list_sinks():
    if not have("pactl"):
        return []
    rc, out = run(["pactl", "list", "short", "sinks"])
    return [ln.split("\t")[1] for ln in out.splitlines() if "\t" in ln]

def default_sink():
    rc, out = run(["pactl", "get-default-sink"])
    return out.strip() if rc == 0 and out.strip() else "@DEFAULT_SINK@"

def sink_volume(sink):
    rc, out = run(["pactl", "get-sink-volume", sink])
    m = re.search(r"(\d+)%", out)
    return int(m.group(1)) if m else 0

# --------------------------------------------------------------------------- #


MACROS_FILE = os.path.join(CONFIG_DIR, "macros.json")

TRIGGERS = ["connect", "disconnect", "port_on", "port_off", "opendock_connect", "opendock_disconnect"]



# --------------------------------------------------------------------------- #
#  Starter templates — editable examples, NOT defaults that run.
#  Deliberately generic: we don't know the user's desktop, apps or scripts.
#  Every one is created disabled so nothing ever fires unexpectedly.
# --------------------------------------------------------------------------- #
TEMPLATES = [
    {
        "name": "Notify on dock/undock",
        "trigger": "connect",
        "dock": "",
        "enabled": False,
        "commands": ['notify-send "Docked" "Dock connected"'],
        "note": "Simplest possible macro. Needs a desktop that has notify-send.",
    },
    {
        "name": "Lock screen on undock",
        "trigger": "disconnect",
        "dock": "",
        "enabled": False,
        "commands": ["loginctl lock-session"],
        "note": "Walk-away security. Your desktop may use a different lock command.",
    },
    {
        "name": "Mute audio on undock",
        "trigger": "disconnect",
        "dock": "",
        "enabled": False,
        "commands": ["pactl set-sink-mute @DEFAULT_SINK@ 1"],
        "note": "Stops sound blasting from laptop speakers when you unplug.",
    },
    {
        "name": "Run my own script on connect",
        "trigger": "connect",
        "dock": "",
        "enabled": False,
        "commands": ["$HOME/bin/on-dock.sh"],
        "note": "The flexible one: point it at your own script and do whatever you like.",
    },
    {
        "name": "Restore monitor layout on connect",
        "trigger": "connect",
        "dock": "",
        "enabled": False,
        "commands": ["# X11 example — edit for your setup:",
                     "# xrandr --output HDMI-1 --auto --right-of eDP-1",
                     "# Wayland users: wlr-randr or kanshi instead"],
        "note": "Display layout is desktop-specific, so this is a stub to edit. "
                "DockPilot deliberately doesn't try to be a window manager.",
    },
]


# --------------------------------------------------------------------------- #
#  Store
# --------------------------------------------------------------------------- #
class MacroStore:
    def __init__(self, path=MACROS_FILE):
        self.path = path
        self.macros = []
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            self.macros = data if isinstance(data, list) else []
        except Exception:
            self.macros = []
        return self.macros

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self.macros, f, indent=2)
            return True
        except Exception:
            return False

    def add(self, macro):
        self.macros.append(macro)
        self.save()

    def remove(self, idx):
        if 0 <= idx < len(self.macros):
            self.macros.pop(idx)
            self.save()

    def update(self, idx, macro):
        if 0 <= idx < len(self.macros):
            self.macros[idx] = macro
            self.save()

    def add_templates(self):
        """Add any starter templates not already present (by name). All disabled."""
        have = {m.get("name") for m in self.macros}
        added = 0
        for t in TEMPLATES:
            if t["name"] not in have:
                m = dict(t)
                m.pop("note", None)
                self.macros.append(m)
                added += 1
        if added:
            self.save()
        return added

    def matching(self, trigger, dock_model=""):
        out = []
        for m in self.macros:
            if not m.get("enabled"):
                continue
            if m.get("trigger") != trigger:
                continue
            want = (m.get("dock") or "").strip()
            if want and want != (dock_model or ""):
                continue
            out.append(m)
        return out


# --------------------------------------------------------------------------- #
#  Runner
# --------------------------------------------------------------------------- #
def run_commands(commands, log=None, env_extra=None):
    """Run a list of shell commands in order, on a worker thread. Never raises."""
    def worker():
        env = dict(os.environ)
        if env_extra:
            env.update({k: str(v) for k, v in env_extra.items()})
        for cmd in commands:
            c = (cmd or "").strip()
            if not c or c.startswith("#"):
                continue                      # blank lines and comments are skipped
            if log:
                log("$ %s" % c)
            try:
                p = subprocess.run(c, shell=True, capture_output=True, text=True,
                                   timeout=60, env=env)
                out = ((p.stdout or "") + (p.stderr or "")).strip()
                if log and out:
                    log(out[:500])
                if log and p.returncode != 0:
                    log("(exit %d)" % p.returncode)
            except subprocess.TimeoutExpired:
                if log:
                    log("(timed out after 60s)")
            except Exception as e:
                if log:
                    log("(error: %s)" % e)
    threading.Thread(target=worker, daemon=True).start()


def run_macros(store, trigger, log=None, dock_model="", **env_extra):
    """Fire every enabled macro matching this trigger (and dock, if scoped)."""
    fired = 0
    for m in store.matching(trigger, dock_model):
        if log:
            log("# macro: %s (%s)" % (m.get("name", "unnamed"), trigger))
        run_commands(m.get("commands", []), log=log, env_extra=env_extra)
        fired += 1
    return fired



# --------------------------------------------------------------------------- #
#  The seat — this laptop at an OpenDock (firmware 0.6)
#
#  The dock holds the desk (tag, options, keys) and, per laptop, a seat: rules,
#  own commands, a saved configuration. DockPilot is a client: it reads what the
#  dock hands out and does the laptop-side work. The seat is filed in the dock
#  under an id derived from a secret the laptop keeps (six words); the dock never
#  sees the words, so it never learns who docked. Give the words to another
#  laptop and it is the same seat at every dock.
# --------------------------------------------------------------------------- #
SEAT_WORDS = (
    "acorn amber anchor apple arrow aspen atlas autumn badger bamboo barley basil beacon berry birch "
    "bison blossom bolt bonfire breeze bridge brook butter cabin cactus camel candle canyon carbon "
    "castle cedar cello chalk cherry cider cinder clover cobalt cocoa comet copper coral cotton cradle "
    "crane cricket crystal cumin daisy dawn delta denim desert dune eagle ember engine falcon feather "
    "fennel fern fiddle finch flame flint forest fossil garden garnet ginger glacier goose granite "
    "gravel hammer harbor harvest hazel heron honey hornet iceberg indigo iris island ivory jade jasper "
    "jungle juniper kettle kiwi lagoon lantern lava lemon lily linen lobster locust lotus lumber magnet "
    "mango maple marble meadow melon meteor mint mirror mocha moss moth muffin nickel noodle nutmeg "
    "oasis ocean olive onyx orbit orchid otter oyster panda paper parrot pebble pepper pine plum poppy "
    "prairie pumpkin quartz quiver rabbit radar raven reef ribbon river rocket rose ruby saddle saffron "
    "salmon sandal sapphire scarf shadow silk silver sketch slate smoke sparrow spider spruce squid "
    "stone storm sugar summer sunset swan tango thistle thunder tiger timber topaz torch tulip tundra "
    "turtle umbrella valley vanilla velvet violet walnut walrus wheat willow winter wolf yarrow zebra "
    "zinc seven tide cloud north south east west gold iron oak elm ash yew bay fig alder anvil apron "
    "arch attic aurora avocado bagel ballad banjo barn basin bead beetle bell belt biscuit blanket bluff "
    "boat boot bottle bow brick broom bucket bugle bulb bun burrow cable cake camera canal cane canoe "
    "cape card cargo carrot cart cave chair chapel chess chime chip clay "
).split()
assert len(SEAT_WORDS) >= 256
SEAT_WORDS = SEAT_WORDS[:256]
SEAT_LEN = 6


def seat_phrase_new():
    """Six random words, e.g. copper-lantern-orbit-seven-maple-tide."""
    import secrets
    return "-".join(secrets.choice(SEAT_WORDS) for _ in range(SEAT_LEN))


def seat_phrase_clean(text):
    """A phrase as typed: words in any case, separated by spaces or dashes.
    Returns the canonical phrase or None if it is not one."""
    words = [w for w in re.split(r"[\s\-_,]+", (text or "").strip().lower()) if w]
    if len(words) != SEAT_LEN or any(w not in SEAT_WORDS for w in words):
        return None
    return "-".join(words)


def seat_id(phrase):
    """The 16 hex characters the dock files the seat under. A slow hash, so the
    id on the dock cannot be turned back into the words in any useful time."""
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", phrase.encode("utf-8"), b"opendock-seat", 200000)[:8].hex()


def seat_load(path=None):
    """This laptop's phrase, made on first use and kept with owner-only permissions."""
    path = path or SEAT_FILE
    try:
        with open(path) as f:
            ph = seat_phrase_clean(f.read())
        if ph:
            return ph
    except Exception:
        pass
    ph = seat_phrase_new()
    seat_save(ph, path)
    return ph


def seat_save(phrase, path=None):
    path = path or SEAT_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(phrase + "\n")
        return True
    except Exception:
        return False


# What the laptop does with each rule the dock knows. Unknown rule ids (a newer dock)
# are shown by the app and ignored here. (tool needed, or None)
RULE_TOOLS = {"wifi_off": "nmcli", "audio_dock": "pactl", "lock_undock": "loginctl", "mute_undock": "pactl",
              "touchpad_off": "gsettings", "vpn": "nmcli", "proxy": "gsettings", "tile": None}
# Desk options this laptop can apply, and what it needs. The rest (ntp) is kept by the dock and shown.
OPT_TOOLS = {"vlan": "nmcli", "mtu": "nmcli", "addr": "nmcli", "dns": "nmcli", "domain": "nmcli",
             "tz": "timedatectl", "proxy": "gsettings", "vpn": "nmcli"}


def desk_plan(desk, seat, connected, iface, have_fn=None):
    """What to run for this desk and seat, as a list of (note, argv-or-shell) steps.
    Pure: no side effects, so it can be tested. `have_fn` says which tools exist.
    Returns (steps, applied, skipped) — applied/skipped are option ids, for the log line."""
    have_fn = have_fn or have
    opt = (desk or {}).get("opt") or {}
    rules = (seat or {}).get("rules") or {}
    steps, applied, skipped = [], [], []

    def nm_con():
        return 'c=$(nmcli -g GENERAL.CONNECTION device show "%s" 2>/dev/null); [ -n "$c" ]' % iface

    # -- desk options: in force while docked, undone at undock
    if opt.get("vlan"):
        if have_fn("nmcli") and iface:
            name = "opendock-vlan-%s" % opt["vlan"]
            if connected:
                steps.append(("VLAN %s on %s" % (opt["vlan"], iface),
                              'nmcli connection add type vlan con-name "%s" ifname "%s.%s" dev "%s" id %s >/dev/null 2>&1; nmcli connection up "%s"'
                              % (name, iface, opt["vlan"], iface, opt["vlan"], name)))
            else:
                steps.append(("VLAN removed", 'nmcli connection delete "%s" 2>/dev/null || true' % name))
            applied.append("vlan")
        else:
            skipped.append("vlan")
    if opt.get("mtu"):
        if have_fn("nmcli") and iface:
            steps.append(("MTU %s" % opt["mtu"] if connected else "MTU back to automatic",
                          nm_con() + ' && nmcli con mod "$c" 802-3-ethernet.mtu %s && nmcli con up "$c" >/dev/null' % (opt["mtu"] if connected else 0)))
            applied.append("mtu")
        else:
            skipped.append("mtu")
    if opt.get("addr"):
        if have_fn("nmcli") and iface:
            parts = opt["addr"].split()
            ip, gw = parts[0], (parts[1] if len(parts) > 1 else "")
            if connected:
                steps.append(("fixed address %s" % ip,
                              nm_con() + ' && nmcli con mod "$c" ipv4.method manual ipv4.addresses "%s" ipv4.gateway "%s" && nmcli con up "$c" >/dev/null' % (ip, gw)))
            else:
                steps.append(("address from the network again",
                              nm_con() + ' && nmcli con mod "$c" ipv4.method auto ipv4.addresses "" ipv4.gateway "" && nmcli con up "$c" >/dev/null'))
            applied.append("addr")
        else:
            skipped.append("addr")
    if opt.get("dns") or opt.get("domain"):
        if have_fn("nmcli") and iface:
            dns = opt.get("dns", "").replace(",", " ")
            if connected:
                steps.append(("DNS %s%s" % (dns, (" search " + opt["domain"]) if opt.get("domain") else ""),
                              nm_con() + ' && nmcli con mod "$c" ipv4.dns "%s" ipv4.dns-search "%s" ipv4.ignore-auto-dns %s && nmcli con up "$c" >/dev/null'
                              % (dns, opt.get("domain", ""), "yes" if dns else "no")))
            else:
                steps.append(("DNS from the network again",
                              nm_con() + ' && nmcli con mod "$c" ipv4.dns "" ipv4.dns-search "" ipv4.ignore-auto-dns no && nmcli con up "$c" >/dev/null'))
            applied.append("dns")
        else:
            skipped.append("dns")
    if opt.get("tz"):
        if have_fn("timedatectl"):
            if connected:
                steps.append(("time zone %s" % opt["tz"],
                              'mkdir -p "$HOME/.cache/dockpilot"; timedatectl show -p Timezone --value > "$HOME/.cache/dockpilot/tz.prev"; timedatectl set-timezone "%s"' % opt["tz"]))
            else:
                steps.append(("time zone back", '[ -s "$HOME/.cache/dockpilot/tz.prev" ] && timedatectl set-timezone "$(cat "$HOME/.cache/dockpilot/tz.prev")"'))
            applied.append("tz")
        else:
            skipped.append("tz")
    if opt.get("ntp"):
        skipped.append("ntp")                      # kept by the dock, shown; setting it needs root
    # proxy and vpn are desk options the seat chooses to use, by rule
    if rules.get("proxy") and opt.get("proxy"):
        if have_fn("gsettings"):
            host, _, port = opt["proxy"].rpartition(":")
            if not host:
                host, port = port, "3128"
            host = host.replace("http://", "").replace("https://", "")
            if connected:
                steps.append(("proxy %s" % opt["proxy"],
                              'gsettings set org.gnome.system.proxy.http host "%s"; gsettings set org.gnome.system.proxy.http port %s; '
                              'gsettings set org.gnome.system.proxy.https host "%s"; gsettings set org.gnome.system.proxy.https port %s; '
                              'gsettings set org.gnome.system.proxy mode manual' % (host, port, host, port)))
            else:
                steps.append(("proxy off", "gsettings set org.gnome.system.proxy mode none"))
            applied.append("proxy")
        else:
            skipped.append("proxy")
    if rules.get("vpn") and opt.get("vpn"):
        if have_fn("nmcli"):
            v = opt["vpn"]
            find = ('n=$(nmcli -t -f NAME,TYPE connection show | awk -F: \'$2=="vpn"||$2=="wireguard"{print $1}\' | grep -Fx "%s" | head -1); '
                    '[ -z "$n" ] && n=$(for x in $(nmcli -t -f NAME,TYPE connection show | awk -F: \'$2=="vpn"||$2=="wireguard"{print $1}\'); do '
                    'nmcli -g vpn.data connection show "$x" 2>/dev/null | grep -q "%s" && echo "$x" && break; done); [ -n "$n" ]' % (v, v))
            steps.append(("VPN %s" % v if connected else "VPN down",
                          find + (' && nmcli connection up "$n"' if connected else ' && nmcli connection down "$n" 2>/dev/null || true')))
            applied.append("vpn")
        else:
            skipped.append("vpn")
    # -- rules that are not about an option
    if rules.get("wifi_off"):
        if have_fn("nmcli"):
            steps.append(("Wi-Fi %s" % ("off" if connected else "on"), ["nmcli", "radio", "wifi", "off" if connected else "on"]))
    if rules.get("audio_dock") and connected and have_fn("pactl"):
        steps.append(("sound through the dock", 'for s in $(pactl list short sinks | cut -f2); do case "$s" in *usb*|*USB*) pactl set-default-sink "$s"; break;; esac; done'))
    if rules.get("lock_undock") and not connected and have_fn("loginctl"):
        steps.append(("lock the screen", ["loginctl", "lock-session"]))
    if rules.get("mute_undock") and not connected and have_fn("pactl"):
        steps.append(("mute", ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"]))
    if rules.get("touchpad_off") and have_fn("gsettings"):
        steps.append(("touchpad %s" % ("off" if connected else "on"),
                      ["gsettings", "set", "org.gnome.desktop.peripherals.touchpad", "send-events", "disabled" if connected else "enabled"]))
    return steps, applied, skipped


def take_snapshot():
    """How the laptop is set up right now, as the dock will keep it (under 400 bytes):
    screens, sound in and out, volume, keyboard layout, VPNs that are up."""
    snap = {}
    try:
        n = 0
        for c in glob.glob("/sys/class/drm/card*-*/status"):
            if read_sys(c) == "connected":
                n += 1
        if n:
            snap["screens"] = n
    except Exception:
        pass
    if have("pactl"):
        rc, out = run(["pactl", "get-default-sink"], timeout=5)
        if rc == 0 and out.strip():
            snap["sink"] = out.strip()[:60]
        rc, out = run(["pactl", "get-default-source"], timeout=5)
        if rc == 0 and out.strip():
            snap["source"] = out.strip()[:60]
        rc, out = run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"], timeout=5)
        m = re.search(r"(\d+)%", out) if rc == 0 else None
        if m:
            snap["volume"] = int(m.group(1))
    if have("gsettings"):
        rc, out = run(["gsettings", "get", "org.gnome.desktop.input-sources", "sources"], timeout=5)
        m = re.search(r"\('xkb',\s*'([^']+)'\)", out) if rc == 0 else None
        if m:
            snap["keyboard"] = m.group(1)[:20]
    if have("nmcli"):
        rc, out = run(["nmcli", "-t", "-f", "NAME,TYPE,STATE", "connection", "show", "--active"], timeout=5)
        vpns = [l.split(":")[0] for l in out.splitlines() if rc == 0 and l.count(":") >= 2 and l.split(":")[1] in ("vpn", "wireguard")]
        if vpns:
            snap["vpn"] = vpns[:2]
    text = json.dumps(snap, separators=(",", ":"))
    while len(text.encode("utf-8")) > 390 and snap:
        snap.pop(sorted(snap)[-1])
        text = json.dumps(snap, separators=(",", ":"))
    return text


def snapshot_words(text):
    """One line describing a saved configuration."""
    try:
        d = json.loads(text) if text else {}
    except ValueError:
        return "unreadable"
    if not d:
        return "nothing saved yet"
    bits = []
    if "screens" in d:
        bits.append("%d screen%s" % (d["screens"], "" if d["screens"] == 1 else "s"))
    if "sink" in d:
        bits.append("sound: " + ("dock" if "usb" in d["sink"].lower() else d["sink"].split(".")[-1][:18]))
    if "volume" in d:
        bits.append("%d %%" % d["volume"])
    if "keyboard" in d:
        bits.append("keyboard: " + d["keyboard"])
    if "vpn" in d:
        bits.append("VPN: " + ", ".join(d["vpn"]))
    return " · ".join(bits) or "saved"


def restore_plan(text, have_fn=None):
    """Steps that put a saved configuration back: sound, volume, keyboard, VPN.
    Screens are what the desk has; nothing to set."""
    have_fn = have_fn or have
    try:
        d = json.loads(text) if text else {}
    except ValueError:
        return []
    steps = []
    if have_fn("pactl"):
        if d.get("sink"):
            steps.append(("sound output", ["pactl", "set-default-sink", d["sink"]]))
        if d.get("source"):
            steps.append(("sound input", ["pactl", "set-default-source", d["source"]]))
        if "volume" in d:
            steps.append(("volume %d %%" % d["volume"], ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "%d%%" % int(d["volume"])]))
    if have_fn("gsettings") and d.get("keyboard"):
        steps.append(("keyboard %s" % d["keyboard"], ["gsettings", "set", "org.gnome.desktop.input-sources", "sources", "[('xkb', '%s')]" % d["keyboard"]]))
    if have_fn("nmcli"):
        for v in d.get("vpn", []):
            steps.append(("VPN %s" % v, ["nmcli", "connection", "up", v]))
    return steps


# --------------------------------------------------------------------------- #
#  Dock usage — the dock's own log turned into "when was this desk in use"
# --------------------------------------------------------------------------- #
def dock_usage(entries, days=14, now=None, docked_now=None, docked_s=0):
    """From the host's copy of a dock's log: per-day docked seconds for the last
    `days` days (oldest first), total sessions, and the last time it was used.
    Only timestamped (clock-synced) entries count; an unsynced boot is a gap.
    If the dock says it is docked right now for docked_s, that open session is
    counted up to now."""
    now = int(now if now is not None else time.time())
    day_start = now - (now % 86400)
    first_day = day_start - 86400 * (days - 1)
    per_day = [0] * days
    sessions = 0
    last_used = None
    start = None

    def add(a, b):
        a, b = max(a, first_day), min(b, now)
        while a < b:
            d = (a - first_day) // 86400
            if 0 <= d < days:
                end = min(b, first_day + 86400 * (d + 1))
                per_day[d] += end - a
                a = end
            else:
                break
    for e in sorted(entries, key=lambda e: e.get("i", 0)):
        t = e.get("t")
        ev = e.get("ev")
        if not t:
            if ev == "boot":
                start = None                      # power was lost; the session ended unseen
            continue
        if ev == "dock":
            start = t
            sessions += 1
            last_used = max(last_used or 0, t)
        elif ev in ("undock", "boot"):
            if start is not None and t > start:
                add(start, t)
                last_used = max(last_used or 0, t)
            start = None
    if docked_now and docked_s:
        s0 = now - int(docked_s)
        if start is None:                         # the dock event was before the clock was set
            start = s0
            sessions += 1
    if start is not None:
        add(start, now)                           # the session is still open
        last_used = now
    return {"per_day": per_day, "day0": first_day, "sessions": sessions, "last_used": last_used,
            "today_s": per_day[-1], "week_s": sum(per_day[-7:])}


def health_words(h):
    """One line from a HEALTH reply."""
    if not h:
        return "—"
    if h.get("ok"):
        return "OK — %s faults, log intact, %s writes left, clock %s" % (
            h.get("faults", {}).get("since_boot", 0), h.get("flash", {}).get("writes_left", "?"), h.get("clock", "?"))
    why = []
    f = h.get("faults") or {}
    if f.get("since_boot"):
        why.append("%d fault%s since boot (last: %s)" % (f["since_boot"], "" if f["since_boot"] == 1 else "s", f.get("last") or "?"))
    lg = h.get("log") or {}
    if lg.get("recreated"):
        why.append("log file rebuilt")
    if lg.get("skipped"):
        why.append("log lost a block to a power cut")
    fl = h.get("flash") or {}
    if fl.get("writes_left") == 0:
        why.append("write budget spent")
    if (h.get("temp") or {}).get("high_events") and h.get("temp", {}).get("now", 0) and h["temp"]["now"] >= 55:
        why.append("running hot")
    if h.get("reset") in ("watchdog", "brownout"):
        why.append("last reset was a %s" % h["reset"])
    return "Needs a look — " + "; ".join(why) if why else "Needs a look"


def _hms_short(sec):
    sec = int(sec or 0)
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm" % (sec // 60)
    return "%dh %02dm" % (sec // 3600, (sec % 3600) // 60)


# --------------------------------------------------------------------------- #
#  What the dock reports to IT — one document, stable field names
# --------------------------------------------------------------------------- #
def dock_report(snap, host=None):
    """The inventory record for one OpenDock, from its snapshot. Keys are fixed;
    a field a dock cannot provide is null, never missing."""
    ident, st = snap.get("identity") or {}, snap.get("status") or {}
    caps = snap.get("caps") or {}
    desk = snap.get("desk") or {}
    return {
        "schema": "opendock-report/2",
        "serial": ident.get("serial"), "chip_id": ident.get("uid"), "model": ident.get("model"),
        "firmware": ident.get("fw"), "protocol": ident.get("proto"),
        "tag": st.get("name") or None,
        "options": {k: v for k, v in (desk.get("opt") or {}).items() if v} if desk else None,
        "keys": desk.get("keys") if desk else None, "seats": desk.get("seats") if desk else None,
        "docked": st.get("docked"), "docked_s": st.get("docked_s"),
        "temp_c": st.get("temp_c"), "vbus_v": st.get("vbus_v"),
        "uptime_s": st.get("uptime_s"), "clock_synced": st.get("time_synced"),
        "log_entries": st.get("log_entries"), "log_volatile": st.get("log_volatile"),
        "health": snap.get("health"), "stats": snap.get("stats"),
        "capabilities": sorted(k for k, v in caps.items() if v is True) if caps else None,
        "seen_by": host or platform.node(), "seen_at": int(time.time()),
    }


def report_key(rep):
    """The part of a report that means 'something changed' — not the clocks."""
    return tuple(json.dumps(rep.get(k), sort_keys=True) for k in ("serial", "firmware", "tag", "options", "keys", "seats", "docked",
                                                                 "clock_synced", "log_volatile", "health"))

# --------------------------------------------------------------------------- #
#  GUI page
# --------------------------------------------------------------------------- #


REDACT_IPS = True          # set False to include IPs (rarely needed for dock diagnosis)


# --------------------------------------------------------------------------- #
def _snap_snap_run(cmd, timeout=10):
    """Run a command, return its output or an honest 'not available' note."""
    exe = cmd[0]
    if shutil.which(exe) is None:
        return "[%s not installed]" % exe
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        return out or "[no output]"
    except Exception as e:
        return "[error running %s: %s]" % (" ".join(cmd), e)


def _snap_read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return "[unavailable]"


def _redact(text):
    """Strip IPv4/IPv6 addresses and the hostname so snapshots are safe to paste publicly."""
    if not REDACT_IPS:
        return text
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<ip>", text)
    text = re.sub(r"\b(?:[0-9a-fA-F]{0,4}:){3,7}[0-9a-fA-F]{0,4}\b", "<ipv6>", text)
    host = platform.node()
    if host:
        text = text.replace(host, "<host>")
    return text


def _section(title, body):
    return "\n=== %s ===\n%s\n" % (title, body)


# --------------------------------------------------------------------------- #
def _usb_ancestry(iface):
    """Walk from the NIC up to its USB device dir; report chip IDs along the way."""
    if not iface:
        return "[no dock NIC]"
    real = os.path.realpath("/sys/class/net/%s/device" % iface)
    lines = ["resolved device path: %s" % real]
    p, hops = real, 0
    while p and p != "/" and hops < 8:
        vid, pid = _snap_read(os.path.join(p, "idVendor")), _snap_read(os.path.join(p, "idProduct"))
        if vid != "[unavailable]" and pid != "[unavailable]":
            lines.append("  %s  %s:%s  %s %s  (cfg %s of %s)" % (
                os.path.basename(p), vid, pid,
                _snap_read(os.path.join(p, "manufacturer")),
                _snap_read(os.path.join(p, "product")),
                _snap_read(os.path.join(p, "bConfigurationValue")),
                _snap_read(os.path.join(p, "bNumConfigurations")),
            ))
        p = os.path.dirname(p)
        hops += 1
    return "\n".join(lines)


def _net_stats(iface):
    if not iface:
        return "[no dock NIC]"
    base = "/sys/class/net/%s" % iface
    keys = ["rx_bytes", "tx_bytes", "rx_errors", "tx_errors",
            "rx_dropped", "tx_dropped", "rx_crc_errors"]
    out = ["operstate: %s" % _snap_read(base + "/operstate"),
           "carrier:   %s" % _snap_read(base + "/carrier"),
           "speed:     %s" % _snap_read(base + "/speed"),
           "mtu:       %s" % _snap_read(base + "/mtu"),
           "address:   %s" % _snap_read(base + "/address")]
    for k in keys:
        v = _snap_read("%s/statistics/%s" % (base, k))
        if v != "[unavailable]":
            out.append("%-14s %s" % (k + ":", v))
    return "\n".join(out)


def _mcu_state(snap=None):
    """IDENTIFY / CAPS / STATUS of a DockPilot-protocol dock (an MCU running the
    OpenDock firmware), from the app's last poll, plus the tail of its log."""
    if not snap or not snap.get("connected"):
        why = (snap or {}).get("error") or ""
        return "[no DockPilot-protocol dock detected]" + (("\n" + why) if why else "")
    ident, st = snap.get("identity") or {}, snap.get("status") or {}
    lines = ["port:      %s" % snap.get("port"),
             "identity:  %s" % json.dumps(ident, sort_keys=True),
             "caps:      %s" % json.dumps(snap.get("caps") or {}, sort_keys=True),
             "status:    %s" % json.dumps(st, sort_keys=True),
             "log (last 20 of %d held here):" % snap.get("log_count", 0)]
    if opendock is not None:
        lines += ["  " + opendock.describe(e) for e in (snap.get("log") or [])[-20:]]
    return "\n".join(lines)

# --------------------------------------------------------------------------- #
def build_snapshot(iface=None, log_lines=None, dockpilot_version="unknown", od_snapshot=None):
    """Assemble the full diagnostic report as a string."""
    parts = []
    parts.append("DockPilot System Snapshot")
    parts.append("generated: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    parts.append("DockPilot version: %s" % dockpilot_version)
    parts.append("(IPs and hostname are redacted; MAC addresses are kept — they identify the NIC chip)")

    # --- system ---
    parts.append(_section("SYSTEM", "\n".join([
        "python:  %s" % sys.version.split()[0],
        "kernel:  %s" % platform.release(),
        "arch:    %s" % platform.machine(),
        "distro:  %s" % _snap_read("/etc/os-release").split("\n")[0].replace('PRETTY_NAME=', '').strip('"'),
    ])))

    # --- tool availability (explains why sections may be empty) ---
    tools = ["ethtool", "lsusb", "uhubctl", "nmcli", "pactl", "ip", "fwupdmgr"]
    parts.append(_section("TOOLS AVAILABLE", "\n".join(
        "%-10s %s" % (t, "yes" if shutil.which(t) else "NOT INSTALLED") for t in tools)))

    # --- dock NIC ---
    parts.append(_section("DOCK NIC", "interface: %s" % (iface or "[none detected]")))
    if iface:
        c = nic_chip(iface)
        if c:
            info = ethtool_info(iface)
            fw = info.get("firmware-version", "") or kernel_log_firmware(iface)
            rt = realtek_identity(c["vid"], c["pid"], c["driver"], fw, c["parent_vid"])
            parts.append(_section("NIC CHIP", "\n".join([
                "usb id:    %s:%s  rev %s  product %r" % (c["vid"], c["pid"], c["bcd"], c["product"]),
                "vendor:    %s" % (c["vendor"] or "unknown"),
                "realtek:   %s" % (json.dumps(rt) if rt else "n/a"),
                "configs:   %s" % json.dumps(c["configs"]),
                "active:    #%s   vendor cfg #%s   cdc cfg #%s   driver %s" % (
                    c["config"], c["vendor_config"] or "?", c["cdc_config"] or "?", c["driver"]),
                "firmware:  %s" % (fw or "[none reported]"),
            ])))
        parts.append(_section("NIC DRIVER (ethtool -i)", _snap_snap_run(["ethtool", "-i", iface])))
        parts.append(_section("NIC LINK (ethtool)", _redact(_snap_snap_run(["ethtool", iface]))))
        parts.append(_section("NIC STATS (sysfs)", _net_stats(iface)))
        parts.append(_section("USB ANCESTRY / CHIP IDs", _usb_ancestry(iface)))

    # --- USB ---
    parts.append(_section("USB DEVICES (lsusb)", _snap_snap_run(["lsusb"])))
    parts.append(_section("USB TREE (lsusb -t)", _snap_snap_run(["lsusb", "-t"])))

    # --- network interfaces (redacted) ---
    parts.append(_section("INTERFACES (ip -br addr)", _redact(_snap_snap_run(["ip", "-br", "addr"]))))

    # --- MCU / protocol dock ---
    parts.append(_section("DOCKPILOT-PROTOCOL DOCK (MCU)", _mcu_state(od_snapshot)))

    # --- app command log ---
    if log_lines:
        parts.append(_section("DOCKPILOT COMMAND LOG (last 200)",
                              _redact("\n".join(log_lines[-200:]))))
    else:
        parts.append(_section("DOCKPILOT COMMAND LOG", "[none]"))

    parts.append("\n--- end of snapshot ---\n")
    return "\n".join(parts)


def write_snapshot(iface=None, log_lines=None, dockpilot_version="unknown", directory=None, od_snapshot=None):
    """Build the snapshot and write it to a timestamped file. Returns the path."""
    text = build_snapshot(iface, log_lines, dockpilot_version, od_snapshot)
    directory = directory or os.path.expanduser("~")
    path = os.path.join(directory, "dockpilot-snapshot-%s.txt" % time.strftime("%Y%m%d-%H%M%S"))
    with open(path, "w") as f:
        f.write(text)
    return path




# =========================================================================== #
#  ASIX chips — from ASIX's own Linux driver sources (GPL-2.0): ax_usb_nic 4.2.0 and asix 5.3.0.
#  Nothing here is copied code; these are the vendor/product/revision numbers the drivers match
#  on. The gigabit family shares one USB id (0b95:1790); the generation is in bcdDevice.
# =========================================================================== #
# ---- gigabit: 0b95:1790 (and 0b95:178a), generation by bcdDevice ----------------------
GIGABIT_IDS = {
    (0x0b95, 0x1790), (0x0b95, 0x178a),
    (0x0df6, 0x0072),   # Sitecom
    (0x17ef, 0x304b),   # Lenovo
    (0x0930, 0x0a13),   # Toshiba
    (0x04e8, 0xa100),   # Samsung
    (0x2001, 0x4a00),   # D-Link
    (0x0711, 0x0179),   # Magic Control
}
GIGABIT_BY_BCD = {
    0x0100: ("AX88179", "USB 3.0 gigabit, 2013 design; checksum offload quirks on some hosts"),
    0x0200: ("AX88179A", "USB 3.0 gigabit, flash firmware, 179A/179B share this id"),
    0x0300: ("AX88772D", "USB 2.0 100 Mb, 772D/772E share this id"),
    0x0400: ("AX88279", "USB 3.2 2.5 GbE"),
    0x0500: ("AX88279A", "USB 3.2 2.5 GbE, multi-queue"),
}
# 0b95:178a with bcdDevice 0x0100 is the AX88178A (gigabit, USB 2.0)
GIGABIT_178A = (0x0b95, 0x178a)

# ---- 100 Mb / older gigabit: one id per chip, plus rebrands ------------------------------
# (vid, pid) -> (chip, sold as)
FAST_IDS = {
    (0x0b95, 0x1780): ("AX88178", None),
    (0x077b, 0x2226): ("AX88178", "Linksys USB200M"),          # also listed for the 8817x
    (0x1737, 0x0039): ("AX88178", "Linksys (ABOCOM)"),
    (0x14ea, 0xab11): ("AX88178", "ABOCOM"),
    (0x050d, 0x5055): ("AX88178", "Belkin"),
    (0x0846, 0x1040): ("AX88172", "Netgear FA120"),
    (0x2001, 0x1a00): ("AX88172", "D-Link DUB-E100"),
    (0x2001, 0x3c05): ("AX88172", "D-Link DUB-E100B"),
    (0x07d1, 0x3c05): ("AX88172", "D-Link DUB-E100B"),
    (0x0b95, 0x1720): ("AX88172", "Intellinet / ST Lab"),
    (0x07b8, 0x420a): ("AX88172", "Hawking UF200 / TrendNet TU2-ET100"),
    (0x08dd, 0x90ff): ("AX88172", "Billionton USB2AR"),
    (0x0557, 0x2009): ("AX88172", "ATEN UC210T"),
    (0x0411, 0x003d): ("AX88172", "Buffalo LUA-U2-KTX"),
    (0x6189, 0x182d): ("AX88172", "Sitecom LN-029"),
    (0x07aa, 0x0017): ("AX88172", "corega FEther USB2-TX"),
    (0x1189, 0x0893): ("AX88172", "Surecom EP-1427X-2"),
    (0x1631, 0x6200): ("AX88172", "Goodway GWUSB2E"),
    (0x0b95, 0x7720): ("AX88772", None),
    (0x125e, 0x180d): ("AX88772", None),
    (0x0b95, 0x772a): ("AX88772A", None),
    (0x0db0, 0xa877): ("AX88772A", "MSI"),
    (0x0421, 0x772a): ("AX88772A", "Nokia"),
    (0x13b1, 0x0018): ("AX88772A", "Linksys USB200M v2"),
    (0x05ac, 0x1402): ("AX88772A", "Apple USB Ethernet Adapter"),
    (0x0b95, 0x7e2b): ("AX88772B", "Asus"),
    (0x17ef, 0x7203): ("AX88772B", "Lenovo"),
}
# 772B vs 772C: same ids, bcdDevice 1 = 772B, 2 = 772C
FAST_BY_BCD = {
    (0x0b95, 0x772b): {0: "AX88772B", 1: "AX88772B", 2: "AX88772C"},
    (0x2001, 0x1a02): {0: "AX88772B", 1: "AX88772B", 2: "AX88772C"},   # D-Link DUB-E100 later revisions
}
FAST_BY_BCD_SOLD_AS = {(0x2001, 0x1a02): "D-Link DUB-E100"}

ASIX_VID = 0x0b95
DRIVERS = ("ax88179_178a", "asix", "ax_usb_nic")   # kernel gigabit, kernel 100 Mb, ASIX's own


def asix_identity(vid, pid, bcd=None):
    """Name the chip. vid/pid/bcd are ints (bcd = bcdDevice, e.g. 0x0200). Returns
    {"name", "note", "sold_as"} or None if this is not an ASIX part we know."""
    key = (vid, pid)
    if key in GIGABIT_IDS:
        if key == GIGABIT_178A:
            return {"name": "AX88178A", "note": "USB 2.0 gigabit", "sold_as": None}
        gen = GIGABIT_BY_BCD.get(bcd & 0xFF00 if bcd is not None else None)
        name, note = gen if gen else ("AX88179 family", "generation not recognised from bcdDevice %s" % (hex(bcd) if bcd is not None else "?"))
        sold = None if vid == ASIX_VID else {0x0df6: "Sitecom", 0x17ef: "Lenovo", 0x0930: "Toshiba",
                                             0x04e8: "Samsung", 0x2001: "D-Link", 0x0711: "Magic Control"}.get(vid)
        return {"name": name, "note": note, "sold_as": sold}
    if key in FAST_BY_BCD:
        name = FAST_BY_BCD[key].get(bcd) if bcd is not None else None
        return {"name": name or "AX88772B/C", "note": "USB 2.0 100 Mb", "sold_as": FAST_BY_BCD_SOLD_AS.get(key)}
    if key in FAST_IDS:
        name, sold = FAST_IDS[key]
        return {"name": name, "note": "USB 2.0 100 Mb" if name.startswith("AX8877") or name == "AX88172" else "USB 2.0 gigabit", "sold_as": sold}
    return None


# =========================================================================== #
#  Chip naming across vendors
# =========================================================================== #
def chip_identity(c, fw_version=""):
    """One answer for any NIC we know: {"name", "sold_as", "dock", "mac_passthru", "note", "exact"}.
    Realtek from its driver tables (firmware name gives the exact part); ASIX from its driver
    tables (bcdDevice gives the generation). None for anything else."""
    if not c:
        return None
    rt = realtek_identity(c["vid"], c["pid"], c.get("driver", ""), fw_version, c.get("parent_vid", ""))
    if rt:
        return {"name": rt["name"], "sold_as": rt["sold_as"], "dock": rt["dock"],
                "mac_passthru": rt["mac_passthru"], "note": "", "exact": bool(rt["variant"]),
                "vendor": "Realtek"}
    try:
        bcd = int(c["bcd"], 16) if re.fullmatch(r"[0-9a-fA-F]{4}", c.get("bcd") or "") else None
        ax = asix_identity(int(c["vid"], 16), int(c["pid"], 16), bcd)
    except ValueError:
        ax = None
    if ax:
        return {"name": ax["name"], "sold_as": ax["sold_as"] or "", "dock": "", "mac_passthru": False,
                "note": ax["note"], "exact": True, "vendor": "ASIX"}
    return None


# =========================================================================== #
#  Engine — the state and the actions, independent of any window
# =========================================================================== #
class Engine:
    """Everything the old window held: the dock interface, its history, the log, the rules,
    the privileged helper session, the OpenDock link, the native-mode flow. A host (window or
    service) calls tick() once a second and reads what it wants; events come back through
    the listener callbacks, on the host's thread if it passes a `marshal` function."""

    SPEEDS = ["auto", "1000/full", "100/full", "10/full"]
    OFFLOADS = [("rx-checksumming", "RX checksum",
                 "Incoming packets carry a checksum to prove they weren't corrupted. On: the dock chip verifies it, saving your CPU the maths. Off: your computer's CPU checks every packet."),
                ("tx-checksumming", "TX checksum",
                 "Outgoing packets need a checksum computed. On: the dock chip fills it in. Off: your CPU computes it for each packet."),
                ("scatter-gather", "Scatter-gather",
                 "A packet's data can sit in several memory chunks. On: the dock chip reads them directly. Off: your computer assembles the packet into one buffer first."),
                ("tcp-segmentation-offload", "TCP segmentation (TSO)",
                 "Big sends are sliced into wire-sized packets. On: the dock chip does the slicing — a big CPU saving on uploads. Off: your CPU slices every packet (captures then show the real packets)."),
                ("generic-segmentation-offload", "Generic segmentation (GSO)",
                 "The same slicing done in software at the last moment. On: the CPU delays the work. Off: it slices immediately."),
                ("generic-receive-offload", "Generic receive (GRO)",
                 "On downloads, many small packets are merged into fewer big ones before your CPU sees them. Off: each packet is processed separately — better for packet capture or routing.")]

    def __init__(self, role="ui", marshal=None, automation=True, opendock_link=True):
        self.role = role
        self.marshal = marshal or (lambda fn: fn())
        self.listeners = {"log": [], "transition": [], "opendock": [], "status": [], "native": []}
        self.devices = load_devices()
        self.iface = find_dock_iface()
        self.top_hub = dock_top_hub(dock_nic_usb_name(self.iface)) if self.iface else ""
        self.orig_mac = original_mac(self.iface, net_mac(self.iface)) if self.iface else ""
        _c0 = nic_chip(self.iface) if self.iface else None
        self.standard_config = (_c0["config"] if _c0 and _c0["driver"] in CDC_DRIVERS and _c0["config"]
                                else (_c0["cdc_config"] if _c0 and _c0.get("cdc_config") else "2"))
        self.rx_hist = collections.deque(maxlen=120)
        self.tx_hist = collections.deque(maxlen=120)
        self._last_bytes = net_bytes(self.iface) if self.iface else (0, 0)
        self.prev_link = net_carrier(self.iface) if self.iface else False
        self.log_lines = []
        self.macro_store = MacroStore()
        self.privshell = PrivShell()
        self.automation = automation
        self.rules = self._load_rules()
        self.seat_phrase = seat_load()
        self.seat_id = seat_id(self.seat_phrase)
        self.desk_applied = load_json(DESK_STATE_FILE, None)   # the desk/seat in force, to undo at undock
        self.last_report_key = None
        self._rules_mtime = self._file_mtime(AUTOMATION_FILE)
        self.reach = {}
        self._timers = []                       # (due, fn) — fired from tick()
        self.native_status = ("", "muted")      # text, level in {muted, busy, ok, bad}
        self.native_last_state = None
        self.odlink = None
        self.od_snap = {}
        if opendock_link:
            self._start_opendock()

    # ---- listeners ----------------------------------------------------------
    def on(self, kind, fn):
        self.listeners[kind].append(fn)

    def _emit(self, kind, *args):
        for fn in list(self.listeners[kind]):
            try:
                self.marshal(lambda fn=fn, args=args: fn(*args))
            except Exception:
                pass

    def log(self, msg):
        line = str(msg).rstrip()
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-500:]
        self._emit("log", line)

    # ---- timers (coarse, driven by tick) ----------------------------------------
    def later(self, seconds, fn):
        self._timers.append((time.monotonic() + seconds, fn))

    def _run_timers(self):
        now = time.monotonic()
        due = [t for t in self._timers if t[0] <= now]
        self._timers = [t for t in self._timers if t[0] > now]
        for _, fn in due:
            try:
                fn()
            except Exception as e:
                self.log("# internal: %s" % e)

    # ---- rules ----------------------------------------------------------------
    @staticmethod
    def _file_mtime(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0

    def _load_rules(self):
        a = load_json(AUTOMATION_FILE, {})
        return {"wifi": bool(a.get("wifi", False)), "mac": bool(a.get("mac", False)),
                "audio": bool(a.get("audio", False))}

    RULE_NAMES = {"wifi": "Wi-Fi off when docked", "mac": "MAC pass-through", "audio": "audio to dock"}

    def set_rule(self, key, on):
        on = bool(on)
        if self.rules.get(key) == on:
            return
        self.rules[key] = on
        self.log("# automation: %s %s" % (self.RULE_NAMES.get(key, key), "on" if on else "off"))
        save_json(AUTOMATION_FILE, dict(self.rules))
        self._rules_mtime = self._file_mtime(AUTOMATION_FILE)

    def reload_rules_if_changed(self):
        changed = False
        m = self._file_mtime(AUTOMATION_FILE)
        if m != self._rules_mtime:
            self._rules_mtime = m
            self.rules = self._load_rules()
            self.macro_store.load()
            changed = True
        return changed

    # ---- the desk and the seat (what the dock hands out) --------------------------------
    def od_serial(self):
        return ((self.od_snap or {}).get("identity") or {}).get("serial") or ""

    def od_desk(self):
        return (self.od_snap or {}).get("desk")

    def od_seat(self):
        return (self.od_snap or {}).get("seat")

    def apply_desk(self, connected, desk=None, seat=None):
        """Do the laptop-side work for the desk the dock handed out and this
        laptop's seat. Only when this engine owns automation. Undo at undock uses
        what was applied at dock, kept in a file so a restart still undoes it."""
        if connected:
            desk = desk if desk is not None else self.od_desk()
            seat = seat if seat is not None else self.od_seat()
            if desk is None:
                return False
            self.desk_applied = {"desk": desk, "seat": seat, "serial": self.od_serial()}
            save_json(DESK_STATE_FILE, self.desk_applied)
        else:
            if not self.desk_applied:
                return False
            desk, seat = self.desk_applied.get("desk"), self.desk_applied.get("seat")
            self.desk_applied = None
            try:
                os.remove(DESK_STATE_FILE)
            except OSError:
                pass
        steps, applied, skipped = desk_plan(desk, seat, connected, self.iface)
        n_opt = len([k for k, v in ((desk or {}).get("opt") or {}).items() if v])
        self.log("# desk %s: %s — %d of %d options %s%s" % (
            (desk or {}).get("tag") or self.od_serial(), "dock" if connected else "undock",
            len(applied), n_opt, "applied" if connected else "undone",
            (" (not here: %s)" % ", ".join(skipped)) if skipped else ""))
        env = self.od_env()
        env["DOCK_IFACE"] = self.iface or ""
        shell = []
        for note, step in steps:
            if isinstance(step, list):
                self.do(step, note=note)
            else:
                self.log("# " + note)
                shell.append(step)
        if shell:
            run_commands(shell, log=self.log, env_extra=env)
        cmds = ((seat or {}).get("cmd") or {}).get("dock" if connected else "undock") or []
        if cmds:
            self.log("# your own commands (%s)" % ("dock" if connected else "undock"))
            run_commands(cmds, log=self.log, env_extra=env)
        return True

    def apply_rule_now(self, rid, on):
        """A rule switched while docked takes effect at once: on = do its docked
        part, off = undo it. Rules that act at undock (lock, mute) wait for undock."""
        st = (self.od_snap or {}).get("status") or {}
        if not self.automation or not st.get("docked") or rid not in RULE_TOOLS:
            return False
        opt = ((self.od_desk() or {}).get("opt") or {})
        desk = {"opt": {rid: opt.get(rid, "")}} if rid in ("proxy", "vpn") else {"opt": {}}
        steps, _a, _s = desk_plan(desk, {"rules": {rid: 1}}, bool(on), self.iface)
        if self.desk_applied:                       # so undock undoes what is in force now
            seat = dict(self.desk_applied.get("seat") or {})
            rules = dict(seat.get("rules") or {})
            if on:
                rules[rid] = 1
            else:
                rules.pop(rid, None)
            seat["rules"] = rules
            self.desk_applied["seat"] = seat
            save_json(DESK_STATE_FILE, self.desk_applied)
        if not steps:
            return False
        self.log("# rule %s %s — applied now" % (rid, "on" if on else "off"))
        shell = []
        for note, step in steps:
            if isinstance(step, list):
                self.do(step, note=note)
            else:
                self.log("# " + note)
                shell.append(step)
        if shell:
            env = self.od_env()
            env["DOCK_IFACE"] = self.iface or ""
            run_commands(shell, log=self.log, env_extra=env)
        return True

    def restore_snapshot(self, text=None):
        """Put the seat's saved configuration back."""
        text = text if text is not None else ((self.od_seat() or {}).get("snap") or "")
        steps = restore_plan(text)
        if not steps:
            self.log("# nothing to restore")
            return 0
        self.log("# configuration restored: %s" % snapshot_words(text))
        for note, step in steps:
            self.do(step, note=note)
        return len(steps)

    def save_snapshot_to_dock(self, done=None):
        """Take a snapshot of this laptop and write it to the seat."""
        text = take_snapshot()
        self.od_desk_action("set_snap", text, done=done, note="configuration saved: " + snapshot_words(text))

    def use_seat_phrase(self, phrase):
        """Another laptop's words: this laptop becomes that seat at every dock."""
        ph = seat_phrase_clean(phrase)
        if not ph:
            return False
        if ph == self.seat_phrase:
            return True
        seat_save(ph)
        self.seat_phrase, self.seat_id = ph, seat_id(ph)
        if self.odlink is not None:
            self.odlink.seat_id = self.seat_id
            self.odlink.close()                   # reconnects within seconds, as the new seat
        self.log("# seat key replaced; this laptop is now the same seat as the one that gave it")
        return True

    def od_report(self):
        return dock_report(self.od_snap or {})

    def od_usage(self, days=14):
        snap = self.od_snap or {}
        st = snap.get("status") or {}
        entries = snap.get("log") or []
        if self.odlink is not None and self.odlink.store:
            entries = self.odlink.store.entries
        return dock_usage(entries, days=days, docked_now=st.get("docked"), docked_s=st.get("docked_s", 0))

    # ---- running things -----------------------------------------------------------
    def do(self, args, note=None, after=None):
        """Run a command (privileged ones carry the pkexec prefix from priv()) on a worker
        thread; log the result; call `after` on the host thread."""
        if note:
            self.log("# " + note)
        self.log(("$ dockpilot-helper " + " ".join(args[2:])) if args and args[0] == "pkexec" else ("$ " + " ".join(args)))

        def worker():
            if args and args[0] == "pkexec":
                rc, out = self.privshell.run(args)
            else:
                rc, out = run(args)

            def done():
                self.log(out or "(exit %d)" % rc)
                if after:
                    after()
            self.marshal(done)
        threading.Thread(target=worker, daemon=True).start()

    def privshell_async(self, cmd):
        threading.Thread(target=lambda: self.privshell.run(cmd), daemon=True).start()

    def nmcli_connect(self, ifc):
        threading.Thread(target=lambda: run(["nmcli", "device", "connect", ifc]), daemon=True).start()

    # ---- the 1 Hz tick ---------------------------------------------------------------
    def tick(self):
        ifc = self.iface
        if ifc and not os.path.exists("/sys/class/net/%s" % ifc):
            ifc = None                                   # the dock went away
        if not ifc:
            new = find_dock_iface()
            if new:
                self._adopt_iface(new)
                ifc = new
        if ifc:
            rx, tx = net_bytes(ifc)
            lrx, ltx = self._last_bytes
            self._last_bytes = (rx, tx)
            self.rx_hist.append(max(0, rx - lrx) * 8 / 1e6)
            self.tx_hist.append(max(0, tx - ltx) * 8 / 1e6)
            up = net_carrier(ifc)
            if up != self.prev_link:
                self.prev_link = up
                self.on_transition(up)
        elif self.prev_link:
            self.prev_link = False
            self.on_transition(False)
        if self.odlink is not None:
            self.od_snap = self.odlink.snapshot()
        self._run_timers()
        self._emit("status")

    def _adopt_iface(self, new):
        self.iface = new
        self.top_hub = dock_top_hub(dock_nic_usb_name(new)) or ""
        self.orig_mac = original_mac(new, net_mac(new))
        self._last_bytes = net_bytes(new)
        self.log("# dock NIC appeared: %s" % new)

    def on_transition(self, connected):
        self.log("# dock %s" % ("connected" if connected else "disconnected"))
        at_opendock = bool((self.od_snap or {}).get("connected"))
        if self.automation and at_opendock and (self.rules["wifi"] or self.rules["audio"]):
            self.log("# generic Wi-Fi/audio rules stand down — the OpenDock's rules decide here")
        if self.automation:
            if self.rules["wifi"] and have("nmcli") and not at_opendock:
                self.do(["nmcli", "radio", "wifi", "off" if connected else "on"])
            if self.rules["mac"] and self.iface:
                if connected:
                    src = primary_host_iface(self.iface)
                    if src:
                        self.set_mac(net_mac(src))
                elif os.path.exists("/sys/class/net/%s" % self.iface):
                    self.mac_restore()
                else:
                    self.log("# MAC restore skipped — interface no longer present")
            if self.rules["audio"] and connected and have("pactl") and not at_opendock:
                for s in list_sinks():
                    if "usb" in s.lower():
                        self.do(["pactl", "set-default-sink", s])
                        break
            c = nic_chip(self.iface) if self.iface else None
            model = (c.get("product") or "") if c else ""
            n = run_macros(self.macro_store, "connect" if connected else "disconnect", log=self.log, dock_model=model)
            if n:
                self.log("# %d macro(s) fired" % n)
        self._emit("transition", connected)

    # ---- reachability (slow; run on a worker) ----------------------------------------
    def refresh_reach(self, done=None):
        def worker():
            gw = default_gw()
            d = {"gw": ping_ms(gw), "net": ping_ms("1.1.1.1"), "dns": dns_ms(), "pub": public_ip(), "lan": lan_count()}
            self.reach = d
            if done:
                self.marshal(lambda: done(d))
        threading.Thread(target=worker, daemon=True).start()

    # ---- texts -----------------------------------------------------------------------
    def identity_lines(self):
        """[(label, value)] for the Dashboard identity card."""
        ifc = self.iface
        if not ifc:
            return [("Network chip", "none found — this hub has no network chip")]
        rows = [("Interface", ifc), ("Current MAC", "%s  %s" % (net_mac(ifc), self.mac_origin()))]
        if self.orig_mac and self.orig_mac != net_mac(ifc):
            rows.append(("Original MAC", self.orig_mac))
        rows += [("IPv4", get_ipv4(ifc) or "—"),
                 ("Link", "%s · carrier %s" % (net_speed(ifc), "up" if net_carrier(ifc) else "down"))]
        mode = nic_mode(ifc)
        if mode:
            rows.append(("NIC mode", mode))
        return rows

    def mac_origin(self):
        ifc = self.iface
        if not ifc:
            return ""
        cur = net_mac(ifc)
        host = {net_mac(n) for n in list_ifaces() if n != ifc and not iface_is_usb(n)}
        if cur in host:
            if self.orig_mac and self.orig_mac == cur:
                return "(same as this laptop's own NIC — firmware pass-through)"
            return "(mirrored from this laptop's NIC by DockPilot)"
        if self.orig_mac and cur != self.orig_mac:
            return "(changed by DockPilot)"
        return "(the dock's own)"

    def chip_lines(self):
        """[(label, value)] for the chip card. Everything we can tell without touching it."""
        c = nic_chip(self.iface) if self.iface else None
        if not c:
            return [("Chip", "No USB-Ethernet NIC found — this hub has no network chip.")]
        info = ethtool_info(self.iface)
        fw = info.get("firmware-version", "") or kernel_log_firmware(self.iface)
        ident = chip_identity(c, fw)
        bcd = c["bcd"]
        rev = "%s.%s" % (bcd[:2], bcd[2:]) if re.fullmatch(r"[0-9a-fA-F]{4}", bcd or "") else (bcd or "—")
        if ident:
            chip = ident["name"]
            if ident["sold_as"]:
                chip += "  — sold under %s's USB id" % ident["sold_as"]
            elif ident["vendor"] == "Realtek" and not ident["exact"]:
                chip += "  (from the product id; the exact part shows once the driver names its firmware)"
        else:
            chip = c["product"] or "unknown"
        rows = [("Chip", chip),
                ("USB id", "%s:%s · hardware rev %s · %s" % (c["vid"], c["pid"], rev, c["product"] or "—"))]
        if ident and ident.get("note"):
            rows.append(("Family", ident["note"]))
        if ident and ident["dock"]:
            rows.append(("Dock", "%s  (named in Realtek's own driver)" % ident["dock"]))
        if c["configs"]:
            rows.append(("USB configs", "   ".join("#%s %s%s" % (x["value"], x["kind"], " ◀" if x["value"] == c["config"] else "") for x in c["configs"])))
        drv = c["driver"] or "none"
        if info.get("version"):
            v = info["version"]
            drv += " " + (v if v[:1] in "vV" else "v" + v)
        rows.append(("Driver", drv + ("   firmware %s" % fw if fw else "")))
        if ident and ident["mac_passthru"]:
            rows.append(("MAC", "can carry the laptop's own MAC through from BIOS — the Current MAC line says whether it does"))
        return rows

    def health(self):
        ifc = self.iface
        if not ifc:
            return None
        e = net_stat(ifc, "rx_errors") + net_stat(ifc, "tx_errors")
        d = net_stat(ifc, "rx_dropped") + net_stat(ifc, "tx_dropped")
        rx, tx = net_bytes(ifc)
        return {"errors": e, "drops": d, "carrier": net_carrier(ifc), "rx": rx, "tx": tx, "speed": net_speed(ifc)}

    # ---- MAC -----------------------------------------------------------------------
    def set_mac(self, mac, after=None):
        self.do(priv(["mac", self.iface, mac]), note="set MAC -> %s" % mac, after=after)

    def mac_passthrough(self, after=None):
        src = primary_host_iface(self.iface) if self.iface else None
        if src:
            self.set_mac(net_mac(src), after=after)
        else:
            self.log("# no laptop NIC to mirror")

    def mac_restore(self, after=None):
        if self.orig_mac:
            self.set_mac(self.orig_mac, after=after)
        else:
            self.log("Original MAC unknown.")

    # ---- native mode --------------------------------------------------------------------
    def set_native_status(self, text, level="muted"):
        self.native_status = (text, level)
        self._emit("native", text, level)

    def dock_key(self):
        c = nic_chip(self.iface) if self.iface else None
        return "%s:%s" % (c["vid"], c["pid"]) if c else None

    def remember_dock(self, native_ok):
        key = self.dock_key()
        if not key:
            return
        mem = load_json(DOCKMEM_FILE, {})
        mem[key] = {"native_ok": native_ok, "chip": (nic_chip(self.iface) or {}).get("product", "")}
        save_json(DOCKMEM_FILE, mem)

    def native_known_bad(self):
        key = self.dock_key()
        if not key:
            return False
        mem = load_json(DOCKMEM_FILE, {})
        return key in mem and mem[key].get("native_ok") is False

    def native_header(self):
        state, c = native_state(self.iface)
        if not c or not c["vendor"]:
            return state, c, []
        rows = [("Chip", "%s %s" % (c["vendor"], c["product"] or (c["vid"] + ":" + c["pid"]))),
                ("Driver", c["driver"] or "none"),
                ("USB config", "%s of %s (%s) · native mode is config #%s · link %s" % (
                    c["config"], c["nconfigs"], "NATIVE" if state == "native" else "standard mode",
                    c.get("vendor_config") or "?", "up" if net_carrier(self.iface) else "down"))]
        return state, c, rows

    def native_unlock(self):
        c = nic_chip(self.iface)
        if not c or not c["usb"]:
            self.set_native_status("Couldn't locate the dock NIC's USB device — give it a second, or replug the dock.", "bad")
            return False
        dev, drv, cfg = c["usb"], c["driver"], c["config"]
        native = c["vendor_config"] or "1"
        self.do(priv(["usb-config", dev, drv, "%s.0" % cfg, str(native)]),
                note="unbind %s on %s, switch to native config (%s)" % (drv, dev, native))
        self.set_native_status("Unlocking… switching to native mode.", "busy")
        self.later(2, lambda: self._native_unlock_check(6))
        return True

    def _native_unlock_check(self, tries):
        state, c = native_state(self.iface)
        if state == "native":
            self.log("# native mode active — checking whether the wired link comes up…")
            self.set_native_status("Native mode on — waiting for the wired link to come up…", "busy")
            self._link_attempt(5)
            return
        if tries > 0:
            self.set_native_status("Switching to native mode… (%d/6)" % (7 - tries), "busy")
            self.later(1.2, lambda: self._native_unlock_check(tries - 1))
            return
        self.set_native_status("The switch didn't take — try Unlock again, or replug the dock.", "bad")

    def _link_attempt(self, rounds):
        ifc = self.iface
        if net_carrier(ifc):
            self._native_verdict_ok()
            return
        if rounds <= 0:
            self._native_verdict_no_link()
            return
        self.set_native_status("Native mode on — waiting for the wired link… (%d/5)" % (6 - rounds), "busy")
        self.privshell_async(["link-up", ifc])
        if have("nmcli"):
            self.later(0.3, lambda: self.nmcli_connect(ifc))
        self.later(2.5, lambda: self._link_attempt(rounds - 1))

    def _native_verdict_ok(self):
        state, c = native_state(self.iface)
        self.remember_dock(True)
        self.log("# native mode + wired link up (driver %s)" % (c["driver"] if c else "native"))
        self.set_native_status("Native mode active — link up. Wake-on-LAN and tuning are available.", "ok")

    def _native_verdict_no_link(self):
        self.remember_dock(False)
        self.set_native_status("Native driver runs, but the wired link won't come up on this dock — putting you back on standard mode…", "busy")
        c = nic_chip(self.iface)
        if c and c["usb"]:
            self.do(priv(["usb-config", c["usb"], c["driver"], "1.0", str(self.standard_config)]),
                    note="native link won't come up — relocking to standard")
        self.later(3, self._native_no_link_done)

    def _native_no_link_done(self):
        if have("nmcli"):
            self.nmcli_connect(self.iface)
        self.set_native_status("Native mode isn't usable on this dock — you're safely on standard mode, link working. "
                               "Wake-on-LAN and tuning need native, so they're unavailable here.", "bad")

    WHY_NATIVE_FAILED = ("This dock runs the native driver fine, but its wired Ethernet link never comes up in native "
                         "mode — the native driver can't initialise this particular board's Ethernet PHY. It's a known "
                         "board/chip-revision quirk (common on AX88179B-based docks). Standard mode works perfectly, so "
                         "DockPilot keeps you there; only the native-only extras (Wake-on-LAN, link tuning) are unavailable.")

    def native_relock(self):
        c = nic_chip(self.iface)
        if not c or not c["usb"]:
            self.set_native_status("Couldn't locate the dock NIC's USB device — give it a second, or replug the dock.", "bad")
            return False
        self.do(priv(["usb-config", c["usb"], c["driver"], "1.0", str(self.standard_config)]),
                note="unbind %s on %s, switch to standard config (%s)" % (c["driver"], c["usb"], self.standard_config))
        self.set_native_status("Returning to standard mode…", "busy")
        self.later(3.8, lambda: self.set_native_status("Back on standard (plug-and-play) mode.", "ok"))
        return True

    # ---- native-only features ------------------------------------------------------------
    def wol_state(self):
        return wol_state(self.iface, self.privshell)

    def wol_set(self, on, after=None):
        self.do(priv(["wol", self.iface, "g" if on else "d"]), note="Wake-on-LAN %s" % ("armed" if on else "disarmed"), after=after)

    def wol_send(self, mac):
        ok, msg = send_magic_packet(mac)
        self.log("# WoL -> %s: %s" % (mac, msg))
        return ok, msg

    def eeprom_read(self, done):
        def worker():
            supported, all_ff, out = eeprom_dump(self.iface, 64, self.privshell)
            if not supported:
                verdict = "This driver gives no EEPROM access — nothing to read here."
            elif all_ff:
                verdict = "Reads all 0xFF — no rewritable EEPROM (the chip uses one-time eFuse)."
            else:
                verdict = "Real data present — this chip has a populated EEPROM."
            head = "\n".join(out.splitlines()[:6])
            self.marshal(lambda: done(verdict, head))
        threading.Thread(target=worker, daemon=True).start()

    def eeprom_backup(self, done):
        def worker():
            ok, res = eeprom_backup(self.iface, self.privshell)
            self.log("# EEPROM backup: %s" % res)
            self.marshal(lambda: done(ok, res))
        threading.Thread(target=worker, daemon=True).start()

    def speed_set(self, sel, after=None):
        if sel == "auto":
            args = ["speed", self.iface, "auto"]
        else:
            spd, dup = sel.split("/")
            args = ["speed", self.iface, spd, dup, "on" if spd == "1000" else "off"]
        self.do(priv(args), after=after)

    def renegotiate(self, after=None):
        self.do(priv(["renegotiate", self.iface]), after=after)

    def flow_set(self, on, after=None):
        self.do(priv(["pause", self.iface, "on" if on else "off"]), note="flow control %s" % ("on" if on else "off"), after=after)

    def flow_supported(self):
        rc, aout = run(["ethtool", "-a", self.iface])
        return rc == 0 and "not supported" not in aout.lower() and bool(aout.strip())

    def offload_set(self, key, on, after=None):
        self.do(priv(["offload", self.iface, key, "on" if on else "off"]), note="offload %s %s" % (key, "on" if on else "off"), after=after)

    # ---- USB ------------------------------------------------------------------------------
    def usb_authorize(self, af, enable, after=None):
        self.do(priv(["usb-authorize", os.path.basename(os.path.dirname(af)), "1" if enable else "0"]),
                note=("enable" if enable else "disable") + " USB device", after=after)

    def usb_leaves(self):
        anchors = [self.top_hub] if self.top_hub else external_top_hubs()
        leaves = []
        for top in anchors:
            leaves += dock_leaf_devices(top)
        return anchors, leaves

    def scan_ports(self, done):
        def worker():
            rc, out = self.privshell.run(["usb-ports"])
            hubs, cur = [], None
            for line in out.splitlines():
                m = re.search(r"status for hub\s+(\S+)\s+\[([^\]]+)\].*?(\d+)\s*ports", line)
                if m:
                    cur = {"loc": m.group(1), "desc": m.group(2), "ports": []}
                    hubs.append(cur)
                    continue
                p = re.match(r"\s*Port\s+(\d+):", line)
                if p and cur is not None:
                    cur["ports"].append(int(p.group(1)))
            self.log("$ uhubctl\n" + out)
            self.marshal(lambda: done(hubs, out))
        threading.Thread(target=worker, daemon=True).start()

    def port_power(self, loc, port, action, after=None):
        self.do(priv(["usb-port", loc, str(port), action]), note="port %s on hub %s: %s" % (port, loc, action), after=after)

    def topology_rows(self):
        """[(depth, label, kind)] for each hub subtree, plus a note."""
        anchors, note = ([self.top_hub], "") if self.top_hub else (external_top_hubs(), "")
        if not self.top_hub and anchors:
            note = "No Ethernet chip on this hub — showing the USB hub(s) detected; this may include other hubs."
        out = []
        for top in anchors:
            rows = []
            flatten_tree(build_dock_tree(top), rows)
            out.append((top, rows))
        return out, note

    # ---- NetworkManager -----------------------------------------------------------------------
    def nm_duplicate_profiles(self):
        """Wired profiles not attached to any device, as [(uuid, name)]. Deleting goes by UUID:
        NetworkManager happily keeps twenty profiles with the same name, one of them active,
        and `nmcli connection delete NAME` would take the active one down with the rest."""
        if not have("nmcli"):
            return []
        rc, out = run(["nmcli", "-t", "-f", "UUID,NAME,TYPE,DEVICE", "connection", "show"])
        unused = []
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) < 4 or "ethernet" not in parts[2]:
                continue
            if not parts[3] or parts[3] == "--":
                unused.append((parts[0], parts[1]))
        return unused

    def nm_cleanup(self, profiles):
        for uuid, name in profiles:
            self.do(["nmcli", "connection", "delete", "uuid", uuid], note="remove unused wired profile %r" % name)

    # ---- audio ----------------------------------------------------------------------------
    def set_volume(self, sink, pct):
        run(["pactl", "set-sink-volume", sink or default_sink(), "%d%%" % int(pct)])

    def toggle_mute(self, sink):
        self.do(["pactl", "set-sink-mute", sink or default_sink(), "toggle"])

    # ---- snapshot ---------------------------------------------------------------------------
    def save_snapshot(self):
        return write_snapshot(iface=self.iface, log_lines=self.log_lines, dockpilot_version=APP_VERSION,
                              od_snapshot=self.odlink.snapshot() if self.odlink else None)

    # ---- OpenDock ---------------------------------------------------------------------------
    def _start_opendock(self):
        if opendock is None or opendock.serial is None:
            return
        self.odlink = opendock.DockLink(
            CONFIG_DIR, seat_id=self.seat_id,
            on_connect=lambda l: self.marshal(self._od_connected),
            on_disconnect=lambda l: self.marshal(self._od_disconnected),
            on_event=lambda t: self.marshal(lambda: self._od_event(t)),
            on_log=lambda es: self.marshal(lambda: self._od_new_log(es)))

        def worker():
            while True:
                try:
                    self.odlink.poll()
                except Exception as e:
                    self.odlink.error = str(e)
                self.od_snap = self.odlink.snapshot()
                time.sleep(2.0)
        threading.Thread(target=worker, daemon=True).start()

    def od_env(self):
        snap = self.od_snap
        ident, st = snap.get("identity") or {}, snap.get("status") or {}
        return {"DOCK_UID": ident.get("uid", ""), "DOCK_SERIAL": ident.get("serial", ""),
                "DOCK_MODEL": ident.get("model", ""), "DOCK_FW": ident.get("fw", ""),
                "DOCK_PORT": snap.get("port") or "", "DOCK_NAME": st.get("name", ""), "DOCK_TAG": st.get("name", "")}

    def _od_connected(self):
        self.od_snap = self.odlink.snapshot()
        i = self.od_snap.get("identity") or {}
        self.log("# OpenDock %s (%s, fw %s) on %s" % (i.get("serial"), i.get("model"), i.get("fw"), self.od_snap.get("port")))
        if self.automation:
            st = self.od_snap.get("status") or {}
            if st.get("docked", True):               # a 0.5 dock that is not docked will say so with EVENT dock
                self.apply_desk(True)
                if ((self.od_seat() or {}).get("snap")):
                    self.restore_snapshot()
            n = run_macros(self.macro_store, "opendock_connect", log=self.log, dock_model=i.get("model", ""), **self.od_env())
            if n:
                self.log("# %d macro(s) fired" % n)
        self._emit("opendock", "connected")

    def _od_disconnected(self):
        env = self.od_env()
        self.od_snap = self.odlink.snapshot()
        self.log("# OpenDock gone (%s)" % (self.od_snap.get("error") or "unplugged"))
        if self.automation:
            self.apply_desk(False)
            run_macros(self.macro_store, "opendock_disconnect", log=self.log, dock_model=env.get("DOCK_MODEL", ""), **env)
        self._emit("opendock", "disconnected")

    def _od_event(self, text):
        self.log("# OpenDock event: %s" % text)
        if text in ("dock", "undock") and self.automation:
            if text == "undock" and (self.od_seat() or {}).get("autosave"):
                self.save_snapshot_to_dock()         # the laptop is still on the link for a moment
            self.apply_desk(text == "dock")          # firmware 0.5 pushes these the moment VBUS changes
            if text == "dock" and (self.od_seat() or {}).get("snap"):
                self.restore_snapshot()
        self._emit("opendock", "event:" + text)

    def _od_new_log(self, entries):
        self.od_snap = self.odlink.snapshot()
        for e in entries[-5:]:
            self.log("# OpenDock log: " + opendock.describe(e))
        if len(entries) > 5:
            self.log("# OpenDock log: … %d entries in all" % len(entries))
        self._emit("opendock", "log")

    def od_set(self, key, value, done=None):
        """Validate and write one setting. done(message) on the host thread."""
        if not self.odlink or not self.odlink.connected:
            return
        why = opendock.validate_setting(value)
        if why:
            self.log("# OpenDock %s not saved: %s" % (key, why))
            if done:
                done("not saved: " + why)
            return
        self.log("# OpenDock: set %s" % key)

        def worker():
            try:
                self.odlink.set(key, value)
                msg = "%s saved" % key
            except opendock.DockBusy as e:
                msg = "%s not saved — dock busy: %s" % (key, e)
            except opendock.DockError as e:
                msg = "%s not saved: %s" % (key, e)
            self.od_snap = self.odlink.snapshot()
            self.log("# OpenDock " + msg)
            if done:
                self.marshal(lambda: done(msg))
        threading.Thread(target=worker, daemon=True).start()

    def od_desk_action(self, name, *args, done=None, note=None):
        """One desk/seat write on the dock, on a worker. done(message) on the host thread."""
        if not self.odlink or not self.odlink.connected:
            if done:
                done("no dock connected")
            return
        self.log("# OpenDock: %s" % (note or name.replace("_", " ")))

        def worker():
            try:
                self.odlink.desk_action(name, *args)
                msg = note or "saved"
                if name == "set_rule" and len(args) == 2:
                    self.marshal(lambda: self.apply_rule_now(args[0], args[1]))
            except opendock.DockBusy as e:
                msg = "not saved — dock busy: %s" % e
            except opendock.DockError as e:
                msg = "not saved: %s" % e
            self.od_snap = self.odlink.snapshot()
            self.log("# OpenDock " + msg)
            if done:
                self.marshal(lambda: done(msg))
        threading.Thread(target=worker, daemon=True).start()

    def od_desk_export(self):
        """The desk as a script file in the home folder. Returns the path."""
        if not self.odlink or not self.odlink.connected or not self.odlink.dock.has_desk():
            return None
        lines = self.odlink.dock.export()
        serial_no = self.od_serial() or "dock"
        path = os.path.join(os.path.expanduser("~"), "opendock-desk-%s.txt" % serial_no)
        with open(path, "w") as f:
            f.write("# OpenDock desk %s — exported %s. Send back with: opendock import <file>\n" % (serial_no, time.strftime("%Y-%m-%d %H:%M")))
            f.write("\n".join(lines) + "\n")
        self.log("# OpenDock desk exported: %s" % path)
        return path

    def od_clear(self, done=None):
        if not self.odlink or not self.odlink.connected:
            return

        def worker():
            try:
                self.odlink.clear_log()
                msg = "log cleared"
            except opendock.DockError as e:
                msg = "log not cleared: %s" % e
            self.od_snap = self.odlink.snapshot()
            self.log("# OpenDock " + msg)
            if done:
                self.marshal(lambda: done(msg))
        threading.Thread(target=worker, daemon=True).start()

    def od_export(self):
        snap = self.od_snap
        if not snap.get("connected") or not self.odlink or not self.odlink.store:
            return None
        serial_no = (snap.get("identity") or {}).get("serial", "dock")
        path = os.path.join(os.path.expanduser("~"), "opendock-log-%s-%s.json" % (serial_no, time.strftime("%Y%m%d-%H%M%S")))
        with open(path, "w") as f:
            json.dump({"identity": snap.get("identity"), "status": snap.get("status"),
                       "entries": self.odlink.store.entries}, f, indent=1)
        self.log("# OpenDock log exported: %s" % path)
        return path

    # ---- a status picture for the service file / the indicator -----------------------------------------
    def status_dict(self):
        ifc = self.iface
        h = self.health()
        snap = self.od_snap or {}
        ident, st = snap.get("identity") or {}, snap.get("status") or {}
        return {
            "time": int(time.time()),
            "nic": {"iface": ifc, "carrier": bool(h and h["carrier"]), "speed": h["speed"] if h else "",
                    "errors": h["errors"] if h else 0, "drops": h["drops"] if h else 0,
                    "rx_mbps": round(self.rx_hist[-1], 2) if self.rx_hist else 0.0,
                    "tx_mbps": round(self.tx_hist[-1], 2) if self.tx_hist else 0.0},
            "opendock": {"connected": bool(snap.get("connected")), "port": snap.get("port"),
                         "serial": ident.get("serial"), "model": ident.get("model"), "fw": ident.get("fw"),
                         "name": st.get("name"), "docked": st.get("docked"), "temp_c": st.get("temp_c"),
                         "vbus_v": st.get("vbus_v"), "log_entries": st.get("log_entries"),
                         "tile": bool(((snap.get("seat") or {}).get("rules") or {}).get("tile")) and bool(st.get("docked")),
                         "error": snap.get("error")},
            "rules": dict(self.rules),
        }


# =========================================================================== #
#  The background service (dockpilotd)
# =========================================================================== #
RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
SOCKET_PATH = os.path.join(RUNTIME, "dockpilot.sock")
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"), "dockpilot")
STATUS_PATH = os.path.join(CACHE, "status.json")


def notify(title, body, icon="dockpilot", urgency="normal"):
    if not have("notify-send"):
        return
    try:
        subprocess.Popen(["notify-send", "-a", "DockPilot", "-i", icon, "-u", urgency, title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class Service:
    def __init__(self):
        self.lock = threading.Lock()
        self.engine = Engine(role="service", marshal=self._marshal)
        self._queue = []
        self.engine.on("transition", self._on_transition)
        self.engine.on("opendock", self._on_opendock)
        self._hot = False
        self.engine.log("# dockpilotd %s started" % APP_VERSION)

    # work handed from threads runs on the main loop, in order
    def _marshal(self, fn):
        with self.lock:
            self._queue.append(fn)

    def _drain(self):
        with self.lock:
            q, self._queue = self._queue, []
        for fn in q:
            try:
                fn()
            except Exception as e:
                self.engine.log("# internal: %s" % e)

    # ---- events -> notifications -------------------------------------------------
    def _on_transition(self, connected):
        ifc = self.engine.iface or "dock"
        notify("Dock connected" if connected else "Dock disconnected",
               "%s — wired link %s" % (ifc, "up" if connected else "down"), urgency="low")

    def write_report(self, force=False):
        """One JSON file per dock in the cache, rewritten only when something that
        matters changed — never on a timer."""
        e = self.engine
        snap = e.od_snap or {}
        if not snap.get("connected"):
            return
        rep = dock_report(snap)
        key = report_key(rep)
        if key == e.last_report_key and not force:
            return
        e.last_report_key = key
        try:
            os.makedirs(CACHE, exist_ok=True)
            path = os.path.join(CACHE, "opendock-%s.json" % (rep.get("serial") or "unknown"))
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(rep, f, indent=1)
            os.replace(tmp, path)
        except Exception:
            pass

    def _on_opendock(self, what):
        snap = self.engine.od_snap
        i, st = snap.get("identity") or {}, snap.get("status") or {}
        if what in ("connected", "log", "disconnected"):
            self.write_report(force=(what == "connected"))
        if what == "connected":
            notify("OpenDock %s" % i.get("serial", ""),
                   "%s, firmware %s" % (st.get("name") or i.get("model", ""), i.get("fw", "")), urgency="low")
        elif what == "disconnected":
            notify("OpenDock gone", snap.get("error") or "unplugged", urgency="low")
        elif what.startswith("event:temp"):
            try:
                t = int(what.split()[-1])
            except ValueError:
                t = None
            if t is not None and t >= 60 and not self._hot:
                self._hot = True
                notify("Dock running hot", "%d °C — clear the vents" % t, urgency="critical")
            elif t is not None and t < 60:
                self._hot = False

    # ---- status file -------------------------------------------------------------------
    def write_status(self):
        try:
            os.makedirs(CACHE, exist_ok=True)
            tmp = STATUS_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.engine.status_dict(), f)
            os.replace(tmp, STATUS_PATH)
        except Exception:
            pass

    # ---- socket API ----------------------------------------------------------------------
    def handle(self, req):
        e = self.engine
        cmd = req.get("cmd")
        if cmd == "status":
            return {"ok": True, "opendock": e.od_snap, "rules": e.rules, "log": e.log_lines[-50:],
                    "nic": e.status_dict()["nic"], "version": APP_VERSION}
        if cmd == "log":
            return {"ok": True, "log": e.log_lines[-200:]}
        if cmd in ("od_set", "od_clear") and not (e.odlink and e.odlink.connected):
            return {"ok": False, "message": "no OpenDock connected", "opendock": e.od_snap}
        if cmd == "od_set":
            done = threading.Event()
            box = {}

            def fin(msg):
                box["msg"] = msg
                done.set()
            e.od_set(str(req.get("key", "")), str(req.get("value", "")), done=fin)
            done.wait(20)
            return {"ok": True, "message": box.get("msg", "no reply"), "opendock": e.od_snap}
        if cmd == "od_clear":
            done = threading.Event()
            box = {}

            def fin(msg):
                box["msg"] = msg
                done.set()
            e.od_clear(done=fin)
            done.wait(20)
            return {"ok": True, "message": box.get("msg", "no reply"), "opendock": e.od_snap}
        if cmd == "od_export":
            path = e.od_export()
            return {"ok": bool(path), "path": path}
        if cmd == "report":
            return {"ok": True, "report": e.od_report()}
        if cmd == "usage":
            return {"ok": True, "usage": e.od_usage(int(req.get("days", 14)))}
        if cmd == "desk":
            done = threading.Event()
            box = {}

            def fin(msg):
                box["msg"] = msg
                done.set()
            args = req.get("args") or []
            e.od_desk_action(str(req.get("action", "")), *args, done=fin)
            done.wait(25)
            return {"ok": True, "message": box.get("msg", "no reply"), "opendock": e.od_snap}
        if cmd == "desk_export":
            path = e.od_desk_export()
            return {"ok": bool(path), "path": path}
        if cmd == "save_setup":
            done = threading.Event()
            box = {}

            def fin(msg):
                box["msg"] = msg
                done.set()
            e.save_snapshot_to_dock(done=fin)
            done.wait(25)
            return {"ok": True, "message": box.get("msg", "no reply"), "opendock": e.od_snap}
        if cmd == "restore_setup":
            return {"ok": True, "steps": e.restore_snapshot()}
        if cmd == "seat":
            if req.get("phrase"):
                return {"ok": e.use_seat_phrase(str(req.get("phrase")))}
            return {"ok": True, "phrase": e.seat_phrase, "id": e.seat_id}
        if cmd == "apply_desk":
            return {"ok": e.apply_desk(bool(req.get("connected", True)))}
        if cmd == "reload":
            e.reload_rules_if_changed()
            return {"ok": True}
        return {"ok": False, "error": "unknown command"}

    def serve_socket(self):
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o600)
        srv.listen(8)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        try:
            f = conn.makefile("rwb", buffering=0)
            line = f.readline()
            while line:
                try:
                    req = json.loads(line.decode("utf-8"))
                    rep = self.handle(req if isinstance(req, dict) else {})
                except Exception as ex:
                    rep = {"ok": False, "error": str(ex)}
                f.write((json.dumps(rep) + "\n").encode("utf-8"))
                line = f.readline()
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---- main loop --------------------------------------------------------------------------
    def run(self):
        threading.Thread(target=self.serve_socket, daemon=True).start()
        n = 0
        while True:
            t0 = time.monotonic()
            self._drain()
            try:
                self.engine.tick()
            except Exception as ex:
                self.engine.log("# tick error: %s" % ex)
            self._drain()
            n += 1
            if n % 5 == 0 and self.engine.reload_rules_if_changed():
                self.engine.log("# settings reloaded")
            self.write_status()
            time.sleep(max(0.2, 1.0 - (time.monotonic() - t0)))


def already_running():
    """True if another dockpilotd answers on the socket."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(SOCKET_PATH)
        s.sendall(b'{"cmd":"status"}\n')
        s.recv(16)
        s.close()
        return True
    except Exception:
        return False


def ask(cmd, timeout=25.0, **kw):
    """Client side: one request to the running service, or None if there is none."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCKET_PATH)
        req = dict(kw)
        req["cmd"] = cmd
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        return json.loads(buf.decode("utf-8")) if buf else None
    except Exception:
        return None


EXTENSION_UUID = "dockpilot@opendock.io"


def ensure_extension_enabled():
    """The top-bar indicator is a GNOME Shell extension the .deb installs. Enabling it from the
    installer (root, outside the session) does not always take, so the service — which runs
    inside the user's session — makes sure of it on every start. Returns True if it did something.
    Harmless where there is no GNOME: nothing to enable, nothing to say."""
    if not os.path.isdir("/usr/share/gnome-shell/extensions/" + EXTENSION_UUID):
        return False
    done = False
    try:
        import gi                                             # noqa: F401
        from gi.repository import Gio
        src = Gio.SettingsSchemaSource.get_default()
        if src is not None and src.lookup("org.gnome.shell", True) is not None:
            st = Gio.Settings.new("org.gnome.shell")
            cur = list(st.get_strv("enabled-extensions"))
            if EXTENSION_UUID not in cur:
                st.set_strv("enabled-extensions", cur + [EXTENSION_UUID])
                st.sync()
                done = True
    except Exception:
        pass
    try:                                                      # loads it into the running Shell, no re-login
        subprocess.run(["gnome-extensions", "enable", EXTENSION_UUID], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        pass
    return done


def service_main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["--version"]:
        print("dockpilotd " + APP_VERSION)
        return 0
    if already_running():
        print("dockpilotd is already running (%s)" % SOCKET_PATH, file=sys.stderr)
        return 0
    ensure_extension_enabled()
    Service().run()
    return 0


if __name__ == "__main__":
    sys.exit(service_main())
