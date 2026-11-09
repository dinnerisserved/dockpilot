#!/usr/bin/env python3
"""opendock.py — the host side of the DockPilot Dock Protocol v1.

A dock running the OpenDock firmware appears as a USB serial port and speaks a
small line protocol. This module finds such docks, talks to them safely, keeps
an incremental copy of each dock's event log, and gives DockPilot (or any
script) a plain Python object to work with. It is also a command-line tool:

    python3 opendock.py list                    # every dock on the machine
    python3 opendock.py status                  # STATUS of the first dock, as JSON
    python3 opendock.py log [--all]             # new entries since last time (or everything)
    python3 opendock.py set name "Desk 22"      # name | assettag | site
    python3 opendock.py clear                   # hide the log's history
    python3 opendock.py watch                   # print events and status as they happen
    python3 opendock.py desk                    # the desk the dock holds (firmware 0.6): tag, options, keys
    python3 opendock.py get proxy               # one desk option or key, bare — for scripts
    python3 opendock.py export > desk.txt       # the desk as a script; `import desk.txt` sends it back

Needs pyserial (pip install pyserial). Everything else is the standard library.

Rules this module keeps, because the protocol asks hosts to:
  - it never sends anything but IDENTIFY to a port it has not yet recognised,
    and never opens a port at 1200 baud;
  - it sends SET TIME right after every successful IDENTIFY;
  - it parses replies as JSON, not text, and ignores fields it does not know;
  - it treats any line beginning "EVENT " as an event, wherever it appears;
  - when a busy reply says how long to wait, it waits that long and retries
    once, then gives up with the reason;
  - it collects the log by index, and notices from STATUS `log_last` when a
    dock's log has been reset.
"""

import glob
import json
import os
import re
import sys
import threading
import time

try:
    import serial
except ImportError:                      # the GUI shows a hint; scripts get a clear error
    serial = None

PROTO = 1
BAUD = 115200                            # never 1200: that is the bootloader reset convention
LINE_TIMEOUT = 2.0                       # for one reply line
LOG_TIMEOUT = 15.0                       # for a whole GET LOG transfer
BUSY_WAIT_MAX = 130                      # the dock never asks for more than 120 s
MAX_STR = 64                             # bytes per setting, raw and JSON-escaped
LOG_PAGE = 200                           # entries per GET LOG request when collecting
LOG_KEEP = 5000                          # entries kept per dock on disk
FIELDS = ("name", "assettag", "site")

EVENT_NAMES = {
    "boot": "firmware started", "dock": "laptop attached", "undock": "laptop removed",
    "host_connect": "software opened the port", "host_disconnect": "software closed the port",
    "temp_high": "temperature high", "temp_ok": "temperature back to normal",
    "config_change": "setting changed", "time_sync": "clock set", "log_cleared": "log cleared",
    "fault": "firmware fault (carried on)",
    "desk_change": "desk written", "desk_read": "desk handed to a laptop",
    "fw_update": "firmware update",
}
DESK_CHANGE = {1: "tag", 2: "option", 3: "key", 4: "import", 5: "seat", 6: "seat forgotten"}
FW_UPDATE = {1: "new firmware written, restarting into it", 2: "new firmware running"}
OPT_LIMIT = 64
KEY_LIMIT = 128
SNAP_LIMIT = 400
BOOT_REASONS = {0: "unknown", 1: "power on", 2: "brownout", 3: "software reset", 4: "deep-sleep alarm",
                5: "reset pin", 6: "WATCHDOG — the firmware hung and was reset", 7: "rescue/debug",
                8: "re-run without reset (console)"}
FAULT_WHERE = {0: "in a command", 1: "in housekeeping", 2: "at start-up: damage found and repaired"}


# --------------------------------------------------------------------------- errors
class DockError(Exception):
    """Base: anything the dock or the port did that stops a call."""


class DockRefused(DockError):
    """The dock answered ERR badcmd / badarg / range: the request was wrong."""
    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code, self.message = code, message


class DockBusy(DockError):
    """The dock answered ERR busy. `wait` is the seconds it asked for, or None."""
    def __init__(self, message, wait=None):
        super().__init__(message)
        self.wait = wait


class DockGone(DockError):
    """No reply, or the port went away: the dock is not there (any more)."""


# --------------------------------------------------------------------------- helpers
def validate_setting(value):
    """The host-side copy of the dock's rule, so a user gets a sentence instead
    of ERR range. Returns None if fine, else the reason."""
    if not isinstance(value, str):
        return "must be text"
    raw = len(value.encode("utf-8"))
    escaped = raw + sum(1 for ch in value if ch in '"\\')
    if raw > MAX_STR or escaped > MAX_STR:
        return "too long: %d bytes, the dock keeps %d (a quote or backslash counts double)" % (max(raw, escaped), MAX_STR)
    if any(ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in value):
        return "contains control characters, which the dock would strip"
    return None


def quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def unquote(text):
    """The inverse of quote(): the value in a KEY / SET line as EXPORT prints it."""
    text = text.strip()
    if not text.startswith('"'):
        return text
    out, i = [], 1
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] in '"\\n':
            out.append("\n" if text[i + 1] == "n" else text[i + 1])
            i += 2
            continue
        if ch == '"':
            break
        out.append(ch)
        i += 1
    return "".join(out)


def validate_text(value, limit):
    """Length as the dock counts it (raw and escaped), for a value of `limit` bytes."""
    if not isinstance(value, str):
        return "must be text"
    raw = len(value.encode("utf-8"))
    escaped = raw + sum(1 for ch in value if ch in '"\\\n')
    if raw > limit or escaped > limit:
        return "too long: %d bytes, the dock keeps %d" % (max(raw, escaped), limit)
    return None


def _busy_wait(message):
    m = re.search(r"(?:less than|try again in) (\d+)s", message)
    return min(int(m.group(1)), BUSY_WAIT_MAX) if m else None


def candidate_ports():
    """Serial ports worth asking. Ports whose USB strings say OpenDock come first."""
    ports = sorted(set(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/tty.usbmodem*")))
    if serial is None:
        return ports
    try:
        from serial.tools import list_ports
        first = []
        for p in list_ports.comports():
            text = " ".join(str(x) for x in (p.manufacturer, p.product, p.description) if x)
            if "opendock" in text.lower() and p.device in ports:
                first.append(p.device)
        # the data port is the higher-numbered of a dock's two ports; try it first
        first.sort(reverse=True)
        return first + [p for p in ports if p not in first]
    except Exception:
        return ports


def format_time(entry):
    """Human time for a log entry: local wall clock if synced, else 'boot + Ns'."""
    if entry.get("synced") and entry.get("t"):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["t"]))
    return "boot+%ss" % entry.get("up", "?")


def describe(entry):
    """One line of English for a log entry."""
    ev = entry.get("ev", "?")
    text = EVENT_NAMES.get(ev, ev)
    v = entry.get("v")
    if ev == "boot":
        text += " (%s)" % BOOT_REASONS.get(v, v)
    elif ev in ("temp_high", "temp_ok"):
        text += " (%s °C)" % v
    elif ev == "config_change":
        text += " (%s)" % {1: "name", 2: "asset tag", 3: "site", 4: "note 1", 5: "note 2", 6: "note 3", 7: "note 4"}.get(v, v)
    elif ev == "fault":
        text += " (%s)" % FAULT_WHERE.get(v, v)
    elif ev == "desk_change":
        text += " (%s)" % DESK_CHANGE.get(v, v)
    elif ev == "fw_update":
        text += " (%s)" % FW_UPDATE.get(v, v)
    return "%s  #%s  %s" % (format_time(entry), entry.get("i", "?"), text)


# --------------------------------------------------------------------------- the dock
class Dock:
    """One dock on one serial port.

        d = Dock("/dev/ttyACM1", on_event=print)
        d.open()                       # IDENTIFY, SET TIME, CAPS — raises DockGone if not a dock
        d.status()                     # dict
        d.set("name", "Desk 22")       # raises DockRefused / DockBusy
        d.get_log(frm=1)               # list of dicts
        d.close()

    A reader thread takes every line off the port: EVENT lines go to on_event
    (from that thread), everything else is a reply and is handed to whichever
    command is waiting. One command at a time; the lock is inside.
    """

    def __init__(self, port, on_event=None, timeout=LINE_TIMEOUT):
        if serial is None:
            raise DockError("pyserial is not installed (pip install pyserial)")
        self.port = port
        self.on_event = on_event
        self.timeout = timeout
        self.identity = None             # dict after open()
        self.caps = None
        self.ser = None
        self._lock = threading.Lock()
        self._replies = []               # lines that are not events, in order
        self._cond = threading.Condition()
        self._reader = None
        self._alive = False
        self.last_events = []            # the most recent EVENT texts, newest last

    # ---- lifecycle
    def open(self, hello_wait=1.0):
        self.ser = serial.Serial(self.port, BAUD, timeout=0.1)
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        deadline = time.time() + hello_wait                  # the greeting, if it comes
        while time.time() < deadline and not self.last_events:
            time.sleep(0.05)
        self._drain()
        try:
            self.identity = self.identify()
            self.set_time()
            self.caps = self.command("CAPS", json_reply=True)
        except DockError:
            self.close()
            raise
        return self

    def close(self):
        self._alive = False
        s, self.ser = self.ser, None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(1.0)
        self._reader = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()

    # ---- wire
    def _read_loop(self):
        buf = b""
        while self._alive:
            try:
                chunk = self.ser.read(256)
            except Exception:
                break                                        # port gone: commands time out -> DockGone
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").rstrip("\r")
                if not line:
                    continue                                 # a bare terminator: the dock re-syncing
                if line.startswith("EVENT "):
                    text = line[6:]
                    self.last_events = (self.last_events + [text])[-20:]
                    if self.on_event:
                        try:
                            self.on_event(text)
                        except Exception:
                            pass
                    continue
                with self._cond:
                    self._replies.append(line)
                    self._cond.notify_all()
        self._alive = False

    def _drain(self):
        with self._cond:
            self._replies = []

    def _next_line(self, timeout):
        with self._cond:
            deadline = time.time() + timeout
            while not self._replies:
                left = deadline - time.time()
                if left <= 0 or not self._alive:
                    return None
                self._cond.wait(left)
            return self._replies.pop(0)

    def _write(self, line):
        try:
            self.ser.write((line + "\n").encode("utf-8"))
        except Exception as e:
            raise DockGone("port write failed: %s" % e)

    @staticmethod
    def _check(line, cmd):
        if line.startswith("ERR "):
            parts = line.split(" ", 2)
            code = parts[1] if len(parts) > 1 else "?"
            msg = parts[2] if len(parts) > 2 else ""
            if code == "busy":
                raise DockBusy(msg, _busy_wait(msg))
            raise DockRefused(code, msg or cmd)

    def command(self, cmd, json_reply=False, retry_busy=True):
        """Send one command, return its single reply line (or the parsed dict).
        A busy reply that names a wait is waited out once."""
        for attempt in (1, 2):
            with self._lock:
                self._drain()
                self._write(cmd)
                line = self._next_line(self.timeout)
            if line is None:
                raise DockGone("no reply to %s" % cmd.split(" ", 1)[0])
            try:
                self._check(line, cmd)
            except DockBusy as e:
                if retry_busy and e.wait and attempt == 1:
                    time.sleep(e.wait + 0.5)
                    continue
                raise
            if json_reply:
                try:
                    return json.loads(line)
                except ValueError:
                    raise DockGone("reply to %s was not JSON: %r" % (cmd, line[:60]))
            return line

    # ---- commands
    def identify(self):
        """IDENTIFY, tolerant of a stale line before it. Raises DockGone if this is not a dock."""
        with self._lock:
            self._drain()
            self._write("")                                  # a fresh line first, as the spec advises
            self._write("IDENTIFY")
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                line = self._next_line(deadline - time.time())
                if line is None:
                    break
                f = line.split()
                if len(f) == 6 and f[0] == "DOCKPILOT":
                    try:
                        proto = int(f[1])
                    except ValueError:
                        continue
                    return {"proto": proto, "fw": f[2], "model": f[3], "uid": f[4], "serial": f[5]}
        raise DockGone("no DOCKPILOT signature on %s" % self.port)

    def ping(self):
        return self.command("PING") == "PONG"

    def set_time(self, unix=None):
        return self.command("SET TIME %d" % int(unix if unix is not None else time.time())) == "OK"

    def status(self):
        return self.command("STATUS", json_reply=True)

    def has(self, cap):
        """Does this dock advertise a capability (firmware 0.5 and later)?"""
        return bool(self.caps and self.caps.get(cap))

    def health(self):
        """HEALTH: the dock's own verdict, or None on a dock without it."""
        return self.command("HEALTH", json_reply=True) if self.has("health") else None

    def stats(self):
        """STATS: lifetime counters, or None on a dock without them."""
        return self.command("STATS", json_reply=True) if self.has("stats") else None

    def notes(self):
        """GET NOTES: {"1": text, ...}, or {} on a dock without note slots."""
        return self.command("GET NOTES", json_reply=True) if self.has("notes") else {}

    def set_note(self, n, value):
        n = int(n)
        if not (1 <= n <= int(self.caps.get("notes", 0) if self.caps else 0)):
            raise DockRefused("badarg", "no note slot %d" % n)
        why = validate_setting(value)
        if why:
            raise DockRefused("range", why)
        return self.command("SET NOTE %d %s" % (n, quote(value))) == "OK"

    def set(self, field, value):
        """SET NAME / ASSETTAG / SITE. Validates first; raises DockRefused or DockBusy."""
        field = field.lower()
        if field not in FIELDS:
            raise DockRefused("badcmd", "unknown field %s" % field)
        why = validate_setting(value)
        if why:
            raise DockRefused("range", why)
        return self.command("SET %s %s" % (field.upper(), quote(value))) == "OK"

    def clear_log(self):
        return self.command("CLEAR LOG") == "OK"

    # ---- firmware 0.6: the desk lives in the dock
    def has_desk(self):
        return bool(self.caps and isinstance(self.caps.get("desk"), dict))

    def _lines(self, cmd, timeout=LOG_TIMEOUT):
        """A command answered by several lines and END. Returns the lines."""
        out = []
        with self._lock:
            self._drain()
            self._write(cmd)
            deadline = time.time() + timeout
            while True:
                line = self._next_line(max(0.0, deadline - time.time()))
                if line is None:
                    raise DockGone("%s did not finish" % cmd.split(" ", 1)[0])
                if line == "END":
                    return out
                self._check(line, cmd)
                out.append(line)

    def _json_lines(self, cmd):
        items = []
        for line in self._lines(cmd):
            try:
                items.append(json.loads(line))
            except ValueError:
                continue
        return items

    def get_desk(self):
        return self.command("GET DESK", json_reply=True)

    def get_schema(self):
        return self._json_lines("GET SCHEMA")

    def get_rules(self):
        return self._json_lines("GET RULES")

    def get_keys(self):
        """{name: value}"""
        keys = {}
        for line in self._lines("GET KEYS"):
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[0] == "KEY":
                keys[parts[1]] = unquote(parts[2])
        return keys

    def set_tag(self, value):
        why = validate_setting(value)
        if why:
            raise DockRefused("range", why)
        return self.command("SET TAG %s" % quote(value)) == "OK"

    def set_opt(self, oid, value):
        why = validate_text(value, OPT_LIMIT)
        if why:
            raise DockRefused("range", why)
        return self.command("SET OPT %s %s" % (oid, quote(value))) == "OK"

    def set_key(self, name, value):
        why = validate_text(value, KEY_LIMIT)
        if why:
            raise DockRefused("range", why)
        return self.command("SET KEY %s %s" % (name, quote(value))) == "OK"

    def del_key(self, name):
        return self.command("DEL KEY %s" % name) == "OK"

    def export(self):
        """The desk as the SET lines the dock prints (a script you can send back)."""
        return self._lines("EXPORT")

    def import_lines(self, lines):
        """Send a saved desk back: IMPORT, the lines, IMPORT END — one write on the dock.
        Only SET TAG / SET OPT / SET KEY / DEL KEY lines are sent; anything else is skipped."""
        if self.command("IMPORT") != "OK":
            return False
        try:
            for line in lines:
                t = line.strip()
                u = t.upper()
                if not t or t.startswith("#"):
                    continue
                if not (u.startswith("SET TAG ") or u.startswith("SET OPT ") or u.startswith("SET KEY ") or u.startswith("DEL KEY ")):
                    continue
                self.command(t, retry_busy=False)
        except DockError:
            try:
                self.command("IMPORT ABORT")
            except DockError:
                pass
            raise
        return self.command("IMPORT END") == "OK"

    def seat(self, seat_id):
        return self.command("SEAT %s" % (seat_id or "NONE")) == "OK"

    def get_seat(self):
        return self.command("GET SEAT", json_reply=True)

    def set_rule(self, rid, on):
        return self.command("SET RULE %s %d" % (rid, 1 if on else 0)) == "OK"

    def set_cmd(self, when, lines):
        text = "\n".join(l for l in lines if l.strip())
        return self.command("SET CMD %s %s" % (when, quote(text))) == "OK"

    def set_snap(self, text):
        why = validate_text(text, SNAP_LIMIT)
        if why:
            raise DockRefused("range", why)
        return self.command("SET SNAP %s" % quote(text)) == "OK"

    def set_autosave(self, on):
        return self.command("SET AUTOSAVE %d" % (1 if on else 0)) == "OK"

    def forget_seat(self):
        return self.command("FORGET SEAT") == "OK"

    def get_log(self, frm=1, mx=None):
        """GET LOG: a list of entry dicts. An abandoned transfer (no END) raises DockGone."""
        cmd = "GET LOG %d" % frm + (" %d" % mx if mx is not None else "")
        out = []
        with self._lock:
            self._drain()
            self._write(cmd)
            deadline = time.time() + LOG_TIMEOUT
            while True:
                line = self._next_line(max(0.0, deadline - time.time()))
                if line is None:
                    raise DockGone("GET LOG did not finish")
                if line == "END":
                    return out
                self._check(line, cmd)                       # a single ERR line, no END
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue                                 # a cut line from an earlier stall


# --------------------------------------------------------------------------- discovery
# ---------------------------------------------------------------- firmware files and the web face
UF2_MAGIC0, UF2_MAGIC1 = 0x0A324655, 0x9E5D5157
UF2_FAMILY_RP2350_ARM_S, UF2_FAMILY_ABSOLUTE = 0xE48BFF59, 0xE48BFF57
WEB_HEADER = {"X-OpenDock": "1"}


def check_uf2(path):
    """A firmware file the dock would accept? Returns (ok, message). Only the first block is looked at;
    the dock checks every block again."""
    import struct
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError as ex:
        return False, "couldn't read it: %s" % ex
    if size == 0 or size % 512 or len(head) < 32:
        return False, "not a firmware file (.uf2)"
    m0, m1, flags, addr, blen, no, total, family = struct.unpack("<8I", head)
    if (m0, m1) != (UF2_MAGIC0, UF2_MAGIC1):
        return False, "not a firmware file (.uf2)"
    if family == UF2_FAMILY_ABSOLUTE and addr == 0x10FFFF00 and (flags & 0x8000) and size >= 1024:
        # picotool's first block in every RP2350 file is an "ignore me" marker; the image starts at block 1
        with open(path, "rb") as f:
            f.seek(512)
            head = f.read(32)
        m0, m1, flags, addr, blen, no, total, family = struct.unpack("<8I", head)
    if family == UF2_FAMILY_ABSOLUTE:
        return False, "that is the installer file, for a dock with no firmware yet; use opendock-dev.uf2 or opendock-carrier.uf2"
    if family != UF2_FAMILY_RP2350_ARM_S:
        return False, "that file is not for this dock's chip"
    return True, "%d blocks, %d KB" % (total, size // 1024)


def update_firmware(web, path, timeout=180):
    """POST a .uf2 to the dock's web face (web = the CAPS "web" dict). Returns the dock's reply line;
    raises DockRefused with the dock's reason, DockGone when nothing answered."""
    import urllib.error
    import urllib.request
    url = "http://%s/update" % (web.get("ip") or web.get("host"))
    with open(path, "rb") as f:
        body = f.read()
    req = urllib.request.Request(url, data=body, method="POST", headers=dict(WEB_HEADER, **{"Content-Type": "application/octet-stream"}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            line = r.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        line = e.read().decode("utf-8", "replace").strip()
    except Exception as ex:
        raise DockGone("the dock's page did not answer at %s (%s)" % (url, ex))
    if not line.startswith("OK"):
        m = re.match(r"^ERR (\S+) ?(.*)$", line)
        raise DockRefused(m.group(1) if m else "http", (m.group(2) if m else line) or "the dock refused the file")
    return line


def find_docks(on_progress=None):
    """Every port that answers IDENTIFY with the signature. Returns a list of
    (port, identity) — identity is the dict Dock.identify() returns. Ports that
    cannot be opened are skipped; a permission problem is reported once through
    on_progress if given."""
    found = []
    denied = False
    for port in candidate_ports():
        try:
            d = Dock(port, timeout=1.0)
            try:
                d.ser = serial.Serial(port, BAUD, timeout=0.1)
            except serial.SerialException as e:
                if "ermission" in str(e):
                    denied = True
                continue
            d._alive = True
            d._reader = threading.Thread(target=d._read_loop, daemon=True)
            d._reader.start()
            time.sleep(0.3)                                  # let a greeting land, then ignore it
            try:
                ident = d.identify()
            except DockError:
                continue
            finally:
                d.close()
            if ident["proto"] >= PROTO:
                found.append((port, ident))
        except Exception:
            continue
    if denied and on_progress:
        on_progress("a serial port could not be opened (permission) — is your user in the dialout group?")
    return found


# --------------------------------------------------------------------------- log store
class LogStore:
    """The host's copy of one dock's log, kept in a JSON file keyed by the dock's
    chip id. collect() asks only for what is new, and starts again from 1 when
    STATUS shows the dock's log was reset."""

    def __init__(self, directory, uid):
        self.path = os.path.join(directory, "opendock_log_%s.json" % uid)
        self.uid = uid
        self.last_seen = 0
        self.entries = []
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            self.last_seen = int(d.get("last_seen", 0))
            self.entries = list(d.get("entries", []))
        except Exception:
            self.last_seen, self.entries = 0, []

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"uid": self.uid, "last_seen": self.last_seen,
                           "entries": self.entries[-LOG_KEEP:]}, f)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def collect(self, dock, status=None):
        """Fetch everything newer than last_seen. Returns the new entries."""
        st = status if status is not None else dock.status()
        log_last = int(st.get("log_last", 0) or 0)
        if log_last < self.last_seen:                        # the dock's log was reset
            self.last_seen = 0
        if log_last <= self.last_seen:
            return []
        new = []
        frm = self.last_seen + 1
        while True:
            page = dock.get_log(frm, LOG_PAGE)
            new.extend(page)
            if len(page) < LOG_PAGE:
                break
            frm = page[-1]["i"] + 1
        if new:
            self.entries = (self.entries + new)[-LOG_KEEP:]
            self.last_seen = max(self.last_seen, max(e["i"] for e in new))
            self.save()
        return new

    def forget(self):
        self.last_seen, self.entries = 0, []
        self.save()


# --------------------------------------------------------------------------- link manager
class DockLink:
    """Keeps a dock connected for a long-running program (DockPilot): finds it,
    opens it, polls STATUS, collects the log, notices when it goes away and
    comes back. Call poll() from a worker thread every second or two; read
    snapshot() from anywhere.

    Callbacks (all from the polling thread): on_connect(link), on_disconnect(link),
    on_event(text), on_log(entries)."""

    RESCAN_S = 5.0

    def __init__(self, config_dir, on_connect=None, on_disconnect=None, on_event=None, on_log=None, seat_id=None):
        self.config_dir = config_dir
        self.on_connect, self.on_disconnect = on_connect, on_disconnect
        self.on_event, self.on_log = on_event, on_log
        self.seat_id = seat_id           # 16 hex chars this laptop derives from its key, or None
        self.dock = None
        self.store = None
        self.port = None
        self.status = {}
        self.health = None
        self.stats = None
        self.notes = {}
        self.desk = None                 # GET DESK, on a dock that has one
        self.schema = []                 # GET SCHEMA
        self.rules = []                  # GET RULES
        self.keys = {}                   # GET KEYS
        self.seat = None                 # GET SEAT
        self._polls = 0
        self.error = ""
        self._last_scan = 0.0
        self._lock = threading.Lock()
        self._busy = False

    @property
    def connected(self):
        return self.dock is not None

    def snapshot(self):
        with self._lock:
            return {
                "connected": self.dock is not None,
                "port": self.port,
                "identity": dict(self.dock.identity) if self.dock else None,
                "caps": dict(self.dock.caps) if self.dock and self.dock.caps else None,
                "status": dict(self.status),
                "health": dict(self.health) if self.health else None,
                "stats": dict(self.stats) if self.stats else None,
                "notes": dict(self.notes),
                "desk": dict(self.desk) if self.desk else None,
                "schema": list(self.schema),
                "rules": list(self.rules),
                "keys": dict(self.keys),
                "seat": dict(self.seat) if self.seat else None,
                "events": list(self.dock.last_events) if self.dock else [],
                "log": list(self.store.entries[-200:]) if self.store else [],
                "log_count": len(self.store.entries) if self.store else 0,
                "error": self.error,
            }

    def poll(self):
        """One step: connect if needed, otherwise refresh STATUS and the log."""
        if self._busy:
            return
        self._busy = True
        try:
            if self.dock is None:
                self._try_connect()
            else:
                self._refresh()
        finally:
            self._busy = False

    def _try_connect(self):
        now = time.time()
        if now - self._last_scan < self.RESCAN_S:
            return
        self._last_scan = now
        msgs = []
        docks = find_docks(on_progress=msgs.append)
        if msgs:
            with self._lock:
                self.error = msgs[0]
        if not docks:
            return
        port, _ = docks[0]
        try:
            d = Dock(port, on_event=self._event)
            d.open()
        except DockError as e:
            with self._lock:
                self.error = str(e)
            return
        store = LogStore(self.config_dir, d.identity["uid"])
        try:
            h, sx, notes = d.health(), d.stats(), d.notes()
        except DockError:
            h, sx, notes = None, None, {}
        with self._lock:
            self.dock, self.port, self.store, self.error = d, port, store, ""
            self.health, self.stats, self.notes, self._polls = h, sx, notes or {}, 0
            self.desk, self.schema, self.rules, self.keys, self.seat = None, [], [], {}, None
        self._load_desk(full=True)
        if self.on_connect:
            self.on_connect(self)
        self._refresh()

    def _load_desk(self, full=False):
        """What the dock holds (firmware 0.6): the desk, and this laptop's seat."""
        d = self.dock
        if d is None or not d.has_desk():
            return
        try:
            if full:
                schema, rules = d.get_schema(), d.get_rules()
                if self.seat_id:
                    d.seat(self.seat_id)
            desk, keys = d.get_desk(), d.get_keys()
            seat = d.get_seat() if self.seat_id else None
        except DockError:
            return
        with self._lock:
            if full:
                self.schema, self.rules = schema, rules
            self.desk, self.keys, self.seat = desk, keys, seat

    def _refresh(self):
        try:
            st = self.dock.status()
            self._polls += 1
            h = sx = None
            if self._polls % 5 == 1:                      # every ~10 s: cheap, and nothing to write
                h, sx = self.dock.health(), self.dock.stats()
            if self._polls % 15 == 2:                     # every ~30 s: another laptop may have written
                self._load_desk()
            with self._lock:
                self.status = st
                if h is not None:
                    self.health = h
                if sx is not None:
                    self.stats = sx
            new = self.store.collect(self.dock, st)
            if new and self.on_log:
                self.on_log(new)
        except DockError as e:
            self._lost(str(e))

    def _lost(self, why):
        d = self.dock
        with self._lock:
            self.dock, self.port, self.status, self.error = None, None, {}, why
            self.health, self.stats, self.notes = None, None, {}
            self.desk, self.schema, self.rules, self.keys, self.seat = None, [], [], {}, None
        if d is not None:
            d.close()
        if self.on_disconnect:
            self.on_disconnect(self)

    def _event(self, text):
        if text == "hello":
            return
        if self.on_event:
            self.on_event(text)

    # -- actions for the GUI (call from a worker thread)
    def set(self, field, value):
        if self.dock is None:
            raise DockGone("no dock connected")
        if field.startswith("note") and field[4:].isdigit():
            ok = self.dock.set_note(int(field[4:]), value)
            notes = self.dock.notes()
            with self._lock:
                self.notes = notes or {}
        else:
            ok = self.dock.set(field, value)
        self._refresh()
        return ok

    def desk_action(self, name, *args):
        """One of the Dock's desk calls by name, then re-read what the dock holds."""
        if self.dock is None:
            raise DockGone("no dock connected")
        ok = getattr(self.dock, name)(*args)
        self._load_desk()
        self._refresh()
        return ok

    def clear_log(self):
        if self.dock is None:
            raise DockGone("no dock connected")
        ok = self.dock.clear_log()
        if ok and self.store:
            self.store.forget()
        self._refresh()
        return ok

    def close(self):
        if self.dock is not None:
            self._lost("closed")


# --------------------------------------------------------------------------- CLI
def _first_dock(args):
    if args.port:
        return Dock(args.port).open()
    docks = find_docks(on_progress=lambda m: print("note:", m, file=sys.stderr))
    if not docks:
        sys.exit("no dock found (is it plugged in? try --port /dev/ttyACM1)")
    return Dock(docks[0][0]).open()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Talk to an OpenDock over the DockPilot Dock Protocol v1.")
    ap.add_argument("--port", help="serial port (default: find it)")
    ap.add_argument("--config", default=os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"), "dockpilot"),
                    help="where the log copy is kept (the same folder DockPilot uses)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every dock on this machine")
    sub.add_parser("status", help="STATUS as JSON")
    p = sub.add_parser("log", help="new log entries (or --all)")
    p.add_argument("--all", action="store_true", help="everything the dock holds, not just what is new")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("set", help="set name, assettag or site — or a key: `set room 4.12` (firmware 0.6)")
    p.add_argument("field", help="name, assettag, site, or a key name (a-z, 0-9, _)")
    p.add_argument("value", help="the value; an empty value removes a key")
    p = sub.add_parser("del", help="remove a key")
    p.add_argument("name")
    sub.add_parser("keys", help="every key the dock holds, name=value per line")
    sub.add_parser("clear", help="CLEAR LOG")
    sub.add_parser("watch", help="print events and a status line as they happen")
    sub.add_parser("desk", help="the desk the dock holds: tag, options, keys (firmware 0.6)")
    p = sub.add_parser("get", help="one desk option or key, printed bare (empty if unset)")
    p.add_argument("name")
    sub.add_parser("export", help="the desk as SET lines, to stdout")
    p = sub.add_parser("import", help="send a saved desk back (one write on the dock)")
    p.add_argument("file")
    args = ap.parse_args(argv)

    if serial is None:
        sys.exit("pyserial is not installed: pip install pyserial")

    if args.cmd == "list":
        docks = find_docks(on_progress=lambda m: print("note:", m, file=sys.stderr))
        if not docks:
            print("no docks found")
        for port, ident in docks:
            print("%-16s %s  serial %s  fw %s  chip %s" % (port, ident["model"], ident["serial"], ident["fw"], ident["uid"]))
        return

    d = _first_dock(args)
    try:
        if args.cmd == "status":
            print(json.dumps(d.status(), indent=2, sort_keys=True))
        elif args.cmd == "log":
            if args.all:
                entries = d.get_log(1)
            else:
                store = LogStore(args.config, d.identity["uid"])
                entries = store.collect(d)
                if not entries:
                    print("nothing new since index %d" % store.last_seen)
            for e in entries:
                print(json.dumps(e) if args.json else describe(e))
        elif args.cmd in ("set", "del", "keys"):
            try:
                if args.cmd == "set" and args.field in FIELDS:
                    d.set(args.field, args.value)
                    print("OK")
                else:
                    if not d.has_desk():
                        sys.exit("this dock's firmware (%s) has no keys; 0.6 or later does" % d.identity["fw"])
                    if args.cmd == "keys":
                        for k, v in sorted(d.get_keys().items()):
                            print("%s=%s" % (k, v))
                    elif args.cmd == "del" or args.value == "":
                        print("OK" if d.del_key(args.name if args.cmd == "del" else args.field) else "failed")
                    else:
                        print("OK" if d.set_key(args.field, args.value) else "failed")
            except DockRefused as e:
                sys.exit("refused: %s" % e.message)
            except DockBusy as e:
                sys.exit("busy: %s" % e)
        elif args.cmd == "clear":
            d.clear_log()
            LogStore(args.config, d.identity["uid"]).forget()
            print("OK")
        elif args.cmd in ("desk", "get", "export", "import"):
            if not d.has_desk():
                sys.exit("this dock's firmware (%s) has no desk; 0.6 or later does" % d.identity["fw"])
            if args.cmd == "desk":
                desk = d.get_desk()
                print("tag:", desk.get("tag", ""))
                for o in d.get_schema():
                    print("%-14s %s" % (o["label"] + ":", desk["opt"].get(o["id"], "")))
                for k, v in sorted(d.get_keys().items()):
                    print("%-14s %s" % (k + ":", v))
            elif args.cmd == "get":
                desk = d.get_desk()
                name = args.name.lower()
                if name == "tag":
                    print(desk.get("tag", ""))
                elif name in desk.get("opt", {}):
                    print(desk["opt"][name])
                else:
                    print(d.get_keys().get(name, ""))
            elif args.cmd == "export":
                for line in d.export():
                    print(line)
            else:
                with open(args.file) as f:
                    lines = f.read().splitlines()
                try:
                    print("OK" if d.import_lines(lines) else "failed")
                except DockRefused as e:
                    sys.exit("refused: %s" % e.message)
                except DockBusy as e:
                    sys.exit("busy: %s" % e)
        elif args.cmd == "watch":
            d.on_event = lambda t: print(time.strftime("%H:%M:%S"), "EVENT", t, flush=True)
            print("watching %s (%s) — Ctrl-C to stop" % (d.identity["serial"], d.port))
            try:
                while True:
                    s = d.status()
                    print(time.strftime("%H:%M:%S"), "docked=%s temp=%s°C entries=%s last=%s" % (
                        s.get("docked"), s.get("temp_c"), s.get("log_entries"), s.get("log_last")), flush=True)
                    time.sleep(5)
            except KeyboardInterrupt:
                pass
    finally:
        d.close()


if __name__ == "__main__":
    main()
