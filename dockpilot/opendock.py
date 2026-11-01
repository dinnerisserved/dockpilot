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
}
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
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


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
        text += " (%s)" % {1: "name", 2: "asset tag", 3: "site"}.get(v, v)
    elif ev == "fault":
        text += " (%s)" % FAULT_WHERE.get(v, v)
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

    def __init__(self, config_dir, on_connect=None, on_disconnect=None, on_event=None, on_log=None):
        self.config_dir = config_dir
        self.on_connect, self.on_disconnect = on_connect, on_disconnect
        self.on_event, self.on_log = on_event, on_log
        self.dock = None
        self.store = None
        self.port = None
        self.status = {}
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
        with self._lock:
            self.dock, self.port, self.store, self.error = d, port, store, ""
        if self.on_connect:
            self.on_connect(self)
        self._refresh()

    def _refresh(self):
        try:
            st = self.dock.status()
            with self._lock:
                self.status = st
            new = self.store.collect(self.dock, st)
            if new and self.on_log:
                self.on_log(new)
        except DockError as e:
            self._lost(str(e))

    def _lost(self, why):
        d = self.dock
        with self._lock:
            self.dock, self.port, self.status, self.error = None, None, {}, why
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
        ok = self.dock.set(field, value)
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
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config"),
                    help="where the log copy is kept")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every dock on this machine")
    sub.add_parser("status", help="STATUS as JSON")
    p = sub.add_parser("log", help="new log entries (or --all)")
    p.add_argument("--all", action="store_true", help="everything the dock holds, not just what is new")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("set", help="set name, assettag or site")
    p.add_argument("field", choices=FIELDS)
    p.add_argument("value")
    sub.add_parser("clear", help="CLEAR LOG")
    sub.add_parser("watch", help="print events and a status line as they happen")
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
        elif args.cmd == "set":
            try:
                d.set(args.field, args.value)
                print("OK")
            except DockRefused as e:
                sys.exit("refused: %s" % e.message)
            except DockBusy as e:
                sys.exit("busy: %s" % e)
        elif args.cmd == "clear":
            d.clear_log()
            LogStore(args.config, d.identity["uid"]).forget()
            print("OK")
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
