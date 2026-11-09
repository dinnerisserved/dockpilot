#!/usr/bin/env python3
"""
DockPilot — see what your USB-C dock is doing and tell it what to do.

This file is the window: GTK 4 and libadwaita, so it looks and behaves like Ubuntu's own
Settings app. All the logic lives in dockpilot_core.py (which, run on its own, is the
background service). The only thing that ever runs as root is dockpilot_helper.py.
opendock.py is the protocol client for docks with the OpenDock management board.

Run:  python3 dockpilot.py        (the .deb installs it as `dockpilot`)
"""
import json
import os
import re
import sys
import threading
import time

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402
except (ImportError, ValueError) as _e:          # --version / --helper-check still work without GTK
    if sys.argv[1:2] and sys.argv[1] in ("--version", "--helper-check"):
        Adw = Gio = GLib = Gtk = None
    else:
        sys.exit("DockPilot needs GTK 4 and libadwaita:\n  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1\n(%s)" % _e)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dockpilot_core as core          # noqa: E402

APP_ID = "io.opendock.DockPilot"
PAGES = [("Dashboard", "view-grid-symbolic"), ("Network", "network-wired-symbolic"),
         ("Native Mode", "emblem-system-symbolic"), ("USB & Power", "drive-removable-media-symbolic"),
         ("Topology", "view-list-symbolic"), ("Audio", "audio-speakers-symbolic"),
         ("Automation", "preferences-other-symbolic"), ("Logs", "text-x-generic-symbolic"),
         ("OpenDock", "network-server-symbolic")]
LEVEL_CSS = {"ok": "success", "bad": "error", "busy": "warning", "muted": "dim-label"}


# --------------------------------------------------------------------------- small helpers
def row(title, subtitle="", prop=True):
    r = Adw.ActionRow()
    r.set_title(GLib.markup_escape_text(str(title)))
    if subtitle:
        r.set_subtitle(GLib.markup_escape_text(str(subtitle)))
        r.set_subtitle_selectable(True)
    if prop:
        r.add_css_class("property")
    return r


def button(label, cb=None, kind=None, tooltip=None):
    b = Gtk.Button(label=label)
    b.set_valign(Gtk.Align.CENTER)
    if kind:
        b.add_css_class(kind)
    if tooltip:
        b.set_tooltip_text(tooltip)
    if cb:
        b.connect("clicked", lambda *_: cb())
    return b


def button_row(title, subtitle, *buttons):
    r = Adw.ActionRow()
    r.set_title(title)
    if subtitle:
        r.set_subtitle(subtitle)
    for b in buttons:
        r.add_suffix(b)
    return r


def group(title, description=""):
    g = Adw.PreferencesGroup()
    g.set_title(title)
    if description:
        g.set_description(description)
    return g


def mono(text):
    l = Gtk.Label(label=text, xalign=0, selectable=True, wrap=True)
    l.add_css_class("monospace")
    l.set_margin_top(6)
    l.set_margin_bottom(6)
    l.set_margin_start(12)
    l.set_margin_end(12)
    return l


class Window(Adw.ApplicationWindow):
    def __init__(self, app, engine, use_service):
        super().__init__(application=app, title="DockPilot")
        self.set_default_size(1040, 720)
        self.set_size_request(820, 560)
        self.engine = engine
        self.use_service = use_service
        self.page_name = None
        self.widgets = {}                         # per-page live widgets
        self.service_log = []
        self._svc_poll = 0

        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView()
        self.split.set_min_sidebar_width(200)
        self.split.set_max_sidebar_width(240)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        # sidebar
        side = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_show_title(True)
        side.add_top_bar(hb)
        self.nav = Gtk.ListBox()
        self.nav.add_css_class("navigation-sidebar")
        for name, icon in PAGES:
            r = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6, margin_start=4)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(Gtk.Label(label=name, xalign=0))
            r.set_child(box)
            r.page_name = name
            self.nav.append(r)
        self.nav.connect("row-selected", lambda lb, r: self.show(r.page_name) if r else None)
        sbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sbox.append(self.nav)
        self.nav.set_vexpand(True)
        self.strip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=14, margin_end=10, margin_bottom=12, spacing=2)
        self.strip_link = Gtk.Label(xalign=0)
        self.strip_link.add_css_class("caption")
        self.strip_od = Gtk.Label(xalign=0)
        self.strip_od.add_css_class("caption")
        self.strip.append(self.strip_link)
        self.strip.append(self.strip_od)
        sbox.append(self.strip)
        side.set_content(sbox)
        self.split.set_sidebar(Adw.NavigationPage.new(side, "DockPilot"))

        # content
        self.content_view = Adw.ToolbarView()
        self.content_hb = Adw.HeaderBar()
        self.content_view.add_top_bar(self.content_hb)
        self.content_page = Adw.NavigationPage.new(self.content_view, "Dashboard")
        self.split.set_content(self.content_page)

        engine.on("log", self._on_log)
        engine.on("native", self._on_native_status)
        engine.on("opendock", self._on_opendock_evt)
        engine.on("transition", lambda up: self._refresh_if("Network"))
        self.nav.select_row(self.nav.get_row_at_index(0))
        GLib.timeout_add(1000, self._tick)

    # ---- plumbing ---------------------------------------------------------------
    def toast(self, text, timeout=3):
        t = Adw.Toast.new(text)
        t.set_timeout(timeout)
        self.toasts.add_toast(t)

    def confirm(self, heading, body, ok_label, cb, destructive=False):
        d = Adw.AlertDialog.new(heading, body)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", ok_label)
        d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("ok")
        d.set_close_response("cancel")
        d.connect("response", lambda dlg, resp: cb() if resp == "ok" else None)
        d.present(self)

    def info(self, heading, body):
        d = Adw.AlertDialog.new(heading, body)
        d.add_response("ok", "OK")
        d.present(self)

    def show(self, name):
        self.page_name = name
        self.widgets = {}
        self.content_page.set_title(name)
        self.content_hb.set_title_widget(None)
        page = Adw.PreferencesPage()
        builder = {"Dashboard": self.page_dashboard, "Network": self.page_network, "Native Mode": self.page_native,
                   "USB & Power": self.page_usb, "Topology": self.page_topology, "Audio": self.page_audio,
                   "Automation": self.page_automation, "Logs": self.page_logs, "OpenDock": self.page_opendock}[name]
        self._od_direct = False
        builder(page)
        if not self._od_direct:
            self.content_view.set_content(page)

    def _refresh_if(self, name):
        if self.page_name == name:
            self.show(name)

    def _tick(self):
        e = self.engine
        e.tick()
        ifc = e.iface
        h = e.health()
        if ifc and h:
            self.strip_link.set_markup("%s %s · %s" % ("🟢" if h["carrier"] else "🔴", GLib.markup_escape_text(ifc), GLib.markup_escape_text(h["speed"] or "")))
        else:
            self.strip_link.set_text("no dock NIC")
        if self.use_service:
            self._svc_poll += 1
            if self._svc_poll % 2 == 0:
                r = core.ask("status", timeout=1.5)
                if r and r.get("ok"):
                    was = (e.od_snap or {}).get("connected")
                    e.od_snap = r.get("opendock") or {}
                    self.service_log = r.get("log") or []
                    if was != e.od_snap.get("connected"):
                        self._refresh_if("OpenDock")
                else:
                    self.use_service = False
                    self.toast("Background service stopped — running standalone")
        snap = e.od_snap or {}
        i = snap.get("identity") or {}
        self.strip_od.set_text(("⛁ OpenDock %s" % i.get("serial")) if snap.get("connected") else "")
        if self.page_name == "Dashboard":
            self._dashboard_tick()
        elif self.page_name == "OpenDock":
            self._opendock_tick()
        elif self.page_name == "Native Mode":
            self._native_tick()
        return True

    def _on_opendock_evt(self, what):
        self._refresh_if("OpenDock")

    def _on_log(self, line):
        tv = self.widgets.get("logview")
        if tv:
            buf = tv.get_buffer()
            buf.insert(buf.get_end_iter(), line + "\n")
            tv.scroll_to_mark(buf.get_insert(), 0, False, 0, 0)

    def _on_native_status(self, text, level):
        r = self.widgets.get("native_status")
        if r:
            self._set_status_row(r, text, level)

    def _set_status_row(self, r, text, level):
        for c in LEVEL_CSS.values():
            r.remove_css_class(c)
        r.set_subtitle(GLib.markup_escape_text(text or "—"))
        r.add_css_class(LEVEL_CSS.get(level, "dim-label"))

    # ---- Dashboard ----------------------------------------------------------------
    def page_dashboard(self, page):
        e = self.engine
        g = group("Dock")
        self.widgets["identity"] = g
        for k, v in e.identity_lines():
            g.add(row(k, v))
        page.add(g)

        g2 = group("The chip", "What is inside, as precisely as the USB descriptors, the driver and the kernel log allow.")
        page.add(g2)
        self.widgets["chip"] = g2
        g2.add(row("Chip", "reading…"))

        def fill_chip():
            rows = e.chip_lines()
            GLib.idle_add(lambda: self._replace_rows(g2, rows))
        threading.Thread(target=fill_chip, daemon=True).start()

        g3 = group("Link health")
        self.widgets["health"] = {"errors": row("Errors", "—"), "drops": row("Drops", "—"), "carrier": row("Carrier", "—")}
        for r in self.widgets["health"].values():
            g3.add(r)
        page.add(g3)

        g4 = group("Throughput", "Live rate through the dock NIC. 0.0 Mb/s is an idle link; the totals prove it is counting.")
        area = Gtk.DrawingArea()
        area.set_content_height(170)
        area.set_draw_func(self._draw_spark)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(area)
        self.widgets["spark"] = area
        self.widgets["totals"] = row("Session totals", "—")
        g4.add(box)
        g4.add(self.widgets["totals"])
        page.add(g4)

        g5 = group("Reachability")
        self.widgets["reach"] = {k: row(t, "measuring…") for k, t in (("gw", "Gateway"), ("net", "Internet (1.1.1.1)"), ("dns", "DNS"), ("pub", "Public IP"), ("lan", "LAN devices seen"))}
        for r in self.widgets["reach"].values():
            g5.add(r)
        page.add(g5)
        e.refresh_reach(self._apply_reach)
        self._dashboard_tick()

    def _replace_rows(self, g, rows):
        # PreferencesGroup has no clear(); rebuild by removing known children
        # walk to the listbox inside and remove rows
        lb = self._find_listbox(g)
        if lb:
            r = lb.get_first_child()
            while r:
                nxt = r.get_next_sibling()
                lb.remove(r)
                r = nxt
        for k, v in rows:
            g.add(row(k, v))

    @staticmethod
    def _find_listbox(w):
        if isinstance(w, Gtk.ListBox):
            return w
        c = w.get_first_child()
        while c:
            f = Window._find_listbox(c)
            if f:
                return f
            c = c.get_next_sibling()
        return None

    def _dashboard_tick(self):
        e = self.engine
        h = e.health()
        hw = self.widgets.get("health")
        if hw and h:
            hw["errors"].set_subtitle(str(h["errors"]))
            hw["drops"].set_subtitle(str(h["drops"]))
            hw["carrier"].set_subtitle("up" if h["carrier"] else "down")
            t = self.widgets.get("totals")
            if t:
                t.set_subtitle("↓ %s   ↑ %s" % (core.human(h["rx"]), core.human(h["tx"])))
        sp = self.widgets.get("spark")
        if sp:
            sp.queue_draw()
        g = self.widgets.get("identity")
        if g and e.iface:
            self._replace_rows(g, e.identity_lines())
        if not hasattr(self, "_rc"):
            self._rc = 0
        self._rc += 1
        if self._rc % 8 == 0:
            e.refresh_reach(self._apply_reach)

    def _apply_reach(self, d):
        rw = self.widgets.get("reach")
        if not rw or self.page_name != "Dashboard":
            return

        def ms(v):
            return "%.0f ms" % v if v is not None else "—"
        rw["gw"].set_subtitle(ms(d["gw"]))
        rw["net"].set_subtitle(ms(d["net"]))
        rw["dns"].set_subtitle(ms(d["dns"]))
        rw["pub"].set_subtitle(d["pub"] or "offline / blocked")
        rw["lan"].set_subtitle(str(d["lan"]))

    def _draw_spark(self, area, cr, W, H):
        e = self.engine
        col = area.get_color()
        L, TOP, BOT = 56, 18, 8
        peak = max([1.0] + list(e.rx_hist) + list(e.tx_hist))
        cr.set_source_rgba(col.red, col.green, col.blue, 0.15)
        cr.set_line_width(1)
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = H - BOT - frac * (H - TOP - BOT)
            cr.move_to(L, y)
            cr.line_to(W - 8, y)
            cr.stroke()
            cr.set_source_rgba(col.red, col.green, col.blue, 0.6)
            cr.set_font_size(10)
            cr.move_to(6, y + 4)
            cr.show_text("%.1f" % (peak * frac))
            cr.set_source_rgba(col.red, col.green, col.blue, 0.15)
        cr.set_source_rgba(col.red, col.green, col.blue, 0.6)
        cr.move_to(6, 12)
        cr.show_text("Mb/s")
        step = (W - L - 10) / max(1, e.rx_hist.maxlen - 1)
        for hist, rgb in ((e.rx_hist, (0.21, 0.52, 0.89)), (e.tx_hist, (0.18, 0.76, 0.49))):
            if len(hist) < 2:
                continue
            cr.set_source_rgb(*rgb)
            cr.set_line_width(2)
            for i, v in enumerate(hist):
                x = L + i * step
                y = H - BOT - (v / peak) * (H - TOP - BOT)
                (cr.move_to if i == 0 else cr.line_to)(x, y)
            cr.stroke()
        cr.set_font_size(10)
        cr.set_source_rgb(0.21, 0.52, 0.89)
        cr.move_to(W - 150, 12)
        cr.show_text("■ download")
        cr.set_source_rgb(0.18, 0.76, 0.49)
        cr.move_to(W - 70, 12)
        cr.show_text("■ upload")

    # ---- Network -------------------------------------------------------------------------
    def page_network(self, page):
        e = self.engine
        if not e.iface:
            page.add(self._status_group("No dock network chip", "Plug in a dock with an Ethernet port and this page fills in."))
            return
        state, c = core.native_state(e.iface)
        native = state == "native" and core.have("ethtool")

        g = group("Speed control", "Force a fixed link speed and duplex, or re-run auto-negotiation. Handy to cap a flaky cable at 100 Mb/s. Needs native mode.")
        if native:
            cr = Adw.ComboRow()
            cr.set_title("Link speed")
            cr.set_model(Gtk.StringList.new(core.Engine.SPEEDS))
            g.add(cr)
            apply = button("Apply", lambda: e.speed_set(core.Engine.SPEEDS[cr.get_selected()], after=lambda: self.toast("Speed applied")))
            reneg = button("Renegotiate", lambda: e.renegotiate(after=lambda: self.toast("Renegotiating")))
            btns = button_row("Apply", "", apply, reneg)
            if e.flow_supported():
                btns.add_suffix(button("Flow on", lambda: e.flow_set(True)))
                btns.add_suffix(button("Flow off", lambda: e.flow_set(False)))
            g.add(btns)
        else:
            g.add(self._locked_row())
        page.add(g)

        g2 = group("Offloads", "Each of these moves a small networking chore off your computer's CPU onto the dock's Ethernet chip. "
                              "On is normal. Off makes every packet visible, which helps when capturing traffic or chasing a bug.")
        if native:
            feats = core.ethtool_offloads(e.iface)
            shown = 0
            for key, label, why in core.Engine.OFFLOADS:
                if key not in feats:
                    continue
                sr = Adw.SwitchRow()
                sr.set_title(label)
                sr.set_subtitle(why)
                sr.set_active(bool(feats[key]))
                sr.connect("notify::active", lambda w, _p, k=key: e.offload_set(k, w.get_active()))
                g2.add(sr)
                shown += 1
            if not shown:
                g2.add(row("Offloads", "This NIC doesn't expose toggleable offloads.", prop=False))
        else:
            g2.add(self._locked_row())
        page.add(g2)

        if core.have("nmcli"):
            g3 = group("NetworkManager tidy-up", "Switching dock modes repeatedly can leave unused 'Wired connection N' profiles behind.")
            g3.add(button_row("Unused wired profiles", "Profiles not attached to any device", button("Find & remove", self._nm_cleanup)))
            page.add(g3)

    def _locked_row(self):
        r = Adw.ActionRow()
        r.set_title("Unlock native mode first")
        r.set_subtitle("These controls need the chip maker's own driver.")
        r.add_suffix(button("Go to Native Mode", lambda: self._goto("Native Mode")))
        return r

    def _goto(self, name):
        for i in range(len(PAGES)):
            r = self.nav.get_row_at_index(i)
            if r.page_name == name:
                self.nav.select_row(r)
                return

    def _nm_cleanup(self):
        d = self.engine.nm_duplicate_profiles()
        if not d:
            self.toast("No unused wired profiles found")
            return
        counts = {}
        for _u, name in d:
            counts[name] = counts.get(name, 0) + 1
        lines = ["• %s%s" % (n, " (%d copies)" % c if c > 1 else "") for n, c in sorted(counts.items())]
        self.confirm("Remove %d unused wired profile%s?" % (len(d), "" if len(d) == 1 else "s"),
                     "\n".join(lines) + "\n\nOnly profiles not attached to any device, removed one by one by their id. The connection you are using stays.",
                     "Remove", lambda: self.engine.nm_cleanup(d), destructive=True)

    def _status_group(self, title, description):
        g = Adw.PreferencesGroup()
        sp = Adw.StatusPage()
        sp.set_title(title)
        sp.set_description(description)
        sp.set_icon_name("network-wired-disconnected-symbolic")
        g.add(sp)
        return g

    # ---- Native mode -----------------------------------------------------------------------------
    def page_native(self, page):
        e = self.engine
        state, c, rows = e.native_header()
        g = group("Native mode", "Dock network chips ship in a plug-and-play mode any OS can drive. Native mode is the chip maker's own driver: "
                                 "it unlocks Wake-on-LAN, real hardware statistics and link tuning. Fully reversible.")
        page.add(g)
        if not c or not c["vendor"]:
            g.add(row("Chip", "No ASIX or Realtek NIC detected on this dock — nothing to unlock here.", prop=False))
            return
        for k, v in rows:
            g.add(row(k, v))
        st = row("Status", e.native_status[0] or "—")
        self._set_status_row(st, e.native_status[0], e.native_status[1])
        self.widgets["native_status"] = st
        self.widgets["native_rows"] = (g, rows)
        self._native_last = state
        g.add(st)

        if state == "unsupported":
            g.add(row("Mode", "This NIC is already on its native driver, or doesn't expose a separate native config.", prop=False))
            if c["driver"] in core.NATIVE_DRIVERS:
                self._native_features(page)
            return
        if state == "unlockable":
            if e.native_known_bad():
                warn = button_row("Native mode isn't usable on this dock", "Last time the native driver ran, the wired link wouldn't come up. Standard mode works. You can still try again.",
                                  button("Why?", lambda: self.info("Why native mode won't work here", core.Engine.WHY_NATIVE_FAILED)))
                warn.add_css_class("warning")
                g.add(warn)
            g.add(button_row("Unlock native mode", "The link drops for a few seconds; Wi-Fi covers you; reversible.",
                             button("Unlock", self._unlock, kind="suggested-action")))
            return
        g.add(button_row("Return to standard mode", "Puts the chip back to plug-and-play (CDC) mode.", button("Return", self._relock)))
        self._native_features(page)

    def _native_tick(self):
        e = self.engine
        state, c, rows = e.native_header()
        pack = self.widgets.get("native_rows")
        if pack and c and c["vendor"]:
            g, old = pack
            if rows != old:
                lb = self._find_listbox(g)
                if lb:
                    for i, (k, v) in enumerate(rows):
                        r = lb.get_row_at_index(i)
                        if r:
                            r.set_subtitle(GLib.markup_escape_text(v))
                self.widgets["native_rows"] = (g, rows)
        if state != getattr(self, "_native_last", state) and e.native_status[1] != "busy":
            self.show("Native Mode")

    def _unlock(self):
        self.confirm("Unlock native mode?",
                     "This switches the dock's network chip from standard (plug-and-play) mode into the manufacturer's native mode.\n\n"
                     "• The wired link drops for a few seconds while it re-initialises.\n• Wi-Fi stays up.\n• Fully reversible.",
                     "Unlock", self.engine.native_unlock)

    def _relock(self):
        self.confirm("Return to standard mode?", "The wired link drops for a few seconds while it re-initialises.", "Return", self.engine.native_relock)

    def _native_features(self, page):
        e = self.engine
        if not core.have("ethtool"):
            page.add(group("Features", "Install ethtool to use the native-mode controls:  sudo apt install ethtool"))
            return
        g = group("Wake-on-LAN", "Let a magic packet wake this laptop from sleep through the dock's wired link, and wake your other machines.")
        st = row("This NIC", "reading…")
        self.widgets["wol"] = st
        g.add(st)

        def refresh():
            def work():
                words = core.wol_words(e.wol_state())
                GLib.idle_add(lambda: (st.set_subtitle(words), False)[1])
            threading.Thread(target=work, daemon=True).start()
        refresh()
        g.add(button_row("Arm or disarm", "Wake on magic packet",
                         button("Arm", lambda: e.wol_set(True, after=refresh), kind="suggested-action"),
                         button("Disarm", lambda: e.wol_set(False, after=refresh))))
        er = Adw.EntryRow()
        er.set_title("Wake another machine — MAC address")
        send = button("Send magic packet", lambda: self._wol_send(er.get_text()))
        er.add_suffix(send)
        g.add(er)
        page.add(g)

        g2 = group("EEPROM", "Some NICs keep their settings in a rewritable EEPROM; others use one-time eFuse and read back as all-FF. Reading is safe; DockPilot never writes it.")
        verdict = row("Verdict", "reading…")
        g2.add(verdict)
        hexlbl = mono("")
        g2.add(hexlbl)
        g2.add(button_row("Read or back up", "", button("Read", lambda: e.eeprom_read(lambda v, h: (verdict.set_subtitle(v), hexlbl.set_text(h)))),
                          button("Back up to file", lambda: e.eeprom_backup(lambda ok, res: self.toast(("Saved " if ok else "Couldn't back up: ") + res, 5)))))
        page.add(g2)
        e.eeprom_read(lambda v, h: (verdict.set_subtitle(v), hexlbl.set_text(h)))
        page.add(group("", "Speed control and offloads are on the Network page once native mode is active."))

    def _wol_send(self, mac):
        ok, msg = self.engine.wol_send(mac.strip())
        self.toast(msg, 4)

    # ---- USB & Power ----------------------------------------------------------------------
    def page_usb(self, page):
        e = self.engine
        anchors, leaves = e.usb_leaves()
        g = group("Devices inside the dock", "Only the dock's own hub subtree — things plugged into the laptop's own ports are excluded.")
        if not anchors:
            g.add(row("Hub", "No dock hub detected.", prop=False))
        elif not leaves:
            g.add(row("Devices", "No non-hub devices currently attached.", prop=False))
        for dev in leaves:
            nm = dev["product"] or "%s:%s" % (dev["vid"], dev["pid"])
            r = Adw.ActionRow()
            r.set_title(GLib.markup_escape_text(nm[:60]))
            r.set_subtitle("%s:%s" % (dev["vid"], dev["pid"]))
            af = core.usb_authorized_file(dev["path"])
            if af:
                r.add_suffix(button("Disable", lambda p=af: e.usb_authorize(p, False, after=lambda: self._refresh_if("USB & Power")), kind="destructive-action"))
                r.add_suffix(button("Enable", lambda p=af: e.usb_authorize(p, True, after=lambda: self._refresh_if("USB & Power"))))
            g.add(r)
        page.add(g)

        g2 = group("Per-port power", "Cut or restore 5 V on a single port. Only hubs with per-port power switching support it — most docks don't. Needs uhubctl.")
        self.widgets["ports"] = g2
        if core.have("uhubctl"):
            g2.set_header_suffix(button("Scan", self._scan_ports))
            g2.add(row("Ports", "Press Scan.", prop=False))
        else:
            g2.add(row("uhubctl", "not installed (optional):  sudo apt install uhubctl", prop=False))
        page.add(g2)

    def _scan_ports(self):
        g = self.widgets.get("ports")
        if not g:
            return

        def done(hubs, raw):
            lb = self._find_listbox(g)
            if lb:
                r = lb.get_first_child()
                while r:
                    nxt = r.get_next_sibling()
                    lb.remove(r)
                    r = nxt
            sw = [h for h in hubs if h["ports"]]
            if not sw:
                g.add(row("Ports", "No switchable hubs — this hardware doesn't expose per-port power.", prop=False))
                return
            for h in sw:
                for p in h["ports"]:
                    r = Adw.ActionRow()
                    r.set_title("Hub %s · port %d" % (h["loc"], p))
                    r.set_subtitle(GLib.markup_escape_text(h["desc"]))
                    for lbl, act, kind in (("On", "on", None), ("Off", "off", "destructive-action"), ("Cycle", "cycle", None)):
                        r.add_suffix(button(lbl, lambda l=h["loc"], pp=p, a=act: self.engine.port_power(l, pp, a), kind=kind))
                    g.add(r)
        self.engine.scan_ports(done)

    # ---- Topology ---------------------------------------------------------------------------
    def page_topology(self, page):
        trees, note = self.engine.topology_rows()
        g = group("Dock topology", note or "The dock as a device: its upstream hub, every downstream port and what sits on each — empty ports included.")
        g.set_header_suffix(button("Redraw", lambda: self.show("Topology")))
        if not trees:
            g.add(row("Hub", "No USB hub detected to draw.", prop=False))
        icons = {"hub": "network-workgroup-symbolic", "dev": "drive-removable-media-symbolic", "empty": "list-remove-symbolic"}
        for top, rows in trees:
            if len(trees) > 1:
                g.add(row("Hub", top, prop=False))
            for depth, label, kind in rows:
                r = Adw.ActionRow()
                r.set_title(GLib.markup_escape_text(("    " * depth) + label[:70]))
                r.add_prefix(Gtk.Image.new_from_icon_name(icons[kind]))
                if kind == "empty":
                    r.add_css_class("dim-label")
                g.add(r)
        page.add(g)

    # ---- Audio ---------------------------------------------------------------------------------
    def page_audio(self, page):
        e = self.engine
        if not core.have("pactl"):
            page.add(group("Audio", "pactl is not available — audio control is off."))
            return
        g = group("Audio output", "Controls the selected output; 'Current output' follows whatever you are using, including the dock's jack.")
        sinks = ["Current output"] + core.list_sinks()
        cr = Adw.ComboRow()
        cr.set_title("Output")
        cr.set_model(Gtk.StringList.new(sinks))
        g.add(cr)

        def cur():
            i = cr.get_selected()
            return None if i == 0 else sinks[i]
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        scale.set_hexpand(True)
        scale.set_draw_value(True)
        scale.set_size_request(260, -1)
        try:
            scale.set_value(core.sink_volume(cur() or core.default_sink()))
        except Exception:
            pass
        scale.connect("value-changed", lambda s: e.set_volume(cur(), s.get_value()))
        vr = Adw.ActionRow()
        vr.set_title("Volume")
        vr.add_suffix(scale)
        g.add(vr)
        cr.connect("notify::selected", lambda *_: scale.set_value(core.sink_volume(cur() or core.default_sink())))
        g.add(button_row("Mute", "Toggle mute on the selected output", button("Toggle mute", lambda: e.toggle_mute(cur()))))
        page.add(g)

    # ---- Automation --------------------------------------------------------------------------------
    def page_automation(self, page):
        e = self.engine
        g = group("When a dock connects or disconnects", "For docks without an OpenDock board. At an OpenDock, the dock's own page decides instead and these stand down.")
        at_od = bool((e.od_snap or {}).get("connected"))
        for key, title, sub, tool in (("wifi", "Wi-Fi off when docked", "Turned back on when the dock disconnects.", "nmcli"),
                                      ("audio", "Audio to the dock", "Switch the audio output to the dock on connect.", "pactl")):
            sr = Adw.SwitchRow()
            sr.set_title(title)
            sr.set_subtitle(sub)
            sr.set_active(e.rules[key])
            if not core.have(tool):
                sr.set_sensitive(False)
                sr.set_subtitle("Needs %s." % tool)
            elif at_od:
                sr.set_sensitive(False)
                sr.set_subtitle("An OpenDock is connected — this is set on the OpenDock page, under Rules.")
            sr.connect("notify::active", lambda w, _p, k=key: e.set_rule(k, w.get_active()))
            g.add(sr)
        page.add(g)

        if e.iface:
            g2 = group("MAC address", "Pass-through makes the dock present your laptop's own MAC, so the network sees one identity docked or not. "
                                      "The original MAC was captured at first launch, so Reset always works. A custom MAC won't get online on a "
                                      "network that is bound to the real one until you reset.")
            sr = Adw.SwitchRow()
            sr.set_title("Pass-through on every connect")
            sr.set_subtitle("The dock wears the laptop's MAC while docked and returns to its own on disconnect.")
            sr.set_active(e.rules["mac"])
            sr.connect("notify::active", lambda w, _p: e.set_rule("mac", w.get_active()))
            g2.add(sr)
            src = core.primary_host_iface(e.iface)
            g2.add(button_row("Once, now", "Mirror the laptop's MAC onto the dock, or put the dock's own MAC back.",
                              button("Mirror %s → dock" % (src or "laptop"), lambda: e.mac_passthrough(after=lambda: self.toast("MAC mirrored"))),
                              button("Reset to original", lambda: e.mac_restore(after=lambda: self.toast("MAC reset")), kind="suggested-action")))
            er = Adw.EntryRow()
            er.set_title("Custom MAC")
            er.set_show_apply_button(True)
            er.connect("apply", lambda w: self._mac_custom(w.get_text()))
            er.add_suffix(button("Random", lambda: e.set_mac(core.random_mac(), after=lambda: self.toast("Random MAC set"))))
            g2.add(er)
            page.add(g2)

        g3 = group("Macros", "Your own commands on connect, disconnect, port power or OpenDock events. All start disabled.")
        hdr = Gtk.Box(spacing=6)
        hdr.append(button("New macro", lambda: self._macro_edit(None)))
        hdr.append(button("Add starter templates", self._macro_templates))
        g3.set_header_suffix(hdr)
        store = e.macro_store
        if not store.macros:
            g3.add(row("Macros", "None yet. Add the starter templates to see examples.", prop=False))
        for idx, m in enumerate(store.macros):
            ex = Adw.ExpanderRow()
            ex.set_title(GLib.markup_escape_text(m.get("name", "unnamed")))
            ex.set_subtitle("%s%s" % (m.get("trigger", ""), (" · only " + m["dock"]) if m.get("dock") else ""))
            sw = Gtk.Switch()
            sw.set_valign(Gtk.Align.CENTER)
            sw.set_active(bool(m.get("enabled")))
            sw.set_tooltip_text("Enabled")
            sw.connect("notify::active", lambda w, _p, i=idx: self._macro_enable(i, w.get_active()))
            ex.add_suffix(sw)
            ex.add_row(mono("\n".join(m.get("commands", [])) or "(no commands)"))
            ex.add_row(button_row("Edit or delete", "", button("Edit", lambda i=idx: self._macro_edit(i)),
                                  button("Delete", lambda i=idx: self._macro_delete(i), kind="destructive-action")))
            g3.add(ex)
        page.add(g3)

    def _mac_custom(self, mac):
        mac = mac.strip().lower()
        if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
            self.engine.set_mac(mac, after=lambda: self.toast("MAC set"))
        else:
            self.toast("Bad MAC format — want aa:bb:cc:dd:ee:ff")

    def _macro_enable(self, idx, on):
        store = self.engine.macro_store
        if 0 <= idx < len(store.macros):
            store.macros[idx]["enabled"] = bool(on)
            store.save()
            self.engine.log("# macro %r %s" % (store.macros[idx].get("name"), "enabled" if on else "disabled"))

    def _macro_templates(self):
        n = self.engine.macro_store.add_templates()
        self.toast("%d template(s) added, all disabled" % n if n else "All templates are already there")
        self.show("Automation")

    def _macro_delete(self, idx):
        store = self.engine.macro_store
        name = store.macros[idx].get("name", "macro") if idx < len(store.macros) else "macro"
        self.confirm("Delete %s?" % name, "This cannot be undone.", "Delete", lambda: (store.remove(idx), self.show("Automation")), destructive=True)

    def _macro_edit(self, idx):
        store = self.engine.macro_store
        m = dict(store.macros[idx]) if idx is not None else {"name": "", "trigger": "connect", "dock": "", "enabled": False, "commands": []}
        dlg = Adw.Dialog()
        dlg.set_title("Edit macro" if idx is not None else "New macro")
        dlg.set_content_width(560)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        tv.add_top_bar(hb)
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup()
        name = Adw.EntryRow()
        name.set_title("Name")
        name.set_text(m.get("name", ""))
        trig = Adw.ComboRow()
        trig.set_title("Trigger")
        trig.set_model(Gtk.StringList.new(core.TRIGGERS))
        trig.set_selected(core.TRIGGERS.index(m.get("trigger")) if m.get("trigger") in core.TRIGGERS else 0)
        dock = Adw.EntryRow()
        dock.set_title("Only for this dock model (blank = any)")
        dock.set_text(m.get("dock", ""))
        g.add(name)
        g.add(trig)
        g.add(dock)
        page.add(g)
        g2 = Adw.PreferencesGroup()
        g2.set_title("Commands, one per line")
        g2.set_description("Lines starting with # are comments. Variables: $DOCK_MODEL, $DOCK_SERIAL, $DOCK_NAME and friends on OpenDock events.")
        cmds = Gtk.TextView()
        cmds.set_monospace(True)
        cmds.get_buffer().set_text("\n".join(m.get("commands", [])))
        sw = Gtk.ScrolledWindow()
        sw.set_min_content_height(160)
        sw.set_child(cmds)
        sw.add_css_class("card")
        g2.add(sw)
        page.add(g2)
        tv.set_content(page)
        save = button("Save", kind="suggested-action")
        hb.pack_end(save)

        def do_save():
            buf = cmds.get_buffer()
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
            new = {"name": name.get_text().strip() or "unnamed", "trigger": core.TRIGGERS[trig.get_selected()],
                   "dock": dock.get_text().strip(), "enabled": bool(m.get("enabled")), "commands": [l for l in text.splitlines()]}
            if idx is None:
                store.add(new)
            else:
                store.update(idx, new)
            dlg.close()
            self.show("Automation")
        save.connect("clicked", lambda *_: do_save())
        dlg.set_child(tv)
        dlg.present(self)

    # ---- Logs ---------------------------------------------------------------------------------------
    def page_logs(self, page):
        e = self.engine
        g = group("Command log", "Every action that changes something is echoed here.")
        tv = Gtk.TextView()
        tv.set_editable(False)
        tv.set_monospace(True)
        tv.get_buffer().set_text("\n".join(e.log_lines) + "\n")
        sw = Gtk.ScrolledWindow()
        sw.set_min_content_height(360)
        sw.set_child(tv)
        sw.add_css_class("card")
        g.add(sw)
        self.widgets["logview"] = tv
        page.add(g)
        GLib.idle_add(lambda: tv.scroll_to_mark(tv.get_buffer().get_insert(), 0, False, 0, 0))

        g2 = group("Background service", "Runs the rules and keeps the OpenDock link with no window open.")
        if self.use_service:
            t2 = Gtk.TextView()
            t2.set_editable(False)
            t2.set_monospace(True)
            t2.get_buffer().set_text("\n".join(self.service_log) + "\n")
            s2 = Gtk.ScrolledWindow()
            s2.set_min_content_height(160)
            s2.set_child(t2)
            s2.add_css_class("card")
            g2.add(s2)
        else:
            g2.add(button_row("Status", "not running — this window does everything itself",
                              button("Start service", self._start_service)))
        page.add(g2)

        g3 = group("System snapshot", "One diagnostic file: chips, drivers, USB topology, link statistics and this log. IPs and hostname are redacted.")
        g3.add(button_row("Save snapshot", "Written to your home folder", button("Save", self._snapshot)))
        page.add(g3)

    def _snapshot(self):
        try:
            path = self.engine.save_snapshot()
            self.engine.log("# snapshot written: %s" % path)
            self.toast("Snapshot saved: %s" % path, 6)
        except Exception as ex:
            self.toast("Couldn't write snapshot: %s" % ex, 6)

    def _start_service(self):
        rc, out = core.run(["systemctl", "--user", "start", "dockpilot.service"])
        self.toast("Service started" if rc == 0 else "Couldn't start it: %s" % (out or "no systemd unit installed"), 5)

    # ---- OpenDock ---------------------------------------------------------------------------------------
    def page_opendock(self, page):
        """Four tabs when a dock is present — Dock, Setup, Rules, Records — one card when it is not."""
        e = self.engine
        self.content_hb.set_title_widget(None)
        if core.opendock is None or (core.opendock.serial is None and not self.use_service):
            g = group("OpenDock")
            g.add(row("Needs pyserial", "sudo apt install python3-serial, then restart DockPilot", prop=False))
            page.add(g)
            return
        snap = e.od_snap or {}
        if not snap.get("connected"):
            g = group("OpenDock", "A dock with the OpenDock board inside holds the desk: its name, its network options, your rules and your saved configuration.")
            why = snap.get("error") or "Plug one in — it is found within a few seconds."
            if why.startswith("no reply") or why.startswith("port write failed"):
                why = "The dock stopped answering — unplugged? It is picked up again within a few seconds."
            g.add(row("No OpenDock found", why, prop=False))
            if core.dialout_hint():
                g.add(button_row("Serial port permission", "Install the udev rule once and the dock is yours without any group change.",
                                 button("Allow access", self._install_udev)))
            page.add(g)
            return

        stack = Adw.ViewStack()
        for name, icon, builders in (("Dock", "view-grid-symbolic", (self._tab_dock,)),
                                     ("Setup", "document-edit-symbolic", (self._tab_setup,)),
                                     ("Rules", "emblem-system-symbolic", (self._tab_rules,)),
                                     ("Records", "text-x-generic-symbolic", (self._tab_records,))):
            pg = Adw.PreferencesPage()
            for b in builders:
                b(pg, snap)
            stack.add_titled_with_icon(pg, name, name, icon)
        sw = Adw.ViewSwitcher()
        sw.set_stack(stack)
        sw.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        self.content_hb.set_title_widget(sw)
        self.widgets["od_stack"] = stack
        if getattr(self, "_od_tab", None):
            stack.set_visible_child_name(self._od_tab)
        stack.connect("notify::visible-child-name", lambda st, _p: setattr(self, "_od_tab", st.get_visible_child_name()))
        stack.set_vexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(stack)
        foot = Gtk.Label(label=core.WEBSITE)
        foot.add_css_class("dim-label")
        foot.set_margin_top(10)
        foot.set_margin_bottom(14)
        box.append(foot)
        self.content_view.set_content(box)
        self._od_direct = True

    @staticmethod
    def _no_desk(snap):
        """A line for a dock whose firmware keeps no desk (before 0.6), or None."""
        if snap.get("desk") is not None:
            return None
        fw = (snap.get("identity") or {}).get("fw", "?")
        return "This dock's firmware (%s) keeps no desk. Firmware 0.6 or later holds the desk tag, options, keys and your rules." % fw

    @staticmethod
    def _below(page, *buttons):
        """Buttons under the box they act on."""
        g = Adw.PreferencesGroup()
        box = Gtk.Box(spacing=8)
        box.set_margin_top(4)
        for b in buttons:
            box.append(b)
        g.add(box)
        page.add(g)

    @staticmethod
    def _textbox(text, height=200, editable=False):
        tv = Gtk.TextView()
        tv.set_editable(editable)
        tv.set_monospace(True)
        tv.get_buffer().set_text(text)
        sw = Gtk.ScrolledWindow()
        sw.set_min_content_height(height)
        sw.set_child(tv)
        sw.add_css_class("card")
        return tv, sw

    # -- Dock: this dock, right now, at this desk
    def _tab_dock(self, page, snap):
        g = group("This dock")
        self.widgets["od_rows"] = {}
        rows_spec = [("serial", "Serial"), ("model", "Model")]
        if snap.get("health") is not None:
            rows_spec.append(("health", "Health"))
        rows_spec.append(("tag", "Desk"))
        if (snap.get("caps") or {}).get("web"):
            rows_spec.append(("web", "Panel"))
        rows_spec.append(("site", "Website"))
        for k, t in rows_spec:
            r = row(t, "—")
            self.widgets["od_rows"][k] = r
            g.add(r)
        if (snap.get("caps") or {}).get("web"):
            r = row("Firmware", "—")
            r.add_suffix(button("Update…", self._firmware_update))
            self.widgets["od_rows"]["fw"] = r
            g.add(r)
        page.add(g)
        g2 = group("Right now")
        rows2 = []
        if (snap.get("status") or {}).get("vbus_v") is not None:
            rows2.append(("power", "Power"))
        rows2 += [("temp", "Temperature"), ("uptime", "Running for"), ("log", "Log")]
        if snap.get("stats") is not None:
            rows2.append(("stats", "Lifetime"))
        for k, t in rows2:
            r = row(t, "—")
            self.widgets["od_rows"][k] = r
            g2.add(r)
        page.add(g2)
        if snap.get("desk") is not None:
            g3 = group("At this desk", "What the dock handed to this laptop when it docked. Applied while docked.")
            for k, t in (("net", "Network"), ("dns", "DNS"), ("time", "Time"), ("proxy", "Proxy"), ("vpn", "VPN"), ("keys", "Keys stored")):
                r = row(t, "—")
                self.widgets["od_rows"][k] = r
                g3.add(r)
            page.add(g3)
        else:
            g3 = group("At this desk")
            g3.add(row("Nothing handed out", self._no_desk(snap), prop=False))
            page.add(g3)
        self._opendock_tick()

    # -- Setup: what is kept in the dock
    def _tab_setup(self, page, snap):
        st = snap.get("status") or {}
        g = group("Kept in the dock")
        er = Adw.EntryRow()
        er.set_title("Desk Tag")
        er.set_text(str(st.get("name", "") or ""))
        er.set_show_apply_button(True)
        er.connect("apply", lambda w: self._od_set("name", w.get_text()))
        g.add(er)
        page.add(g)
        why = self._no_desk(snap)
        if why:
            g2 = group("Desk options")
            g2.add(row("Not on this dock", why, prop=False))
            page.add(g2)
            return
        desk = snap.get("desk") or {}
        opt = desk.get("opt") or {}
        g2 = group("Desk options", "Apply the DHCP options shown or leave blank. Will be automatically appended every time you connect to this dock from this computer.")
        for o in snap.get("schema") or []:
            er = Adw.EntryRow()
            er.set_title(o["label"] + (" — " + o["hint"] if o.get("hint") else ""))
            er.set_text(str(opt.get(o["id"], "") or ""))
            er.set_show_apply_button(True)
            er.connect("apply", lambda w, oid=o["id"]: self._desk("set_opt", oid, w.get_text().strip(), note="%s saved" % oid))
            g2.add(er)
        page.add(g2)
        self._below(page, button("Apply from file", self._desk_import, tooltip="A file made by Export, or written by hand"),
                    button("Export", self._desk_export, tooltip="A text file in your home folder: the whole desk as it is now"))
        keys = snap.get("keys") or {}
        g3 = group("Key storage", "Add keys to store on the docking station for later use.")
        for k in sorted(keys):
            er = Adw.EntryRow()
            er.set_title(k)
            er.set_text(keys[k])
            er.set_show_apply_button(True)
            er.connect("apply", lambda w, name=k: self._desk("set_key", name, w.get_text(), note="%s saved" % name))
            er.add_suffix(button("Remove", lambda name=k: self._desk("del_key", name, note="%s removed" % name), kind="flat"))
            g3.add(er)
        n, nmax = len(keys), int(desk.get("keys_max", 32))
        g3.add(button_row("Add a key", "%d of %d used" % (n, nmax) if n else "%d free" % nmax, button("Add", self._desk_add_key)))
        page.add(g3)

    # -- Rules: this laptop at this desk
    def _tab_rules(self, page, snap):
        why = self._no_desk(snap)
        if why:
            g = group("Rules")
            g.add(row("Not on this dock", why, prop=False))
            page.add(g)
            return
        seat = snap.get("seat") or {}
        rules_on = seat.get("rules") or {}
        g = group("Rules", "What the laptop does when it docks here. Stored in the dock, so they follow the desk, not the laptop.")
        for r in snap.get("rules") or []:
            tool = core.RULE_TOOLS.get(r["id"], None)
            known = r["id"] in core.RULE_TOOLS
            sr = Adw.SwitchRow()
            sr.set_title(r["label"])
            sub = r.get("hint") or ""
            if not known:
                sub = "This dock knows a rule DockPilot %s does not; stored, not acted on." % core.APP_VERSION
            elif tool and not core.have(tool):
                sub = "Not available on this system (needs %s)." % tool
                sr.set_sensitive(False)
            sr.set_subtitle(sub)
            sr.set_active(bool(rules_on.get(r["id"])))
            sr.connect("notify::active", lambda w, _p, rid=r["id"], lab=r["label"]: self._desk("set_rule", rid, w.get_active(), note="%s: %s" % (lab, "on" if w.get_active() else "off")))
            g.add(sr)
        page.add(g)

        cmd = seat.get("cmd") or {}
        g2 = group("Your own commands", "Run something of your own when you dock or undock — mount a drive, start a backup, open the apps you use at this desk. "
                   "One command per line, as you would type it in a terminal. Kept in the dock, run by this laptop as you, from your home folder, "
                   "in the background: there is no terminal, so anything a command prints goes to the Logs page. $DOCK_TAG and $DOCK_SERIAL are set.")
        for when, title in (("dock", "When I dock"), ("undock", "When I undock")):
            lines = cmd.get(when) or []
            ex = Adw.ExpanderRow()
            ex.set_title(title)
            ex.set_subtitle("%d command%s" % (len(lines), "" if len(lines) == 1 else "s") if lines else "none")
            tv, sw = self._textbox("\n".join(lines), height=80, editable=True)
            ex.add_row(sw)
            ex.add_row(button_row("Up to 4 lines", "", button("Save", lambda w=when, t=tv: self._desk_cmds(w, t), kind="suggested-action")))
            g2.add(ex)
        page.add(g2)

        g3 = group("Saved configuration", "A snapshot of how the laptop is set up at this desk — screens, sound, keyboard, VPN. Kept in the dock, restored every time you dock.")
        snap_t = seat.get("snap_t") or 0
        title = ("Saved " + time.strftime("%d %b, %H:%M", time.localtime(snap_t))) if seat.get("snap") and snap_t else ("Saved" if seat.get("snap") else "Nothing saved yet")
        g3.add(button_row(title, core.snapshot_words(seat.get("snap") or ""),
                          button("Restore now", self._restore_setup), button("Save current setup", self._save_setup)))
        sr = Adw.SwitchRow()
        sr.set_title("Save again every time I undock")
        sr.set_subtitle("So next time starts exactly where you left it.")
        sr.set_active(bool(seat.get("autosave")))
        sr.connect("notify::active", lambda w, _p: self._desk("set_autosave", w.get_active(), note="save on undock: %s" % ("on" if w.get_active() else "off")))
        g3.add(sr)
        page.add(g3)

        g4 = group("Your other laptops", "Your rules and configuration are kept in the dock under a key only your laptops know. The dock never learns who you are.")
        g4.add(button_row("Show my key", "Six words to type into DockPilot on your other laptop.", button("Show", self._seat_show)))
        g4.add(button_row("Use a key from another laptop", "Makes this laptop the same seat as that one, at every dock.", button("Enter key", self._seat_enter)))
        g4.add(button_row("Forget me on this dock", "Your rules and saved configuration are removed from the dock; the other laptop loses them too.",
                          button("Forget", lambda: self.confirm("Forget me on this dock?", "Your rules, commands and saved configuration are deleted from the dock. The key stays; other docks are untouched.",
                                                                "Forget", lambda: self._desk("forget_seat", note="seat forgotten"), destructive=True), kind="destructive-action")))
        page.add(g4)

    # -- Records: hours, the log, the configuration
    def _tab_records(self, page, snap):
        u = core.ask("usage", days=14) if self.use_service else None
        u = u.get("usage") if u and u.get("ok") else self.engine.od_usage(14)
        gc = group("Hours docked, last two weeks")
        area = Gtk.DrawingArea()
        area.set_content_height(130)
        area.set_draw_func(lambda a, cr, W, H: self._draw_usage(a, cr, W, H, u))
        gc.add(area)
        words = ["Today %s" % core._hms_short(u["today_s"]) if u["today_s"] else "Today: not yet"]
        words.append("last 7 days %s in %d session%s" % (core._hms_short(u["week_s"]), u["sessions"], "" if u["sessions"] == 1 else "s") if u["week_s"] else "nothing in the last 7 days")
        if u["last_used"]:
            words.append("last used " + time.strftime("%a %d %b, %H:%M", time.localtime(u["last_used"])))
        lab = Gtk.Label(label=" · ".join(words), xalign=0)
        lab.add_css_class("dim-label")
        lab.set_margin_start(8)
        lab.set_margin_top(4)
        gc.add(lab)
        page.add(gc)

        g = group("Log file")
        entries = snap.get("log") or []
        tv, sw = self._textbox("\n".join(core.opendock.describe(x) for x in entries[-300:]) or "(nothing yet)")
        g.add(sw)
        page.add(g)
        self._below(page, button("Export", self._od_export, tooltip="A JSON file in your home folder"),
                    button("Clear", lambda: self.confirm("Clear the dock's log?", "The dock hides everything logged so far. The copy on this laptop is cleared too.", "Clear", self._od_clear, destructive=True)))
        GLib.idle_add(lambda: tv.scroll_to_mark(tv.get_buffer().get_insert(), 0, False, 0, 0))

        r = core.ask("report") if self.use_service else None
        rep = r.get("report") if r and r.get("ok") else self.engine.od_report()
        text = json.dumps(rep, indent=1)
        g2 = group("Configuration", "What an inventory or network system receives — fixed field names, nothing personal. Also kept as a file in ~/.cache/dockpilot/.")
        _tv, sw2 = self._textbox(text)
        g2.add(sw2)
        page.add(g2)
        self._below(page, button("Copy", lambda: self._copy(text)))

    # -- actions on the desk / seat
    def _desk(self, action, *args, note=None):
        """One write to the dock. Never blocks the window: the switch has already
        moved; the reply arrives as a toast."""
        rebuild = action in ("del_key", "set_key", "forget_seat", "import_lines")

        def finish(msg, snap=None):
            if snap:
                self.engine.od_snap = snap
            self.toast(msg, 4)
            if rebuild:
                self.show("OpenDock")

        if self.use_service:
            def worker():
                r = core.ask("desk", action=action, args=list(args))
                GLib.idle_add(lambda: (finish((r or {}).get("message", "no reply from the service"), (r or {}).get("opendock")), False)[1])
            threading.Thread(target=worker, daemon=True).start()
        else:
            self.engine.od_desk_action(action, *args, done=finish, note=note)

    def _desk_cmds(self, when, tv):
        buf = tv.get_buffer()
        lines = [l for l in buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).splitlines() if l.strip()]
        if len(lines) > 4:
            self.toast("Up to 4 lines", 4)
            return
        self._desk("set_cmd", when, lines, note="commands saved")

    def _desk_add_key(self):
        d = Adw.AlertDialog.new("Add a key", "A name (a-z, 0-9, _) and a value up to 128 characters. Scripts read it with:  opendock get <name>")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        name = Gtk.Entry(placeholder_text="name")
        value = Gtk.Entry(placeholder_text="value")
        box.append(name)
        box.append(value)
        d.set_extra_child(box)
        d.add_response("cancel", "Cancel")
        d.add_response("add", "Add")
        d.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("add")
        d.set_close_response("cancel")

        def resp(_d, r):
            if r != "add":
                return
            n = name.get_text().strip().lower()
            if not re.fullmatch(r"[a-z0-9_]{1,24}", n):
                self.toast("Key names are a-z, 0-9 and _ (up to 24)", 4)
                return
            self._desk("set_key", n, value.get_text(), note="%s saved" % n)
        d.connect("response", resp)
        d.present(self)

    def _desk_export(self):
        if self.use_service:
            r = core.ask("desk_export")
            self.toast("Exported: %s" % r["path"] if r and r.get("ok") else "Nothing to export", 6)
        else:
            try:
                p = self.engine.od_desk_export()
                self.toast("Exported: %s" % p if p else "Nothing to export", 6)
            except Exception as ex:
                self.toast("Export failed: %s" % ex, 6)

    def _firmware_update(self):
        snap = self.engine.od_snap or {}
        web = (snap.get("caps") or {}).get("web")
        if not web:
            return self.toast("This dock has no web face to send firmware to", 5)
        dlg = Gtk.FileDialog()
        dlg.set_title("Choose a firmware file (.uf2)")

        def done(d, res):
            try:
                f = d.open_finish(res)
            except Exception:
                return
            path = f.get_path()
            ok, msg = core.opendock.check_uf2(path)
            if not ok:
                return self.toast(msg[0].upper() + msg[1:], 7)
            fw_now = (snap.get("identity") or {}).get("fw")
            self.confirm("Update the dock's firmware?",
                         "%s (%s). The dock writes it beside the current firmware and restarts with it — about half a minute. "
                         "If the new firmware fails to start, the dock comes back on firmware %s by itself. DockPilot reconnects when the dock is back."
                         % (os.path.basename(path), msg, fw_now), "Upgrade", lambda: start(path))

        def start(path):
            self.toast("Sending %s to the dock…" % os.path.basename(path), 10)

            def worker():
                try:
                    line = core.opendock.update_firmware(web, path)
                    GLib.idle_add(lambda: (self.info("Firmware written", "%s\n\nThe dock is restarting with the new firmware. Its log will say whether it took (Records → fw_update)." % line), False)[1])
                except Exception as ex:
                    why = getattr(ex, "message", None) or str(ex)
                    GLib.idle_add(lambda: (self.info("Not updated", "%s\n\nThe dock is unchanged." % why), False)[1])
            threading.Thread(target=worker, daemon=True).start()
        dlg.open(self, None, done)

    def _desk_import(self):
        dlg = Gtk.FileDialog()
        dlg.set_title("Apply a desk from file")

        def done(d, res):
            try:
                f = d.open_finish(res)
            except Exception:
                return
            path = f.get_path()
            try:
                with open(path) as fh:
                    lines = fh.read().splitlines()
            except Exception as ex:
                self.toast("Couldn't read it: %s" % ex, 5)
                return
            self.confirm("Apply this file to the dock?", "The file defines the whole desk: options and keys not in it are removed. One write on the dock.",
                         "Apply", lambda: self._desk("import_lines", lines, note="desk applied from %s" % os.path.basename(path)))
        dlg.open(self, None, done)

    def _save_setup(self):
        if self.use_service:
            r = core.ask("save_setup")
            self.toast((r or {}).get("message", "no reply from the service"), 4)
        else:
            self.engine.save_snapshot_to_dock(done=lambda msg: self.toast(msg, 4))
        GLib.timeout_add(800, lambda: (self.show("OpenDock"), False)[1])

    def _restore_setup(self):
        if self.use_service:
            r = core.ask("restore_setup")
            n = (r or {}).get("steps", 0)
        else:
            n = self.engine.restore_snapshot()
        self.toast("Restored (%d things)" % n if n else "Nothing saved to restore", 4)

    def _seat_show(self):
        if self.use_service:
            r = core.ask("seat")
            phrase = (r or {}).get("phrase") or self.engine.seat_phrase
        else:
            phrase = self.engine.seat_phrase
        d = Adw.AlertDialog.new("Your key", "Type these six words into DockPilot on your other laptop (Rules → Use a key from another laptop). Anyone with them is you at every dock.")
        lab = Gtk.Label(label=phrase, selectable=True)
        lab.add_css_class("title-3")
        d.set_extra_child(lab)
        d.add_response("copy", "Copy")
        d.add_response("ok", "Done")
        d.set_default_response("ok")
        d.connect("response", lambda _d, r: self._copy(phrase) if r == "copy" else None)
        d.present(self)

    def _seat_enter(self):
        d = Adw.AlertDialog.new("Use a key from another laptop", "Six words, as shown on the other laptop. This laptop then has that laptop's rules and saved configuration at every dock. Its own seat on this dock is left as it is.")
        ent = Gtk.Entry(placeholder_text="copper-lantern-orbit-seven-maple-tide")
        d.set_extra_child(ent)
        d.add_response("cancel", "Cancel")
        d.add_response("use", "Use this key")
        d.set_response_appearance("use", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("use")
        d.set_close_response("cancel")

        def resp(_d, r):
            if r != "use":
                return
            ph = core.seat_phrase_clean(ent.get_text())
            if not ph:
                self.toast("That isn't a key — six words from the list, separated by dashes or spaces", 5)
                return
            if self.use_service:
                rr = core.ask("seat", phrase=ph)
                ok = bool(rr and rr.get("ok"))
                if ok:
                    self.engine.seat_phrase, self.engine.seat_id = ph, core.seat_id(ph)
            else:
                ok = self.engine.use_seat_phrase(ph)
            self.toast("Key in use — the dock is re-read in a few seconds" if ok else "Couldn't use that key", 5)
        d.connect("response", resp)
        d.present(self)

    def _draw_usage(self, area, cr, W, H, u):
        col = area.get_color()
        per = u["per_day"]
        n = len(per)
        L, R, TOP, BOT = 8, 8, 14, 22
        peak = max(per + [3600])
        gap = 4
        bw = (W - L - R - gap * (n - 1)) / n
        cr.set_font_size(9.5)
        for i, sec in enumerate(per):
            x = L + i * (bw + gap)
            h = (sec / peak) * (H - TOP - BOT)
            cr.set_source_rgba(col.red, col.green, col.blue, 0.12)
            cr.rectangle(x, TOP, bw, H - TOP - BOT)
            cr.fill()
            if sec:
                cr.set_source_rgba(col.red, col.green, col.blue, 0.55)
                cr.rectangle(x, H - BOT - h, bw, h)
                cr.fill()
            day = time.localtime(u["day0"] + 86400 * i)
            cr.set_source_rgba(col.red, col.green, col.blue, 0.6)
            cr.move_to(x + bw / 2 - 5, H - 8)
            cr.show_text(time.strftime("%a", day)[:2])
        cr.set_source_rgba(col.red, col.green, col.blue, 0.6)
        cr.move_to(L, 10)
        cr.show_text("busiest day %s" % core._hms_short(peak) if max(per) else "no docked time in the last %d days" % n)

    def _copy(self, text):
        try:
            self.get_clipboard().set(text)
            self.toast("Copied")
        except Exception:
            self.toast("Couldn't reach the clipboard")

    def _opendock_tick(self):
        rows = self.widgets.get("od_rows")
        snap = self.engine.od_snap or {}
        if not rows:
            if self.page_name == "OpenDock" and snap.get("connected"):
                self.show("OpenDock")
            return
        if not snap.get("connected"):
            self.show("OpenDock")
            return
        i, s = snap.get("identity") or {}, snap.get("status") or {}
        rows["serial"].set_subtitle("%s · chip id %s · on %s" % (i.get("serial"), i.get("uid"), snap.get("port")))
        rows["model"].set_subtitle("%s · firmware %s · protocol v%s" % (i.get("model"), i.get("fw"), i.get("proto")))
        rows["tag"].set_subtitle((s.get("name") or "—") + "   (edit under Setup)")
        web = (snap.get("caps") or {}).get("web")
        if web and "web" in rows:
            rows["web"].set_subtitle("http://%s · http://%s — the dock’s own page, any browser, any computer" % (web.get("host"), web.get("ip")))
        rows["site"].set_subtitle(core.WEBSITE)
        if "fw" in rows:
            rows["fw"].set_subtitle("%s — a new one from %s goes in here; the current one stays as the fallback." % (i.get("fw"), core.WEBSITE))
        if "power" in rows and s.get("vbus_v") is not None:
            v = s["vbus_v"] * (470 + 47) / 47          # the pin sees VBUS through 470k:47k
            rows["power"].set_subtitle("%.0f V on the host port" % v if v > 1 else "nothing on the host port")
        rows["temp"].set_subtitle("%s °C" % s.get("temp_c"))
        rows["uptime"].set_subtitle("%s · clock %s" % (core._hms(s.get("uptime_s", 0)), "synced" if s.get("time_synced") else "not synced"))
        log = "%s entries on the dock, %s kept here" % (s.get("log_entries"), snap.get("log_count", 0))
        if s.get("log_volatile"):
            log += " · dev jumper fitted: nothing can be written"
        rows["log"].set_subtitle(log)
        h = snap.get("health")
        if h is not None and "health" in rows:
            rows["health"].set_subtitle(core.health_words(h))
        sx = snap.get("stats")
        if sx is not None and "stats" in rows:
            rows["stats"].set_subtitle("%s docked over %s sessions · %s power-ups · hottest %s °C%s" % (
                core._hms_short(sx.get("docked_s", 0)), sx.get("docks", 0), sx.get("boots", 0),
                sx.get("temp_max") if sx.get("temp_max") is not None else "—",
                " · counters in RAM only (dev jumper)" if sx.get("volatile") else ""))
        desk = snap.get("desk")
        if desk is not None and "net" in rows:
            opt = desk.get("opt") or {}
            rules = (snap.get("seat") or {}).get("rules") or {}
            net = []
            if opt.get("vlan"):
                net.append("VLAN " + opt["vlan"])
            if opt.get("mtu"):
                net.append("MTU " + opt["mtu"])
            net.append(("address " + opt["addr"]) if opt.get("addr") else "address from the network")
            rows["net"].set_subtitle(" · ".join(net))
            dns = opt.get("dns") or "from the network"
            if opt.get("domain"):
                dns += " · search " + opt["domain"]
            rows["dns"].set_subtitle(dns)
            rows["time"].set_subtitle(" · ".join(x for x in (opt.get("ntp"), opt.get("tz")) if x) or "from the network")
            rows["proxy"].set_subtitle((opt["proxy"] + (" — in use (your rule)" if rules.get("proxy") else " — not used by this laptop")) if opt.get("proxy") else "none")
            rows["vpn"].set_subtitle((opt["vpn"] + (" — connected when you dock (your rule)" if rules.get("vpn") else " — not used by this laptop")) if opt.get("vpn") else "none")
            rows["keys"].set_subtitle("%s" % desk.get("keys", 0))

    def _od_set(self, key, value):
        if self.use_service:
            r = core.ask("od_set", key=key, value=value)
            self.toast((r or {}).get("message", "no reply from the service"), 4)
        else:
            self.engine.od_set(key, value, done=lambda msg: self.toast(msg, 4))

    def _od_clear(self):
        if self.use_service:
            r = core.ask("od_clear")
            self.toast((r or {}).get("message", "no reply from the service"), 4)
            self.show("OpenDock")
        else:
            self.engine.od_clear(done=lambda msg: (self.toast(msg, 4), self.show("OpenDock")))

    def _od_export(self):
        if self.use_service:
            r = core.ask("od_export")
            self.toast("Exported: %s" % r["path"] if r and r.get("ok") else "Nothing to export", 6)
        else:
            try:
                p = self.engine.od_export()
                self.toast("Exported: %s" % p if p else "Nothing to export", 6)
            except Exception as ex:
                self.toast("Export failed: %s" % ex, 6)

    def _install_udev(self):
        self.engine.do(core.priv(["install-udev-rule"]), note="install the OpenDock udev rule",
                       after=lambda: (self.toast("Rule installed — replug the dock", 5), self.show("OpenDock")))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.win = None

    def do_activate(self):
        if self.win is None:
            use_service = service_available()
            engine = core.Engine(role="ui", marshal=lambda fn: GLib.idle_add(lambda: (fn(), False)[1]),
                                 automation=not use_service, opendock_link=not use_service)
            self.win = Window(self, engine, use_service)
        self.win.present()


def service_available():
    """Use the background service if it answers. If a unit is installed but not running,
    start it (first launch after install) and give it a moment."""
    if core.already_running():
        return True
    if os.path.exists("/usr/lib/systemd/user/dockpilot.service") and core.have("systemctl"):
        core.run(["systemctl", "--user", "start", "dockpilot.service"], timeout=5)
        for _ in range(10):
            if core.already_running():
                return True
            GLib.usleep(200000)
    return False


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["--version"]:
        print("DockPilot " + core.APP_VERSION)
        return 0
    if argv[:1] == ["--helper-check"]:        # used by the installer's tests: is the helper where we expect?
        print(core.HELPER if os.path.exists(core.HELPER) else "missing: " + core.HELPER)
        return 0 if os.path.exists(core.HELPER) else 1
    app = App()
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
