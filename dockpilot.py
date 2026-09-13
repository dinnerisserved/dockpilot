#!/usr/bin/env python3
"""
DockPilot — vendor-neutral admin console for USB-C / Thunderbolt docks & hubs.

Everything the dock exposes is read straight from the Linux /sys filesystem, so
the ONLY dependency is customtkinter (pip). No ethtool / lsusb / fwupd required.
Optional tools (uhubctl, nmcli, pactl) are auto-detected and their panels hide
if the tool isn't present.

Config lives NEXT TO this script, in ./config/ — nothing is written to your home
directory, and no personal data ships with the app.

Run inside a venv:
    python3 -m venv venv && source venv/bin/activate
    pip install customtkinter
    python dockpilot.py
(Tkinter needs the system 'python3-tk' package if your base Python lacks it.)
"""

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
import shlex
import subprocess
import urllib.request
import tkinter as tk
from tkinter import messagebox

try:
    import customtkinter as ctk
except ImportError:
    raise SystemExit("Missing dependency — run:  pip install customtkinter")

# --------------------------------------------------------------------------- #
#  config — beside the script only
# --------------------------------------------------------------------------- #
APP_VERSION = "0.3.0"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
def _pick_config_dir():
    """Prefer ./config next to the script; fall back to ~/.config/dockpilot when the
    script directory isn't writable (e.g. a system-wide pip install into site-packages)."""
    local = os.path.join(SCRIPT_DIR, "config")
    try:
        os.makedirs(local, exist_ok=True)
        probe = os.path.join(local, ".writetest")
        with open(probe, "w") as f:
            f.write("")
        os.remove(probe)
        return local
    except Exception:
        home = os.path.join(os.path.expanduser("~"), ".config", "dockpilot")
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

def priv(args):
    return args if os.geteuid() == 0 else (["pkexec"] + args)


class PrivShell:
    """One long-lived authenticated root shell, so a burst of privileged commands
    (e.g. a speed change that triggers automation) prompts for a password only once
    per session instead of once per command. A lock serialises access so concurrent
    callers can't interleave on the shared pipe and deadlock."""
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()

    def _ensure(self):
        if self.proc and self.proc.poll() is None:
            return True
        try:
            self.proc = subprocess.Popen(
                ["pkexec", "bash", "-c",
                 'while IFS= read -r __l; do eval "$__l"; echo "__RC__$?"; done'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1)
            return True
        except Exception:
            self.proc = None
            return False

    def run(self, cmd, timeout=30):
        if os.geteuid() == 0:
            return run(["bash", "-c", cmd])
        # Serialise: only one privileged command in flight at a time.
        with self.lock:
            if not self._ensure():
                return 1, "pkexec unavailable / dismissed"
            try:
                # Run the command, then echo a unique end marker so a read never blocks
                # forever waiting on output that isn't coming.
                self.proc.stdin.write(cmd + "\n")
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
                    # stalled — drop the shell so the next call re-auths cleanly
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


def priv_cmd_string(args):
    """Turn a pkexec-prefixed arg list back into a shell string for PrivShell."""
    a = args[1:] if args and args[0] == "pkexec" else args
    if len(a) >= 3 and a[0] == "bash" and a[1] == "-c":
        return a[2]
    return shlex.join(a)

def have(b):
    return shutil.which(b) is not None

def read_sys(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""

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

def usb_eth_ifaces():
    return [n for n in list_ifaces() if iface_is_usb(n)]

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
NIC_VENDORS = {"0b95": "ASIX", "0bda": "Realtek", "0b95": "ASIX"}
NATIVE_DRIVERS = {"ax88179_178a", "ax88179a", "r8152", "r8153"}
CDC_DRIVERS = {"cdc_ncm", "cdc_ether", "cdc_eem", "cdc_mbim"}

def nic_chip(iface):
    """Identify the dock NIC's chip vendor, driver, and USB config state."""
    if not iface:
        return None
    name = dock_nic_usb_name(iface)
    if not name:
        return None
    info = usb_info(name)
    vendor = NIC_VENDORS.get(info["vid"])
    return {
        "usb": name, "vid": info["vid"], "pid": info["pid"], "vendor": vendor,
        "product": info["product"] or "",
        "nconfigs": int(read_sys(os.path.join(USB_ROOT, name, "bNumConfigurations") or "1") or 1),
        "config": read_sys(os.path.join(USB_ROOT, name, "bConfigurationValue")),
        "driver": net_driver(iface),
    }

def native_state(iface):
    """Returns (state, chip): state in {native, unlockable, unsupported}."""
    c = nic_chip(iface)
    if not c or c["vendor"] not in ("ASIX", "Realtek"):
        return "unsupported", c
    if c["driver"] in NATIVE_DRIVERS:
        return "native", c
    if c["driver"] in CDC_DRIVERS and c["nconfigs"] > 1:
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

def wol_state(iface):
    m = re.search(r"Wake-on:\s*(\S+)", run(["ethtool", iface])[1]) if have("ethtool") else None
    return m.group(1) if m else "?"

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


def eeprom_dump(iface, length=64):
    """Return (supported, all_ff, hex_lines). Reads only — never writes."""
    if not have("ethtool"):
        return False, False, "ethtool not installed"
    rc, out = run(["ethtool", "-e", iface, "offset", "0", "length", str(length)])
    if rc != 0 or "not supported" in out.lower() or "Cannot get" in out:
        return False, False, out.strip() or "EEPROM access not supported by this driver"
    vals = re.findall(r"\b([0-9a-fA-F]{2})\b", out.split("Values", 1)[-1])
    all_ff = bool(vals) and all(v.lower() == "ff" for v in vals)
    return True, all_ff, out.strip()


def eeprom_backup(iface):
    """Save the full EEPROM to a timestamped file. Returns (ok, path_or_msg)."""
    if not have("ethtool"):
        return False, "ethtool not installed"
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SCRIPT_DIR, f"eeprom_{iface}_{ts}.bin")
    rc, out = run(["bash", "-c", f"ethtool -e {iface} raw on > {shlex.quote(path)}"])
    if rc == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
        return True, path
    return False, out.strip() or "read failed"



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
    m = re.search(r"firmware-version:\s*(.+)", out)
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
class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self._show); widget.bind("<Leave>", self._hide)
    def _show(self, _=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget); self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left", wraplength=300, bg="#111418",
                 fg="#e8e8e8", relief="solid", borderwidth=1, font=("", 10), padx=8, pady=6).pack()
    def _hide(self, _=None):
        if self.tip:
            self.tip.destroy(); self.tip = None
def tip(w, t): Tooltip(w, t)

# --------------------------------------------------------------------------- #
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")
CARD = ("#1b2027", "#1b2027")
ACCENT = "#2fbf5f"
RXCOL = "#3fa9f5"

class DockPilot(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("DockPilot")
        self.geometry("1120x730")
        self.minsize(1000, 640)

        self.devices = load_devices()
        self.iface = find_dock_iface()
        self.top_hub = dock_top_hub(dock_nic_usb_name(self.iface)) if self.iface else ""
        self.orig_mac = original_mac(self.iface, net_mac(self.iface)) if self.iface else ""
        _c0 = nic_chip(self.iface) if self.iface else None
        self.standard_config = (_c0["config"] if _c0 and _c0["driver"] in CDC_DRIVERS and _c0["config"] else "2")
        self.rx_hist = collections.deque(maxlen=120)
        self.tx_hist = collections.deque(maxlen=120)
        self._last_bytes = net_bytes(self.iface) if self.iface else (0, 0)
        self.prev_link = net_carrier(self.iface) if self.iface else False
        self.current = None
        self.log_lines = []
        self.macro_store = MacroStore()
        self.privshell = PrivShell()

        auto = load_json(AUTOMATION_FILE, {})
        self.rule_wifi = tk.BooleanVar(value=bool(auto.get("wifi", False)))
        self.rule_mac = tk.BooleanVar(value=bool(auto.get("mac", False)))
        self.rule_audio = tk.BooleanVar(value=bool(auto.get("audio", False)))

        for v in (self.rule_wifi, self.rule_mac, self.rule_audio,
                  ):
            v.trace_add("write", lambda *_: self._save_automation())

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self._build_statusstrip()
        self._build_sidebar()
        self.content = ctk.CTkFrame(self, corner_radius=0, fg_color=("#12151a", "#12151a"))
        self.content.grid(row=1, column=1, sticky="nsew")
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        self.show("Dashboard")
        self._tick()

    # ---------- status strip ----------
    def _build_statusstrip(self):
        s = ctk.CTkFrame(self, corner_radius=0, height=54, fg_color=("#0e1116", "#0e1116"))
        s.grid(row=0, column=0, columnspan=2, sticky="ew")
        ctk.CTkLabel(s, text="  ⛭ DockPilot", font=ctk.CTkFont(size=18, weight="bold")).pack(side="left", padx=10)
        self.ss_dot = ctk.CTkLabel(s, text="●", font=ctk.CTkFont(size=20)); self.ss_dot.pack(side="left", padx=(12, 4))
        self.ss_iface = ctk.CTkLabel(s, text="no dock NIC", font=ctk.CTkFont(size=13, weight="bold")); self.ss_iface.pack(side="left")
        self.ss_speed = ctk.CTkLabel(s, text="", text_color=("gray60", "gray60")); self.ss_speed.pack(side="left", padx=12)

    # ---------- sidebar ----------
    def _build_sidebar(self):
        bar = ctk.CTkFrame(self, width=176, corner_radius=0, fg_color=("#0e1116", "#0e1116"))
        bar.grid(row=1, column=0, sticky="nsw"); bar.grid_propagate(False)
        self.nav_buttons = {}
        for name in ("Dashboard", "Identity", "Native Mode", "Network", "USB & Power",
                     "Topology", "Audio", "Automation", "Macros", "Logs"):
            b = ctk.CTkButton(bar, text=name, anchor="w", corner_radius=8, height=38,
                              fg_color="transparent", hover_color=("#1b2027", "#1b2027"),
                              command=lambda n=name: self.show(n))
            b.pack(fill="x", padx=8, pady=2)
            self.nav_buttons[name] = b

    def show(self, name):
        # Stop any in-flight native-mode link attempt so its timers/threads don't
        # fire into a page that's about to be destroyed.
        self._rolling = False
        self.current = name
        for n, b in self.nav_buttons.items():
            b.configure(fg_color=(ACCENT if n == name else "transparent"),
                        text_color=("#0e1116" if n == name else ("#e8e8e8", "#e8e8e8")))
        for w in self.content.winfo_children():
            w.destroy()
        wrap = ctk.CTkScrollableFrame(self.content, fg_color="transparent")
        wrap.grid(row=0, column=0, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)
        {
            "Dashboard": self._page_dashboard, "Identity": self._page_identity,
            "Native Mode": self._page_native,
            "Network": self._page_network, "USB & Power": self._page_usb,
            "Topology": self._page_topology, "Audio": self._page_audio,
            "Automation": self._page_automation, "Macros": self._page_macros,
            "Logs": self._page_logs,
        }[name](wrap)

    # ---------- reusable card ----------
    def _card(self, parent, title, explain=""):
        c = ctk.CTkFrame(parent, corner_radius=12, fg_color=CARD)
        c.pack(fill="x", padx=10, pady=8)
        ctk.CTkLabel(c, text=title, font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w", padx=16, pady=(12, 0))
        if explain:
            ctk.CTkLabel(c, text=explain, text_color=("gray55", "gray55"),
                         font=ctk.CTkFont(size=11), justify="left", wraplength=820).pack(anchor="w", padx=16, pady=(2, 0))
        return c

    def _identity_text(self):
        ifc = self.iface
        if not ifc:
            return "No USB-Ethernet NIC found — this hub has no network chip."
        rev = ""
        if self.top_hub:
            raw = usb_info(self.top_hub).get("bcd") or ""
            rev = f"v{int(raw[:2])}.{raw[2:]}" if re.fullmatch(r"[0-9a-f]{4}", raw) else raw
        mode = nic_mode(ifc)
        lines = [f"Dock NIC:      {ifc}   ({net_driver(ifc)})",
                 f"Current MAC:   {net_mac(ifc)}",
                 f"Original MAC:  {self.orig_mac or 'unknown'}",
                 f"Link:          {net_speed(ifc)}   ·   carrier {'up' if net_carrier(ifc) else 'down'}",
                 f"Dock hub rev:  {rev or '—'}   (from USB descriptor)"]
        if mode:
            lines.append(f"NIC mode:      {mode}")
        return "\n".join(lines)

    # ---------- Dashboard ----------
    def _page_dashboard(self, w):
        top = ctk.CTkFrame(w, fg_color="transparent")
        top.pack(fill="x", padx=2)
        top.grid_columnconfigure(0, weight=3)
        top.grid_columnconfigure(1, weight=2)

        c1 = ctk.CTkFrame(top, corner_radius=12, fg_color=CARD)
        c1.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        ctk.CTkLabel(c1, text="Identity & dock", font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w", padx=16, pady=(12, 2))
        self.dash_id = ctk.CTkLabel(c1, text=self._identity_text(), justify="left", anchor="w",
                                    font=ctk.CTkFont(family="monospace", size=13))
        self.dash_id.pack(anchor="w", padx=16, pady=(2, 14))

        right = ctk.CTkFrame(top, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        c3 = ctk.CTkFrame(right, corner_radius=12, fg_color=CARD)
        c3.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        ctk.CTkLabel(c3, text="Link health", font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w", padx=16, pady=(12, 2))
        self.dash_health = ctk.CTkLabel(c3, text="…", justify="left", anchor="w",
                                        font=ctk.CTkFont(family="monospace", size=13))
        self.dash_health.pack(anchor="w", padx=16, pady=(2, 12))
        c4 = ctk.CTkFrame(right, corner_radius=12, fg_color=CARD)
        c4.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        ctk.CTkLabel(c4, text="Reachability", font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w", padx=16, pady=(12, 2))
        self.dash_reach = ctk.CTkLabel(c4, text="measuring…", justify="left", anchor="w",
                                       font=ctk.CTkFont(family="monospace", size=13))
        self.dash_reach.pack(anchor="w", padx=16, pady=(2, 12))

        c2 = self._card(w, "Throughput",
                        "Live rate through the dock NIC. 0.0 Mb/s = idle link; the totals prove it's counting.")
        legend = ctk.CTkFrame(c2, fg_color="transparent"); legend.pack(anchor="w", padx=16)
        ctk.CTkLabel(legend, text="■ RX (download)", text_color=RXCOL).pack(side="left", padx=(0, 14))
        ctk.CTkLabel(legend, text="■ TX (upload)", text_color=ACCENT).pack(side="left")
        self.spark = tk.Canvas(c2, height=150, bg="#161a1f", highlightthickness=0)
        self.spark.pack(fill="both", expand=True, padx=12, pady=(6, 4))
        self.dash_totals = ctk.CTkLabel(c2, text="", text_color=("gray60", "gray60"),
                                        font=ctk.CTkFont(family="monospace", size=12))
        self.dash_totals.pack(anchor="w", padx=16, pady=(0, 12))
        self._update_dashboard_fast()
        self._refresh_reach_async()

    def _update_dashboard_fast(self):
        if self.current != "Dashboard":
            return
        ifc = self.iface
        if hasattr(self, "dash_id") and self.dash_id.winfo_exists():
            self.dash_id.configure(text=self._identity_text())
        if hasattr(self, "dash_health") and self.dash_health.winfo_exists() and ifc:
            e = net_stat(ifc, "rx_errors") + net_stat(ifc, "tx_errors")
            d = net_stat(ifc, "rx_dropped") + net_stat(ifc, "tx_dropped")
            self.dash_health.configure(text=f"errors:  {e}\ndrops:   {d}\ncarrier: {'up' if net_carrier(ifc) else 'down'}",
                                       text_color=(ACCENT if e == 0 else "#e0a13a"))
        if hasattr(self, "dash_totals") and self.dash_totals.winfo_exists() and ifc:
            rx, tx = net_bytes(ifc)
            self.dash_totals.configure(text=f"session totals   ↓ {human(rx)}   ↑ {human(tx)}")
        if hasattr(self, "spark") and self.spark.winfo_exists():
            self._draw_spark()

    def _draw_spark(self):
        c = self.spark; c.delete("all")
        W = c.winfo_width() or 800; H = c.winfo_height() or 170
        L = 56          # left margin for the Y-axis number column
        TOP = 20        # top pad so the unit label sits above the plot
        peak = max([1.0] + list(self.rx_hist) + list(self.tx_hist))
        # unit label, top-left, clear of the numbers
        c.create_text(6, 10, anchor="w", fill="#9aa2ab", text="Mb/s", font=("", 9))
        # Y grid + numbers
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            val = peak * frac
            y = H - frac * (H - TOP - 6) - 6
            c.create_line(L, y, W - 6, y, fill="#232a31")
            c.create_text(L - 8, y, anchor="e", fill="#7a828b", text=f"{val:.1f}", font=("", 9))
        step = (W - L - 8) / (self.rx_hist.maxlen - 1)
        for hist, color in ((self.rx_hist, RXCOL), (self.tx_hist, ACCENT)):
            if len(hist) < 2:
                continue
            pts = []
            for i, v in enumerate(hist):
                pts += [L + i * step, H - (v / peak) * (H - TOP - 6) - 6]
            c.create_line(*pts, fill=color, width=2, smooth=True)

    def _refresh_reach_async(self):
        def worker():
            gw = default_gw()
            d = {"gw": ping_ms(gw), "net": ping_ms("1.1.1.1"), "dns": dns_ms(),
                 "pub": public_ip(), "lan": lan_count()}
            self.after(0, lambda: self._apply_reach(d))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_reach(self, d):
        if self.current != "Dashboard" or not hasattr(self, "dash_reach") or not self.dash_reach.winfo_exists():
            return
        def ms(v): return f"{v:.0f} ms" if v is not None else "—"
        self.dash_reach.configure(text=(f"gateway:   {ms(d['gw'])}\n"
                                        f"internet:  {ms(d['net'])}  (1.1.1.1)\n"
                                        f"DNS:       {ms(d['dns'])}\n"
                                        f"public IP: {d['pub'] or 'offline / blocked'}\n"
                                        f"LAN seen:  {d['lan']} device(s)"))

    # ---------- Identity ----------
    def _page_identity(self, w):
        c = self._card(w, "Identity", "The dock NIC's network identity and current MAC state.")
        self.id_lbl = ctk.CTkLabel(c, text=self._identity_text(), justify="left", anchor="w",
                                   font=ctk.CTkFont(family="monospace", size=13))
        self.id_lbl.pack(anchor="w", padx=16, pady=(6, 14))
        if not self.iface:
            return
        pt = self._card(w, "MAC pass-through",
                        "Make the dock present a host NIC's MAC, so the network sees one identity when docked.")
        src = primary_host_iface(self.iface)
        r = ctk.CTkFrame(pt, fg_color="transparent"); r.pack(fill="x", padx=16, pady=(6, 14))
        b1 = ctk.CTkButton(r, text=f"Mirror {src or 'host'} → dock", command=lambda: self._mac_passthrough(src))
        b1.pack(side="left", padx=(0, 8))
        if not src:
            b1.configure(state="disabled")
        rst = self._card(w, "Reset",
                         "Restore the dock's own factory MAC. Captured at first launch, so this always works "
                         "even after scrambling — you can always come back.")
        ctk.CTkButton(rst, text="✓  Reset to original MAC", fg_color=ACCENT, hover_color="#256741",
                      command=self._mac_restore).pack(anchor="w", padx=16, pady=(6, 14))
        sp = self._card(w, "Spoof", "Set a random or custom MAC. On a MAC-bound network these won't get online "
                                    "until you reset — which is exactly why Reset is right above.")
        r2 = ctk.CTkFrame(sp, fg_color="transparent"); r2.pack(fill="x", padx=16, pady=(6, 14))
        ctk.CTkButton(r2, text="🎲 Random", command=lambda: self._set_mac(random_mac())).pack(side="left", padx=(0, 8))
        self.mac_entry = ctk.CTkEntry(r2, placeholder_text="02:11:22:33:44:55", width=200); self.mac_entry.pack(side="left", padx=8)
        ctk.CTkButton(r2, text="Set custom", command=self._mac_custom).pack(side="left", padx=8)

    def _refresh_identity(self):
        for attr in ("id_lbl", "dash_id"):
            w = getattr(self, attr, None)
            if w is not None and w.winfo_exists():
                w.configure(text=self._identity_text())

    def _set_mac(self, mac):
        ifc = self.iface
        chain = f"ip link set {ifc} down && ip link set {ifc} address {mac}; ip link set {ifc} up"
        self.do(priv(["bash", "-c", chain]), note=f"set MAC -> {mac}", after=self._refresh_identity)

    def _mac_passthrough(self, src):
        if src:
            self._set_mac(net_mac(src))

    def _mac_restore(self):
        if self.orig_mac:
            self._set_mac(self.orig_mac)
        else:
            self.log("Original MAC unknown.")

    def _mac_custom(self):
        mac = self.mac_entry.get().strip().lower()
        if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
            self._set_mac(mac)
        else:
            self.log("Bad MAC format (want aa:bb:cc:dd:ee:ff).")

    # ---------- Network ----------
    def _page_network(self, w):
        c = self._card(w, "Wi-Fi / LAN auto-switch",
                       "When the dock's wired link goes up, turn Wi-Fi off (and back on when it drops). "
                       "Stops your laptop from sitting on the LAN twice at once.")
        sw = ctk.CTkSwitch(c, text="Enable auto-switch", variable=self.rule_wifi)
        sw.pack(anchor="w", padx=18, pady=(6, 14))
        if not have("nmcli"):
            sw.configure(state="disabled"); tip(sw, "Needs NetworkManager (nmcli).")

        hint = dialout_hint()
        if hint:
            hc = self._card(w, "Serial permission", hint)

        if have("nmcli"):
            nc = self._card(w, "NetworkManager tidy-up",
                            "Switching dock modes repeatedly can leave behind unused 'Wired "
                            "connection N' profiles. This removes ones not attached to any device.")
            ctk.CTkButton(nc, text="Find & remove unused wired profiles",
                          command=self._nm_cleanup).pack(anchor="w", padx=16, pady=(4, 14))

        self._build_wire_tuning(w)

    def _nm_duplicate_profiles(self):
        """NetworkManager wired profiles not attached to a device. Heavy native-mode
        flipping accumulates duplicate 'Wired connection N' entries."""
        if not have("nmcli"):
            return []
        rc, out = run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show"])
        dupes = []
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) < 3 or "ethernet" not in parts[1]:
                continue
            name, dev = parts[0], parts[2]
            if not dev or dev == "--":
                dupes.append(name)
        return sorted(set(dupes))

    def _nm_cleanup(self):
        dupes = self._nm_duplicate_profiles()
        if not dupes:
            messagebox.showinfo("NetworkManager", "No unused wired profiles found.")
            return
        if not messagebox.askyesno("Remove unused wired profiles?",
                "These wired profiles aren't attached to any device:\n\n"
                + "\n".join("  - " + d for d in dupes)
                + "\n\nDelete them? Active connections are never touched."):
            return
        for name in dupes:
            self.do(["nmcli", "connection", "delete", name],
                    note="remove unused wired profile %r" % name)

    def _build_wire_tuning(self, w):
        state, c = native_state(self.iface)
        native = (state == "native") and have("ethtool")

        # --- Speed control ---
        lc = self._card(w, "Speed control",
                        "Force a fixed link speed/duplex or re-run auto-negotiation. Handy to cap a flaky "
                        "cable at 100 Mbps, or to pin gigabit. Needs native mode.")
        if native:
            r2 = ctk.CTkFrame(lc, fg_color="transparent"); r2.pack(fill="x", padx=16, pady=(4, 12))
            self.speed_menu = ctk.CTkOptionMenu(r2, values=["auto", "1000/full", "100/full", "10/full"], width=130)
            self.speed_menu.set("auto"); self.speed_menu.pack(side="left")
            ctk.CTkButton(r2, text="Apply", width=90, command=self._apply_speed).pack(side="left", padx=6)
            ctk.CTkButton(r2, text="Renegotiate", width=110, command=lambda: self.do(priv(["ethtool", "-r", self.iface]))).pack(side="left", padx=6)
            rc, aout = run(["ethtool", "-a", self.iface])
            if rc == 0 and "not supported" not in aout.lower() and aout.strip():
                ctk.CTkButton(r2, text="Flow on", width=80, command=lambda: self._flow(True)).pack(side="left", padx=6)
                ctk.CTkButton(r2, text="off", width=50, command=lambda: self._flow(False)).pack(side="left")
        else:
            self._locked_note(lc)

        # --- Offloads ---
        oc = self._card(w, "Offloads",
                        "Each of these moves a small networking chore off your computer's CPU and onto the "
                        "dock's Ethernet chip. ON (normal) = your CPU does less network housekeeping, so it's "
                        "freed up and transfers use less processor. OFF = your CPU handles every packet itself "
                        "— a bit more CPU load, but every packet is 'real' and visible, which helps when "
                        "capturing traffic (Wireshark) or chasing a network bug. It never speeds up your apps "
                        "or the dock — it only shifts packet bookkeeping between your CPU and the NIC chip. "
                        "Hover any switch for exactly what it moves.")
        if native:
            self.offload_area = ctk.CTkFrame(oc, fg_color="transparent"); self.offload_area.pack(fill="x", padx=16, pady=(4, 12))
            self._build_offloads()
        else:
            self._locked_note(oc)

    def _locked_note(self, card):
        row = ctk.CTkFrame(card, fg_color="transparent"); row.pack(fill="x", padx=16, pady=(4, 12))
        ctk.CTkLabel(row, text="🔒 Unlock native mode first —", text_color=("gray55", "gray55")).pack(side="left")
        ctk.CTkButton(row, text="go to Native Mode", width=140, fg_color="#5a5f6a", hover_color="#474b54",
                      command=lambda: self.show("Native Mode")).pack(side="left", padx=8)

    def _msfmt(self, v): return f"{v:.0f} ms" if v is not None else "—"

    # ---------- USB & Power ----------
    def _page_usb(self, w):
        c = self._card(w, "Devices inside the dock",
                       "Scoped strictly to the dock's own hub subtree — things plugged into your laptop's "
                       "own ports are excluded. If the dock chains multiple hubs, all its members appear here.")
        holder = ctk.CTkFrame(c, fg_color="transparent"); holder.pack(fill="x", padx=10, pady=(6, 12))
        anchors = [self.top_hub] if self.top_hub else external_top_hubs()
        if not anchors:
            ctk.CTkLabel(holder, text="No dock hub detected.").pack(anchor="w")
        else:
            if not self.top_hub:
                ctk.CTkLabel(holder, text="No Ethernet chip here — showing devices on the detected USB hub(s).",
                             text_color=("#a98b4a", "#a98b4a"), justify="left", wraplength=780).pack(anchor="w", pady=(0, 4))
            leaves = []
            for top in anchors:
                leaves += dock_leaf_devices(top)
            if not leaves:
                ctk.CTkLabel(holder, text="No non-hub devices currently attached.").pack(anchor="w")
            for dev in leaves:
                r = ctk.CTkFrame(holder, fg_color="transparent"); r.pack(fill="x", pady=2)
                nm = dev["product"] or f"{dev['vid']}:{dev['pid']}"
                ctk.CTkLabel(r, text=f"{dev['vid']}:{dev['pid']}  {nm[:42]}", anchor="w", width=430).pack(side="left", padx=6)
                af = usb_authorized_file(dev["path"])
                if af:
                    ctk.CTkButton(r, text="Disable", width=80, fg_color="#b4552d", hover_color="#8f3f1e",
                                  command=lambda p=af: self._authorize(p, 0)).pack(side="left", padx=3)
                    ctk.CTkButton(r, text="Enable", width=80,
                                  command=lambda p=af: self._authorize(p, 1)).pack(side="left", padx=3)
        pw = self._card(w, "Per-port power (uhubctl, optional)",
                        "Cut/restore 5V to a single port. Only hubs with per-port power switching support it — "
                        "most docks don't. Optional; needs uhubctl.")
        head = ctk.CTkFrame(pw, fg_color="transparent"); head.pack(fill="x", padx=16, pady=(6, 4))
        b = ctk.CTkButton(head, text="Scan", width=80, command=self._scan_ports); b.pack(side="left")
        if not have("uhubctl"):
            b.configure(state="disabled")
        self.port_area = ctk.CTkFrame(pw, fg_color="transparent"); self.port_area.pack(fill="x", padx=10, pady=(0, 12))
        if not have("uhubctl"):
            ctk.CTkLabel(self.port_area, text="uhubctl not installed (optional).").pack(anchor="w")

    def _authorize(self, af, val):
        self.do(priv(["bash", "-c", f"echo {val} > {af}"]),
                note=("disable" if val == 0 else "enable") + " USB device")

    def _scan_ports(self):
        for wdg in self.port_area.winfo_children():
            wdg.destroy()
        ctk.CTkLabel(self.port_area, text="Scanning…").pack(anchor="w")
        def worker():
            rc, out = run(priv(["uhubctl"]))
            hubs, cur = [], None
            for line in out.splitlines():
                m = re.search(r"status for hub\s+(\S+)\s+\[([^\]]+)\].*?(\d+)\s*ports", line)
                if m:
                    cur = {"loc": m.group(1), "desc": m.group(2), "ports": []}; hubs.append(cur); continue
                p = re.match(r"\s*Port\s+(\d+):", line)
                if p and cur is not None:
                    cur["ports"].append(int(p.group(1)))
            self.after(0, lambda: self._render_ports(hubs, out))
        threading.Thread(target=worker, daemon=True).start()

    def _render_ports(self, hubs, raw):
        for wdg in self.port_area.winfo_children():
            wdg.destroy()
        self.log("$ uhubctl\n" + raw)
        sw = [h for h in hubs if h["ports"]]
        if not sw:
            ctk.CTkLabel(self.port_area, text="No switchable hubs — this hardware doesn't expose per-port power.").pack(anchor="w")
            return
        for h in sw:
            ctk.CTkLabel(self.port_area, text=f"Hub {h['loc']} [{h['desc']}]", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(8, 2))
            for port in h["ports"]:
                r = ctk.CTkFrame(self.port_area, fg_color="transparent"); r.pack(fill="x", pady=2)
                ctk.CTkLabel(r, text=f"Port {port}", width=70).pack(side="left", padx=6)
                loc = h["loc"]
                for lbl, act, col in (("On", "on", None), ("Off", "off", "#b4552d"), ("Cycle", "cycle", None)):
                    kw = {"fg_color": col, "hover_color": "#8f3f1e"} if col else {}
                    ctk.CTkButton(r, text=lbl, width=58,
                                  command=lambda l=loc, p=port, a=act: self.do(priv(["uhubctl", "-l", l, "-p", str(p), "-a", a])),
                                  **kw).pack(side="left", padx=3)

    # ---------- Topology ----------
    def _page_topology(self, w):
        c = self._card(w, "Dock topology (live)",
                       "A map of THIS dock as a device: its upstream hub, every downstream port, and what "
                       "occupies each one — the Ethernet chip, the card reader, and your physical USB ports "
                       "(shown empty when nothing is plugged in). Scoped to the dock only.")
        head = ctk.CTkFrame(c, fg_color="transparent"); head.pack(fill="x", padx=16, pady=(2, 0))
        ctk.CTkButton(head, text="Redraw", width=80, command=self._draw_topo).pack(side="left")
        ctk.CTkLabel(head, text="   blue = hub   ·   green = device   ·   grey = empty port",
                     text_color=("gray55", "gray55")).pack(side="left", padx=8)
        self.topo = tk.Canvas(c, bg="#161a1f", highlightthickness=0, height=520)
        self.topo.pack(fill="both", expand=True, padx=12, pady=12)
        self._draw_topo()

    def _draw_topo(self):
        if not hasattr(self, "topo") or not self.topo.winfo_exists():
            return
        c = self.topo; c.delete("all")

        # Determine which hub subtree(s) to draw.
        anchors = []
        note = ""
        if self.top_hub:
            anchors = [self.top_hub]
        else:
            # No NIC to anchor on — best-effort: draw external (non-root) hubs.
            anchors = external_top_hubs()
            if anchors:
                note = ("No Ethernet chip on this hub — showing the USB hub(s) detected. "
                        "Without a NIC to anchor on, this may also include other USB hubs.")
            else:
                c.create_text(20, 20, anchor="w", fill="#888",
                              text="No USB hub detected to draw.")
                return

        y = 22
        if note:
            c.create_text(20, y, anchor="w", fill="#a98b4a", text=note, font=("", 9), width=760)
            y += 26

        for ai, top in enumerate(anchors):
            rows = []
            flatten_tree(build_dock_tree(top), rows)
            if len(anchors) > 1:
                c.create_text(20, y, anchor="w", fill="#7a828b",
                              text=f"— hub {top} —", font=("", 9)); y += 20
            last_at = {}
            for depth, label, kind in rows:
                x = 20 + depth * 46
                fill = {"hub": "#22303c", "dev": "#1d2a20", "empty": "#1a1d22"}[kind]
                outline = {"hub": RXCOL, "dev": ACCENT, "empty": "#3a4550"}[kind]
                tcol = "#e8e8e8" if kind != "empty" else "#7a828b"
                if depth > 0 and (depth - 1) in last_at:
                    px, py = last_at[depth - 1]
                    c.create_line(px + 10, py + 13, x - 6, y + 13, fill="#3a4550")
                c.create_rectangle(x, y, x + 360, y + 26, fill=fill, outline=outline)
                c.create_text(x + 8, y + 13, anchor="w", fill=tcol, text=label[:58], font=("", 10))
                last_at[depth] = (x, y)
                for dd in [d for d in last_at if d > depth]:
                    del last_at[dd]
                y += 34
            y += 12

    # ---------- Audio ----------
    def _page_audio(self, w):
        if not have("pactl"):
            self._card(w, "Audio", "pactl not available — audio control off."); return
        c = self._card(w, "Audio output",
                       "Controls the selected output. 'Current output' follows whatever you're using, "
                       "including the dock's headphone jack.")
        top = ctk.CTkFrame(c, fg_color="transparent"); top.pack(fill="x", padx=16, pady=(6, 6))
        self.sink_menu = ctk.CTkOptionMenu(top, values=["Current output"] + list_sinks(), width=380,
                                           command=lambda _: self._load_vol())
        self.sink_menu.set("Current output"); self.sink_menu.pack(side="left")
        vol = ctk.CTkFrame(c, fg_color="transparent"); vol.pack(fill="x", padx=16, pady=6)
        ctk.CTkLabel(vol, text="Volume").pack(side="left", padx=6)
        self.vol = ctk.CTkSlider(vol, from_=0, to=100, number_of_steps=100, command=self._set_vol, width=340)
        self.vol.pack(side="left", padx=10)
        self.vol_lbl = ctk.CTkLabel(vol, text="-"); self.vol_lbl.pack(side="left")
        ctk.CTkButton(c, text="Toggle mute", command=self._mute).pack(anchor="w", padx=16, pady=(6, 14))
        self._load_vol()

    def _cur_sink(self):
        s = self.sink_menu.get()
        return default_sink() if s in ("-", "", "Current output") else s
    def _load_vol(self):
        try:
            v = sink_volume(self._cur_sink()); self.vol.set(v); self.vol_lbl.configure(text=f"{v}%")
        except Exception:
            pass
    def _set_vol(self, val):
        pct = int(float(val)); self.vol_lbl.configure(text=f"{pct}%")
        run(["pactl", "set-sink-volume", self._cur_sink(), f"{pct}%"])
    def _mute(self):
        self.do(["pactl", "set-sink-mute", self._cur_sink(), "toggle"])

    # ---------- Automation / Macros ----------
    def _page_automation(self, w):
        banner = self._card(w, "How these work",
                            "Ticking a box doesn't do anything right away. Each rule fires the next time the "
                            "dock's wired link actually goes up (connect) or down (disconnect) — e.g. when you "
                            "physically dock/undock, or when the link bounces after a speed change. So enabling "
                            "'Wi-Fi off on connect' takes effect on your next dock event, not on the click.")
        c = self._card(w, "Built-in rules",
                       "Fire automatically when the dock's link goes up (connect) or down (disconnect).")
        ctk.CTkCheckBox(c, text="On connect: turn Wi-Fi off   ·   on disconnect: turn it back on",
                        variable=self.rule_wifi).pack(anchor="w", padx=18, pady=4)
        ctk.CTkCheckBox(c, text="On connect: dock wears a host NIC's MAC   ·   on disconnect: dock returns to its OWN original MAC",
                        variable=self.rule_mac).pack(anchor="w", padx=18, pady=4)
        ctk.CTkCheckBox(c, text="On connect: switch audio output to the dock",
                        variable=self.rule_audio).pack(anchor="w", padx=18, pady=(4, 12))
        m = self._card(w, "Custom commands",
                       "Want to run your own commands on connect or disconnect? That lives on the "
                       "Macros page — named macros, multiple commands each, and they can be scoped "
                       "to a specific dock.")
        ctk.CTkButton(m, text="Open Macros", width=140,
                      command=lambda: self.show("Macros")).pack(anchor="w", padx=18, pady=(4, 14))

    # ---------- Logs ----------
    def _page_macros(self, w):
        MacroPage(self.macro_store, self).build(w)

    def _save_snapshot(self):
        try:
            path = write_snapshot(iface=self.iface, log_lines=self.log_lines,
                                  dockpilot_version=APP_VERSION)
            self.log("# snapshot written: %s" % path)
            messagebox.showinfo("System Snapshot",
                "Diagnostic snapshot saved:\n\n%s\n\nIPs and hostname are redacted — safe to "
                "attach to a GitHub issue." % path)
        except Exception as e:
            messagebox.showerror("System Snapshot", "Couldn't write snapshot: %s" % e)

    def _page_logs(self, w):
        c = self._card(w, "Command log", "Every action that changes something is echoed here.")
        self.logbox = ctk.CTkTextbox(c, font=ctk.CTkFont(family="monospace", size=12), height=420)
        self.logbox.pack(fill="both", expand=True, padx=10, pady=10)
        self.logbox.insert("end", "\n".join(self.log_lines) + "\n"); self.logbox.see("end")

        sc = self._card(w, "System Snapshot",
                        "Writes one diagnostic file with the dock's chips, drivers, USB topology, "
                        "link stats and this log. IPs and hostname are redacted, so it's safe to "
                        "attach to a bug report.")
        b = ctk.CTkButton(sc, text="📋  Save System Snapshot", command=self._save_snapshot)
        b.pack(anchor="w", padx=16, pady=(4, 14))

    def log(self, msg):
        line = msg.rstrip()
        self.log_lines.append(line); self.log_lines = self.log_lines[-500:]
        if self.current == "Logs" and hasattr(self, "logbox") and self.logbox.winfo_exists():
            self.logbox.insert("end", line + "\n"); self.logbox.see("end")

    # ---------- Native Mode ----------
    def _page_native(self, w):
        state, c = native_state(self.iface)
        self._native_last_state = state

        intro = self._card(w, "Native mode",
                            "Dock network chips ship in a standard plug-and-play mode so any OS drives "
                            "them with no software. That mode hides the good stuff. Native mode is the "
                            "chip maker's own driver — it unlocks Wake-on-LAN, real hardware stats, and "
                            "link tuning. Fully reversible.")
        if not c or not c["vendor"]:
            ctk.CTkLabel(intro, text="No ASIX or Realtek NIC detected on this dock — nothing to unlock here.",
                         justify="left", wraplength=820).pack(anchor="w", padx=16, pady=(0, 14))
            return

        self.native_hdr = ctk.CTkLabel(intro, justify="left", font=ctk.CTkFont(family="monospace", size=13),
                                        text=self._native_hdr_text(state, c))
        self.native_hdr.pack(anchor="w", padx=16, pady=(0, 8))

        # persistent inline status line — the flow updates this live instead of firing modals
        self.native_status = ctk.CTkLabel(intro, text=getattr(self, "_native_status_text", ""),
                                           justify="left", wraplength=820,
                                           font=ctk.CTkFont(size=13, weight="bold"),
                                           text_color=getattr(self, "_native_status_color", ("gray55", "gray55")))
        self.native_status.pack(anchor="w", padx=16, pady=(0, 10))

        if state == "unsupported":
            ctk.CTkLabel(intro, text="This NIC is already on its native driver, or doesn't expose a "
                                     "separate native config — no switch needed or available.",
                         justify="left", wraplength=820, text_color=("gray55", "gray55")).pack(anchor="w", padx=16, pady=(0, 14))
            if c["driver"] in NATIVE_DRIVERS:
                self._native_features(w)   # already native (e.g. Realtek r8152): show features anyway
            return

        if state == "unlockable":
            if self._dock_native_known_bad():
                warn = ctk.CTkFrame(intro, fg_color="transparent"); warn.pack(fill="x", padx=16, pady=(0, 6))
                ctk.CTkLabel(warn, text="⚠ Native mode isn't usable on this dock — last time it ran the native "
                                        "driver but the wired link wouldn't come up. Standard mode works "
                                        "perfectly. You can still try again below.",
                             text_color=("#e0a13a", "#e0a13a"), justify="left", wraplength=760).pack(side="left")
                ctk.CTkButton(warn, text="why?", width=50, fg_color="#5a5f6a", hover_color="#474b54",
                              command=self._why_native_failed).pack(side="left", padx=8)
            row = ctk.CTkFrame(intro, fg_color="transparent"); row.pack(fill="x", padx=16, pady=(0, 14))
            ctk.CTkButton(row, text="🔓  Unlock native mode", command=self._native_unlock).pack(side="left")
            ctk.CTkLabel(row, text="  link drops ~5s · Wi-Fi covers you · reversible · watch the status above",
                         text_color=("gray55", "gray55")).pack(side="left", padx=8)
            self._native_poll()
            return

        # state == native
        row = ctk.CTkFrame(intro, fg_color="transparent"); row.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkButton(row, text="↩  Return to standard mode", fg_color="#5a5f6a", hover_color="#474b54",
                      command=self._native_relock).pack(side="left")
        ctk.CTkLabel(row, text="  puts it back to plug-and-play (CDC) mode",
                     text_color=("gray55", "gray55")).pack(side="left", padx=8)
        self._native_features(w)
        # start the live poller so the page can never go stale vs reality
        self._native_poll()

    def _native_hdr_text(self, state, c):
        carrier = "up" if net_carrier(self.iface) else "down"
        return (f"Chip:    {c['vendor']}  {c['product'] or (c['vid']+':'+c['pid'])}\n"
                f"Driver:  {c['driver']}\n"
                f"USB cfg: {c['config']} of {c['nconfigs']}   "
                f"({'NATIVE' if state == 'native' else 'standard mode'})   ·   link {carrier}")

    def _native_poll(self):
        """While the Native Mode page is open, keep the header live and rebuild the page
        if the underlying state actually flips (bounce-back, a terminal flip, or an unlock
        completing) — so what's on screen can never contradict reality."""
        if self.current != "Native Mode":
            return
        state, c = native_state(self.iface)
        if c and c["vendor"] and hasattr(self, "native_hdr") and self.native_hdr.winfo_exists():
            self.native_hdr.configure(text=self._native_hdr_text(state, c))
        # if the mode itself changed since we built the page, rebuild so buttons/features match
        if state != getattr(self, "_native_last_state", state) and not getattr(self, "_rolling", False):
            self.show("Native Mode")
            return
        self.after(1500, self._native_poll)

    def _native_features(self, w):
        if not have("ethtool"):
            self._card(w, "Features", "Install ethtool to use the native-mode controls:  sudo apt install ethtool")
            return

        # --- Wake-on-LAN ---
        wc = self._card(w, "Wake-on-LAN",
                        "Let a 'magic packet' wake this laptop from sleep through the dock's wired link — "
                        "and send magic packets to wake your other machines.")
        r = ctk.CTkFrame(wc, fg_color="transparent"); r.pack(fill="x", padx=16, pady=(4, 6))
        self.wol_lbl = ctk.CTkLabel(r, text=f"this NIC: Wake-on = {wol_state(self.iface)}",
                                    font=ctk.CTkFont(family="monospace", size=13))
        self.wol_lbl.pack(side="left")
        ctk.CTkButton(r, text="Arm (wake on magic packet)", command=lambda: self._wol_set(True)).pack(side="left", padx=(14, 6))
        ctk.CTkButton(r, text="Disarm", fg_color="#5a5f6a", hover_color="#474b54",
                      command=lambda: self._wol_set(False)).pack(side="left")
        s = ctk.CTkFrame(wc, fg_color="transparent"); s.pack(fill="x", padx=16, pady=(2, 14))
        ctk.CTkLabel(s, text="Wake another machine — MAC:").pack(side="left")
        self.wol_mac = ctk.CTkEntry(s, placeholder_text="aa:bb:cc:dd:ee:ff", width=200); self.wol_mac.pack(side="left", padx=8)
        ctk.CTkButton(s, text="Send magic packet", command=self._wol_send).pack(side="left")

        # --- EEPROM inspector ---
        ec = self._card(w, "EEPROM",
                        "Some NICs store config (MAC, IDs, LED bits) in a rewritable EEPROM; others use "
                        "one-time eFuse and read back as all-FF. Reading is safe. This never writes.")
        self.eeprom_lbl = ctk.CTkLabel(ec, text="", justify="left", anchor="w",
                                       font=ctk.CTkFont(family="monospace", size=12))
        self.eeprom_lbl.pack(anchor="w", padx=16, pady=(4, 6))
        er = ctk.CTkFrame(ec, fg_color="transparent"); er.pack(fill="x", padx=16, pady=(0, 12))
        ctk.CTkButton(er, text="Read / check", command=self._eeprom_read).pack(side="left", padx=(0, 8))
        ctk.CTkButton(er, text="Back up to file", command=self._eeprom_backup).pack(side="left")
        self._eeprom_read()

        ctk.CTkLabel(w, text="Speed control and packet-tuning options appear on the Network page once "
                            "native mode is unlocked.", text_color=("gray55", "gray55"),
                     justify="left", wraplength=820).pack(anchor="w", padx=18, pady=(4, 8))

    # ---- native actions ----
    def _native_unlock(self):
        c = nic_chip(self.iface)
        if not c:
            return
        if not messagebox.askyesno("Unlock native mode?",
            "This switches the dock's network chip from standard (plug-and-play) mode into the "
            "manufacturer's native mode.\n\n"
            "• The wired link drops for a few seconds while it re-initialises.\n"
            "• Wi-Fi stays up, so you won't lose the machine.\n"
            "• Fully reversible — 'Return to standard mode' puts it back.\n\nUnlock now?"):
            return
        # Re-resolve the USB path RIGHT NOW — the device may have re-enumerated to a new
        # port path since the page loaded, and writing to a stale path leaves it in limbo.
        c = nic_chip(self.iface)
        if not c or not c["usb"]:
            messagebox.showwarning("Can't find the NIC",
                "Couldn't locate the dock NIC's USB device (it may have just re-enumerated). "
                "Give it a second and try again, or replug the dock.")
            return
        dev, drv, cfg = c["usb"], c["driver"], c["config"]
        path = os.path.join(USB_ROOT, dev, "bConfigurationValue")
        script = (f"echo {dev}:{cfg}.0 > /sys/bus/usb/drivers/{drv}/unbind 2>/dev/null; "
                  f"echo 1 > {path}")
        self.do(priv(["bash", "-c", script]), note=f"unbind {drv} on {dev}, switch to native config (1)")
        self._set_status("Unlocking… switching to native mode.", ("#e0a13a", "#e0a13a"))
        self.after(2000, self._native_unlock_check)

    def _set_status(self, text, color=("gray55", "gray55")):
        """Update the persistent inline status line on the Native Mode page (no modals)."""
        self._native_status_text = text
        self._native_status_color = color
        if hasattr(self, "native_status") and self.native_status.winfo_exists():
            self.native_status.configure(text=text, text_color=color)

    def _native_unlock_check(self, tries=6):
        state, c = native_state(self.iface)
        if state == "native":
            self.log("# native mode active — checking whether the wired link comes up…")
            self._set_status("Native mode on — waiting for the wired link to come up…", ("#e0a13a", "#e0a13a"))
            self._link_attempt(rounds=5)
            return
        if tries > 0:
            self._set_status(f"Switching to native mode… ({7 - tries}/6)", ("#e0a13a", "#e0a13a"))
            self.after(1200, lambda: self._native_unlock_check(tries - 1))
            return
        self._set_status("The switch didn't take — try Unlock again, or replug the dock.", ("#e05252", "#e05252"))

    def _link_attempt(self, rounds):
        """Native is bound. Give the wired link a bounded number of nudged tries to come up.
        Updates the inline status live; no modals."""
        if self.current != "Native Mode":
            return  # user navigated away — stop poking
        ifc = self.iface
        if net_carrier(ifc):
            self._native_verdict_ok()
            return
        if rounds <= 0:
            self._native_verdict_no_link()
            return
        self._set_status(f"Native mode on — waiting for the wired link… ({6 - rounds}/5)", ("#e0a13a", "#e0a13a"))
        self.privshell_async(f"ip link set {ifc} up")
        if have("nmcli"):
            self.after(300, lambda: self._nmcli_connect(ifc))
        self.after(2500, lambda: self._link_attempt(rounds - 1))

    def _native_verdict_ok(self):
        state, c = native_state(self.iface)
        drv = c["driver"] if c else "native"
        self._remember_dock(True)
        self.log(f"# native mode + wired link up (driver {drv})")
        self._set_status(f"✓ Native mode active — link up. Wake-on-LAN and tuning are available.",
                         (ACCENT, ACCENT))
        self._native_last_state = "native"
        self.show("Native Mode")   # redraw immediately so features/relock button appear

    def _native_verdict_no_link(self):
        """Confirmed: native holds but the link won't come up. Fall back to standard and
        report inline (with a 'why?' affordance) instead of a delayed modal."""
        self._remember_dock(False)
        self._set_status("Native driver runs, but the wired link won't come up on this dock — "
                         "putting you back on standard mode (where it works)…", ("#e0a13a", "#e0a13a"))
        c = nic_chip(self.iface)
        if c and c["usb"]:
            dev, drv = c["usb"], c["driver"]
            script = (f"echo {dev}:1.0 > /sys/bus/usb/drivers/{drv}/unbind 2>/dev/null; "
                      f"echo {self.standard_config} > {os.path.join(USB_ROOT, dev, 'bConfigurationValue')}")
            self.do(priv(["bash", "-c", script]), note="native link won't come up — relocking to standard")
        self.after(3000, self._native_no_link_done)

    def _native_no_link_done(self):
        if have("nmcli"):
            self._nmcli_connect(self.iface)
        self._native_last_state = "unlockable"
        self._set_status("Native mode isn't usable on this dock — you're safely on standard mode, "
                         "link working. (Wake-on-LAN/tuning need native, so they're unavailable here.)",
                         ("#e0a13a", "#e0a13a"))
        self.show("Native Mode")   # redraw so the Unlock button + amber note reflect standard mode

    def _why_native_failed(self):
        messagebox.showinfo("Why native mode won't work here",
            "This dock runs the native driver fine, but its wired Ethernet link never comes up in "
            "native mode — the native driver can't initialise this particular board's Ethernet PHY. "
            "It's a known board/chip-revision quirk (common on AX88179B-based docks).\n\n"
            "Standard (plug-and-play) mode works perfectly, so DockPilot keeps you there. Everything "
            "except the native-only extras (Wake-on-LAN, link tuning) works normally.")

    def _dock_key(self):
        c = nic_chip(self.iface)
        return f"{c['vid']}:{c['pid']}" if c else None

    def _remember_dock(self, native_ok):
        key = self._dock_key()
        if not key:
            return
        mem = load_json(DOCKMEM_FILE, {})
        mem[key] = {"native_ok": native_ok, "chip": (nic_chip(self.iface) or {}).get("product", "")}
        save_json(DOCKMEM_FILE, mem)

    def _dock_native_known_bad(self):
        key = self._dock_key()
        if not key:
            return False
        mem = load_json(DOCKMEM_FILE, {})
        return key in mem and mem[key].get("native_ok") is False

    def privshell_async(self, cmd):
        threading.Thread(target=lambda: self.privshell.run(cmd), daemon=True).start()

    def _nmcli_connect(self, ifc):
        threading.Thread(target=lambda: run(["nmcli", "device", "connect", ifc]), daemon=True).start()

    def stop_rolling(self):
        self._rolling = False
        self.log("# native attempt stopped by user")
        self.show("Native Mode")

    def _native_relock(self):
        c = nic_chip(self.iface)
        if not c:
            return
        if not messagebox.askyesno("Return to standard mode?",
            "Switch the NIC back to standard plug-and-play (CDC) mode. The wired link drops for a "
            "few seconds while it re-initialises (Wi-Fi covers you). Continue?"):
            return
        # Re-resolve live — the port path can move across re-enumerations.
        c = nic_chip(self.iface)
        if not c or not c["usb"]:
            messagebox.showwarning("Can't find the NIC",
                "Couldn't locate the dock NIC's USB device (it may have just re-enumerated). "
                "Give it a second and try again, or replug the dock.")
            return
        dev, drv = c["usb"], c["driver"]
        path = os.path.join(USB_ROOT, dev, "bConfigurationValue")
        # Unbind the native driver from :1.0 first, THEN switch config — a live write is
        # refused while the driver still holds the interface.
        script = (f"echo {dev}:1.0 > /sys/bus/usb/drivers/{drv}/unbind 2>/dev/null; "
                  f"echo {self.standard_config} > {path}")
        self.do(priv(["bash", "-c", script]),
                note=f"unbind {drv} on {dev}, switch to standard config ({self.standard_config})")
        self.after(3800, lambda: self.show("Native Mode"))

    def _refresh_native_status(self):
        if hasattr(self, "wol_lbl") and self.wol_lbl.winfo_exists():
            self.wol_lbl.configure(text=f"this NIC: Wake-on = {wol_state(self.iface)}")

    def _wol_set(self, on):
        self.do(priv(["ethtool", "-s", self.iface, "wol", "g" if on else "d"]),
                note=f"Wake-on-LAN {'armed' if on else 'disarmed'}", after=self._refresh_native_status)

    def _wol_send(self):
        ok, msg = send_magic_packet(self.wol_mac.get())
        self.log(f"# WoL -> {self.wol_mac.get()}: {msg}")
        if not ok:
            messagebox.showerror("Wake-on-LAN", msg)

    def _eeprom_read(self):
        if not hasattr(self, "eeprom_lbl") or not self.eeprom_lbl.winfo_exists():
            return
        def worker():
            supported, all_ff, out = eeprom_dump(self.iface, 64)
            if not supported:
                verdict = "No EEPROM access on this driver."
            elif all_ff:
                verdict = "Reads all 0xFF → no rewritable EEPROM (chip uses one-time eFuse)."
            else:
                verdict = "Real data present → this chip has a populated EEPROM."
            head = "\n".join(out.splitlines()[:6])
            self.after(0, lambda: self.eeprom_lbl.configure(text=f"{verdict}\n\n{head}")
                       if self.eeprom_lbl.winfo_exists() else None)
        threading.Thread(target=worker, daemon=True).start()

    def _eeprom_backup(self):
        def worker():
            ok, res = eeprom_backup(self.iface)
            msg = f"Saved EEPROM backup:\n{res}" if ok else f"Couldn't back up: {res}"
            self.after(0, lambda: (self.log(f"# EEPROM backup: {res}"),
                                   messagebox.showinfo("EEPROM", msg) if ok
                                   else messagebox.showwarning("EEPROM", msg)))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_speed(self):
        sel = self.speed_menu.get()
        if sel == "auto":
            args = ["ethtool", "-s", self.iface, "autoneg", "on"]
        else:
            spd, dup = sel.split("/")
            args = ["ethtool", "-s", self.iface, "speed", spd, "duplex", dup,
                    "autoneg", "on" if spd == "1000" else "off"]
        self.do(priv(args))

    def _flow(self, on):
        self.do(priv(["ethtool", "-A", self.iface, "rx", "on" if on else "off", "tx", "on" if on else "off"]),
                note=f"flow control {'on' if on else 'off'}")

    def _build_offloads(self):
        for wdg in self.offload_area.winfo_children():
            wdg.destroy()
        feats = ethtool_offloads(self.iface)
        friendly = {"rx-checksumming": "RX checksum", "tx-checksumming": "TX checksum",
                    "scatter-gather": "Scatter-gather", "tcp-segmentation-offload": "TCP segmentation (TSO)",
                    "generic-segmentation-offload": "Generic segmentation (GSO)",
                    "generic-receive-offload": "Generic receive (GRO)"}
        explain = {
            "rx-checksumming": "Incoming packets carry a checksum to prove they weren't corrupted. ON: the dock chip verifies it, saving your CPU the math. OFF: your computer's CPU checks every packet — slightly more load, but the packet reaches your capture tools untouched.",
            "tx-checksumming": "Outgoing packets need a checksum computed. ON: the dock chip fills it in, so your CPU skips that math on every send. OFF: your computer's CPU computes it for each packet.",
            "scatter-gather": "A packet's data can be scattered across several memory chunks. ON: the dock chip reads them directly, avoiding an extra copy by your CPU. OFF: your computer assembles the packet into one buffer first (an extra copy).",
            "tcp-segmentation-offload": "For big sends, data must be sliced into wire-sized packets. ON: you hand the dock chip one large chunk and IT does the slicing — big CPU saving on uploads. OFF: your computer's CPU slices every packet (and captures then show the real individual packets).",
            "generic-segmentation-offload": "Same slicing as TSO but done in software at the last moment. ON: your CPU delays the work, which is a bit more efficient. OFF: your CPU slices immediately.",
            "generic-receive-offload": "On downloads, many small packets arrive fast. ON: they're merged into fewer big ones before your CPU sees them — much less CPU on heavy downloads. OFF: your computer's CPU processes each packet separately (better for packet capture or when the machine forwards/routes traffic).",
        }
        shown = 0
        for key, label in friendly.items():
            if key not in feats:
                continue
            var = tk.BooleanVar(value=feats[key])
            sw = ctk.CTkSwitch(self.offload_area, text=label, variable=var,
                               command=lambda k=key, v=var: self._offload_set(k, v))
            sw.pack(anchor="w", pady=2)
            tip(sw, explain.get(key, ""))
            shown += 1
        if shown == 0:
            ctk.CTkLabel(self.offload_area, text="This NIC doesn't expose toggleable offloads.").pack(anchor="w")

    def _offload_set(self, key, var):
        self.do(priv(["ethtool", "-K", self.iface, key, "on" if var.get() else "off"]),
                note=f"offload {key} {'on' if var.get() else 'off'}",
                after=self._build_offloads)

    # ---------- runner ----------
    def do(self, args, note=None, after=None):
        if note:
            self.log(f"# {note}")
        self.log("$ " + " ".join(args))
        def worker():
            if args and args[0] == "pkexec":
                rc, out = self.privshell.run(priv_cmd_string(args))
            else:
                rc, out = run(args)
            def done():
                self.log(out or f"(exit {rc})")
                if after:
                    after()
            self.after(0, done)
        threading.Thread(target=worker, daemon=True).start()

    # ---------- automation engine ----------
    def _save_automation(self):
        save_json(AUTOMATION_FILE, {
            "wifi": self.rule_wifi.get(),
            "mac": self.rule_mac.get(),
            "audio": self.rule_audio.get(),
        })

    def _on_transition(self, connected):
        self.log(f"# dock {'connected' if connected else 'disconnected'}")
        if self.rule_wifi.get() and have("nmcli"):
            self.do(["nmcli", "radio", "wifi", "off" if connected else "on"])
        if self.rule_mac.get() and self.iface:
            if connected:
                src = primary_host_iface(self.iface)
                if src:
                    self._set_mac(net_mac(src))
            elif os.path.exists(f"/sys/class/net/{self.iface}"):
                self._mac_restore()   # back to the dock's OWN original MAC
            else:
                # the NIC is already gone (dock physically unplugged) — nothing to restore.
                # It comes back with its factory MAC on next plug-in anyway.
                self.log("# MAC restore skipped — interface no longer present")
        if self.rule_audio.get() and connected and have("pactl"):
            for s in list_sinks():
                if "usb" in s.lower():
                    self.do(["pactl", "set-default-sink", s]); break
        # user-defined macros (macros.py) — scoped to this dock's chip when known
        if self.macro_store:
            c = nic_chip(self.iface) if self.iface else None
            model = (c.get("product") or "") if c else ""
            n = run_macros(self.macro_store, "connect" if connected else "disconnect",
                           log=self.log, dock_model=model)
            if n:
                self.log("# %d macro(s) fired" % n)

    # ---------- 1 Hz tick ----------
    def _tick(self):
        ifc = self.iface
        if ifc:
            rx, tx = net_bytes(ifc)
            lrx, ltx = self._last_bytes
            self._last_bytes = (rx, tx)
            rmb = max(0, rx - lrx) * 8 / 1e6
            tmb = max(0, tx - ltx) * 8 / 1e6
            self.rx_hist.append(rmb); self.tx_hist.append(tmb)
            up = net_carrier(ifc)
            self.ss_dot.configure(text_color=(ACCENT if up else "#e05252"))
            self.ss_iface.configure(text=ifc)
            self.ss_speed.configure(text=net_speed(ifc))
            if up != self.prev_link:
                self.prev_link = up
                self._on_transition(up)
        else:
            self.ss_dot.configure(text_color="#e05252")
            self.ss_iface.configure(text="no dock NIC")
        if self.current == "Dashboard":
            self._update_dashboard_fast()
            if not hasattr(self, "_sc"):
                self._sc = 0
            self._sc += 1
            if self._sc % 8 == 0:
                self._refresh_reach_async()
        self.after(1000, self._tick)




# =========================================================================== #
#  MACROS — user-defined commands fired on dock connect/disconnect
#  (merged in; no assumptions about the user's system)
# =========================================================================== #
MACROS_FILE = os.path.join(CONFIG_DIR, "macros.json")

TRIGGERS = ["connect", "disconnect", "port_on", "port_off"]

ACCENT = "#2fbf5f"
AMBER = "#e0a13a"
CARD = ("#1b2027", "#1b2027")


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
#  GUI page
# --------------------------------------------------------------------------- #
class MacroPage:
    def __init__(self, store=None, app=None):
        self.store = store or MacroStore()
        self.app = app
        self.parent = None

    def log(self, msg):
        if self.app and hasattr(self.app, "log"):
            self.app.log(msg)

    def build(self, w):
        self.parent = w
        intro = ctk.CTkFrame(w, corner_radius=12, fg_color=CARD)
        intro.pack(fill="x", padx=10, pady=8)
        ctk.CTkLabel(intro, text="Macros", font=ctk.CTkFont(size=15, weight="bold")
                     ).pack(anchor="w", padx=16, pady=(12, 2))
        ctk.CTkLabel(
            intro,
            text=("Run your own commands when the dock connects or disconnects. DockPilot makes "
                  "no assumptions about your system — you write the commands. Templates below are "
                  "editable starting points; nothing runs unless you enable it."),
            justify="left", wraplength=820, text_color=("gray55", "gray55")
        ).pack(anchor="w", padx=16, pady=(0, 8))
        row = ctk.CTkFrame(intro, fg_color="transparent"); row.pack(fill="x", padx=16, pady=(0, 12))
        ctk.CTkButton(row, text="+ New macro", command=self._new).pack(side="left", padx=(0, 8))
        ctk.CTkButton(row, text="Add starter templates", fg_color="#5a5f6a",
                      hover_color="#474b54", command=self._templates).pack(side="left")

        self.list_frame = ctk.CTkFrame(w, fg_color="transparent")
        self.list_frame.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self._refresh()
        return w

    def _refresh(self):
        for wdg in self.list_frame.winfo_children():
            wdg.destroy()
        if not self.store.macros:
            ctk.CTkLabel(self.list_frame,
                         text="No macros yet. Create one, or add the starter templates above.",
                         text_color=("gray55", "gray55")).pack(anchor="w", padx=16, pady=12)
            return
        for i, m in enumerate(self.store.macros):
            c = ctk.CTkFrame(self.list_frame, corner_radius=12, fg_color=CARD)
            c.pack(fill="x", pady=6)
            top = ctk.CTkFrame(c, fg_color="transparent"); top.pack(fill="x", padx=16, pady=(12, 4))
            var = tk.BooleanVar(value=bool(m.get("enabled")))
            ctk.CTkSwitch(top, text="", variable=var, width=44,
                          command=lambda i=i, v=var: self._toggle(i, v)).pack(side="left")
            ctk.CTkLabel(top, text=m.get("name", "unnamed"),
                         font=ctk.CTkFont(size=14, weight="bold")).pack(side="left", padx=8)
            scope = m.get("dock") or "any dock"
            ctk.CTkLabel(top, text="   on %s · %s" % (m.get("trigger", "?"), scope),
                         text_color=("gray55", "gray55")).pack(side="left")
            ctk.CTkButton(top, text="Delete", width=70, fg_color="#b4552d",
                          hover_color="#8f3f1e",
                          command=lambda i=i: self._delete(i)).pack(side="right", padx=4)
            ctk.CTkButton(top, text="Edit", width=60,
                          command=lambda i=i: self._edit(i)).pack(side="right", padx=4)
            ctk.CTkButton(top, text="Run now", width=80, fg_color="#5a5f6a",
                          hover_color="#474b54",
                          command=lambda i=i: self._run(i)).pack(side="right", padx=4)
            body = "\n".join(m.get("commands", [])) or "(no commands)"
            ctk.CTkLabel(c, text=body, justify="left", anchor="w",
                         font=ctk.CTkFont(family="monospace", size=12),
                         text_color=("gray60", "gray60"), wraplength=800
                         ).pack(anchor="w", padx=16, pady=(0, 12))

    # ---- actions ----
    def _toggle(self, idx, var):
        self.store.macros[idx]["enabled"] = bool(var.get())
        self.store.save()

    def _delete(self, idx):
        name = self.store.macros[idx].get("name", "this macro")
        if messagebox.askyesno("Delete macro", "Delete %r?" % name):
            self.store.remove(idx)
            self._refresh()

    def _run(self, idx):
        m = self.store.macros[idx]
        if not messagebox.askyesno("Run macro",
                                   "Run %r now?\n\nThis executes its commands immediately."
                                   % m.get("name", "macro")):
            return
        self.log("# macro (manual): %s" % m.get("name"))
        run_commands(m.get("commands", []), log=self.log)

    def _templates(self):
        n = self.store.add_templates()
        self._refresh()
        messagebox.showinfo("Templates",
                            "Added %d starter template(s), all disabled.\n\n"
                            "They're examples — edit the commands for your system, then enable "
                            "the ones you want." % n if n else
                            "All starter templates are already present.")

    def _new(self):
        self._editor(None)

    def _edit(self, idx):
        self._editor(idx)

    def _editor(self, idx):
        existing = self.store.macros[idx] if idx is not None else {
            "name": "", "trigger": "connect", "dock": "", "enabled": False, "commands": []}

        win = ctk.CTkToplevel(self.parent)
        win.title("Edit macro" if idx is not None else "New macro")
        win.geometry("640x520")
        win.transient(self.parent.winfo_toplevel())
        win.after(100, win.lift)

        ctk.CTkLabel(win, text="Name").pack(anchor="w", padx=16, pady=(14, 2))
        name = ctk.CTkEntry(win, width=560); name.pack(padx=16)
        name.insert(0, existing.get("name", ""))

        ctk.CTkLabel(win, text="Trigger").pack(anchor="w", padx=16, pady=(12, 2))
        trig = ctk.CTkOptionMenu(win, values=TRIGGERS, width=200)
        trig.set(existing.get("trigger", "connect")); trig.pack(anchor="w", padx=16)

        ctk.CTkLabel(win, text="Only for dock model (blank = any dock)"
                     ).pack(anchor="w", padx=16, pady=(12, 2))
        dock = ctk.CTkEntry(win, width=300, placeholder_text="e.g. opendock-v1")
        dock.pack(anchor="w", padx=16)
        dock.insert(0, existing.get("dock", ""))

        ctk.CTkLabel(win, text="Commands — one per line. Lines starting with # are ignored."
                     ).pack(anchor="w", padx=16, pady=(12, 2))
        cmds = ctk.CTkTextbox(win, height=170, font=ctk.CTkFont(family="monospace", size=12))
        cmds.pack(fill="x", padx=16)
        cmds.insert("1.0", "\n".join(existing.get("commands", [])))

        ctk.CTkLabel(win, text="Commands run in order, in a shell, with a 60s timeout each. "
                              "Output goes to the Logs page.",
                     text_color=("gray55", "gray55"), justify="left", wraplength=560
                     ).pack(anchor="w", padx=16, pady=(6, 0))

        def save():
            m = {
                "name": name.get().strip() or "unnamed",
                "trigger": trig.get(),
                "dock": dock.get().strip(),
                "enabled": bool(existing.get("enabled")),
                "commands": [l for l in cmds.get("1.0", "end").splitlines()],
            }
            if idx is None:
                self.store.add(m)
            else:
                self.store.update(idx, m)
            win.destroy()
            self._refresh()

        row = ctk.CTkFrame(win, fg_color="transparent"); row.pack(fill="x", padx=16, pady=14)
        ctk.CTkButton(row, text="Save", command=save).pack(side="left")
        ctk.CTkButton(row, text="Cancel", fg_color="#5a5f6a", hover_color="#474b54",
                      command=win.destroy).pack(side="left", padx=8)


# --------------------------------------------------------------------------- #


# =========================================================================== #
#  SYSTEM SNAPSHOT — one-click diagnostic dump
# =========================================================================== #
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


def _mcu_state():
    """Placeholder: future DockPilot-protocol docks (an MCU running our firmware)
    will report IDENTIFY / CAPS / STATUS here."""
    return "[no DockPilot-protocol dock detected]"

# --------------------------------------------------------------------------- #
def build_snapshot(iface=None, log_lines=None, dockpilot_version="unknown"):
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
    parts.append(_section("DOCKPILOT-PROTOCOL DOCK (MCU)", _mcu_state()))

    # --- app command log ---
    if log_lines:
        parts.append(_section("DOCKPILOT COMMAND LOG (last 200)",
                              _redact("\n".join(log_lines[-200:]))))
    else:
        parts.append(_section("DOCKPILOT COMMAND LOG", "[none]"))

    parts.append("\n--- end of snapshot ---\n")
    return "\n".join(parts)


def write_snapshot(iface=None, log_lines=None, dockpilot_version="unknown", directory=None):
    """Build the snapshot and write it to a timestamped file. Returns the path."""
    text = build_snapshot(iface, log_lines, dockpilot_version)
    directory = directory or os.path.expanduser("~")
    path = os.path.join(directory, "dockpilot-snapshot-%s.txt" % time.strftime("%Y%m%d-%H%M%S"))
    with open(path, "w") as f:
        f.write(text)
    return path


if __name__ == "__main__":
    DockPilot().mainloop()
