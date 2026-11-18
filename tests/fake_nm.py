"""A stand-in for NetworkManager, speaking the part of nmcli that DockPilot uses.

Used two ways: as FakeNM().run in place of dockpilot_core.run (unit tests), and as an executable
called `nmcli` on PATH (end-to-end tests) — then the state lives in the JSON file named by
$FAKE_NM_STATE. It behaves like the real one where it matters:
  - `connection show` in terse mode escapes ':' and '\\' in a name;
  - `connection import type wireguard file F` names the connection and the interface after the
    file, refuses an interface name Linux would refuse, and brings the connection UP at once
    (autoconnect is on until someone turns it off — the real nmcli does exactly this);
  - a connection whose name contains "broken" does not come up.
"""
import json
import os
import re
import sys
import uuid as uuidlib


class FakeNM:
    def __init__(self, path=None):
        self.path = path
        self.cons = []          # {"name", "uuid", "type", "active", "autoconnect", "ifname"}
        self.calls = []         # every argv, in order
        self.wifi = "on"
        self.seen_files = []    # (path, mode, text) of every file imported
        if path and os.path.exists(path):
            self._load()

    # -- state on disk, for the executable
    def _load(self):
        d = json.load(open(self.path))
        self.cons, self.calls, self.wifi = d.get("cons", []), d.get("calls", []), d.get("wifi", "on")

    def _save(self):
        if self.path:
            tmp = self.path + ".tmp"
            json.dump({"cons": self.cons, "calls": self.calls, "wifi": self.wifi}, open(tmp, "w"))
            os.replace(tmp, self.path)

    def add(self, name, typ="wireguard", active=False):
        c = {"name": name, "uuid": str(uuidlib.uuid4()), "type": typ, "active": active, "autoconnect": False, "ifname": name[:15]}
        self.cons.append(c)
        self._save()
        return c

    def active(self):
        return sorted(c["name"] for c in self.cons if c["active"])

    def names(self):
        return [c["name"] for c in self.cons]

    def _find(self, kind, value):
        for c in self.cons:
            if (kind == "uuid" and c["uuid"] == value) or (kind == "id" and c["name"] == value):
                return c
        return None

    @staticmethod
    def _esc(s):
        return s.replace("\\", "\\\\").replace(":", "\\:")

    # -- the nmcli command line
    def run(self, args, timeout=25):
        if self.path:
            self._load()
        self.calls.append(list(args))
        rc, out = self._do(list(args))
        self._save()
        return rc, out

    def _do(self, a):
        if a[:1] != ["nmcli"]:
            return 127, "not nmcli"
        a = a[1:]
        terse = False
        fields = None
        while a and a[0].startswith("-"):
            if a[0] == "-t":
                terse = True
                a = a[1:]
            elif a[0] in ("-f", "-g"):
                fields = a[1].split(",")
                a = a[2:]
            else:
                a = a[1:]
        if a[:2] == ["radio", "wifi"]:
            self.wifi = a[2]
            return 0, ""
        if a[:2] == ["connection", "show"]:
            rows = []
            for c in self.cons + [{"name": "Wired connection 1", "uuid": "11111111-1111-1111-1111-111111111111", "type": "802-3-ethernet", "active": True}]:
                vals = {"NAME": c["name"], "UUID": c["uuid"], "TYPE": c["type"], "ACTIVE": "yes" if c["active"] else "no"}
                rows.append(":".join(self._esc(vals[f]) for f in (fields or ["NAME", "UUID", "TYPE"])) if terse else "  ".join(vals[f] for f in (fields or ["NAME", "UUID", "TYPE"])))
            return 0, "\n".join(rows)
        if a[:5] == ["connection", "import", "type", "wireguard", "file"]:
            path = a[5]
            base = os.path.basename(path)
            if not base.endswith(".conf"):
                return 1, "Error: failed to import '%s': The name of the WireGuard config must end with \".conf\"." % path
            ifname = base[:-5]
            if not re.fullmatch(r"[A-Za-z0-9_=+.\-]{1,15}", ifname):
                return 1, "Error: failed to import '%s': The name of the WireGuard config must be a valid interface name followed by \".conf\"." % path
            try:
                text = open(path).read()
                mode = os.stat(path).st_mode & 0o777
            except OSError as e:
                return 1, "Error: failed to load connection: %s" % e
            self.seen_files.append((path, mode, text))
            if "PrivateKey" not in text:
                return 1, "Error: failed to import '%s': Missing or invalid PrivateKey." % path
            if any(c["ifname"] == ifname for c in self.cons):
                pass                                   # the real one adds a second connection on the same interface name; so do we
            c = {"name": ifname, "uuid": str(uuidlib.uuid4()), "type": "wireguard", "active": True, "autoconnect": True, "ifname": ifname}
            self.cons.append(c)
            return 0, "Connection '%s' (%s) successfully added." % (c["name"], c["uuid"])
        if a[:2] == ["connection", "modify"]:
            c = self._find(a[2], a[3])
            if c is None:
                return 10, "Error: unknown connection '%s'." % a[3]
            kv = a[4:]
            for k, v in zip(kv[0::2], kv[1::2]):
                if k == "connection.id":
                    c["name"] = v
                elif k == "connection.autoconnect":
                    c["autoconnect"] = v == "yes"
            return 0, ""
        if a[:2] in (["connection", "up"], ["connection", "down"]):
            c = self._find(a[2], a[3]) if a[2] in ("uuid", "id") else self._find("id", a[2])
            if c is None:
                return 10, "Error: unknown connection '%s'." % a[-1]
            if a[1] == "up":
                if "broken" in c["name"]:
                    return 4, "Error: Connection activation failed: The peer did not answer the handshake"
                c["active"] = True
                return 0, "Connection successfully activated (D-Bus active path: /org/freedesktop/NetworkManager/ActiveConnection/7)"
            if not c["active"]:
                return 10, "Error: '%s' is not an active connection." % c["name"]
            c["active"] = False
            return 0, "Connection '%s' successfully deactivated" % c["name"]
        return 2, "Error: argument not understood by the stand-in: %s" % " ".join(a)


def wg_conf(endpoint="185.213.154.68:51820"):
    return ("[Interface]\nPrivateKey = cE1mU3VwZXJTZWNyZXRLZXlUaGF0U3RheXNPblRoZUxhcHRvcD0=\nAddress = 10.64.0.2/32\nDNS = 10.64.0.1\n\n"
            "[Peer]\nPublicKey = 5JMPeO7gXIbR5CnUa/NPNK4L5GqUnreF0/Bozai4pl4=\nAllowedIPs = 0.0.0.0/0\nEndpoint = %s\n" % endpoint)


if __name__ == "__main__":                       # run as `nmcli`
    nm = FakeNM(os.environ.get("FAKE_NM_STATE") or "/tmp/fake-nm.json")
    rc, out = nm.run(["nmcli"] + sys.argv[1:])
    if out:
        (sys.stdout if rc == 0 else sys.stderr).write(out + "\n")
    sys.exit(rc)
