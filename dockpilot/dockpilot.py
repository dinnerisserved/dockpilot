#!/usr/bin/env python3
"""
DockPilot — see what your USB-C dock is doing and tell it what to do.

This file is the window: GTK 4 and libadwaita, so it looks and behaves like Ubuntu's own
Settings app. All the logic lives in dockpilot_core.py (which, run on its own, is the
background service). The only thing that ever runs as root is dockpilot_helper.py.
opendock.py is the protocol client for docks with the OpenDock management board.

Run:  python3 dockpilot.py        (the .deb installs it as `dockpilot`)
"""
import os
import re
import sys
import threading

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
        engine.on("opendock", lambda what: self._refresh_if("OpenDock"))
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
        page = Adw.PreferencesPage()
        builder = {"Dashboard": self.page_dashboard, "Network": self.page_network, "Native Mode": self.page_native,
                   "USB & Power": self.page_usb, "Topology": self.page_topology, "Audio": self.page_audio,
                   "Automation": self.page_automation, "Logs": self.page_logs, "OpenDock": self.page_opendock}[name]
        builder(page)
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
        g = group("When the dock connects or disconnects", "Each rule fires when the dock's wired link actually goes up or down. Ticking a switch takes effect at the next such event."
                  + ("" if self.use_service else "") )
        for key, title, sub, tool in (("wifi", "Wi-Fi off when docked", "Turned back on when the dock disconnects.", "nmcli"),
                                      ("audio", "Audio to the dock", "Switch the audio output to the dock on connect.", "pactl")):
            sr = Adw.SwitchRow()
            sr.set_title(title)
            sr.set_subtitle(sub)
            sr.set_active(e.rules[key])
            if not core.have(tool):
                sr.set_sensitive(False)
                sr.set_subtitle("Needs %s." % tool)
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
        e = self.engine
        g = group("OpenDock", "A dock with the management chip inside speaks a small open protocol over a USB serial port. "
                             "It says what it is, keeps a few settings, and remembers what happened to it.")
        page.add(g)
        if core.opendock is None:
            g.add(row("Missing", "opendock.py is not beside DockPilot.", prop=False))
            return
        if core.opendock.serial is None and not self.use_service:
            g.add(row("Needs pyserial", "pip install pyserial, then restart DockPilot", prop=False))
            return
        snap = e.od_snap or {}
        if not snap.get("connected"):
            why = snap.get("error") or "Plug one in — it is found within a few seconds."
            if why.startswith("no reply") or why.startswith("port write failed"):
                why = "The dock stopped answering — unplugged? It is picked up again within a few seconds."
            g.add(row("No OpenDock found", why, prop=False))
            hint = core.dialout_hint()
            if hint:
                g.add(button_row("Serial port permission", "Install the udev rule once and the dock is yours without any group change.",
                                 button("Allow access", self._install_udev)))
            return
        self.widgets["od_rows"] = {}
        for k, t in (("serial", "Serial"), ("model", "Model"), ("name", "Name"), ("assettag", "Asset tag"), ("site", "Site"),
                     ("docked", "Laptop docked"), ("temp", "Temperature"), ("uptime", "Uptime"), ("log", "Log")):
            r = row(t, "—")
            self.widgets["od_rows"][k] = r
            g.add(r)
        self._opendock_tick()

        st = snap.get("status") or {}
        g2 = group("Settings", "Written into the dock's own flash; up to 64 bytes each. The dock refuses to rewrite the same field more than once every 10 s — if it says wait, DockPilot waits.")
        for key, label in (("name", "Name"), ("assettag", "Asset tag"), ("site", "Site")):
            er = Adw.EntryRow()
            er.set_title(label)
            er.set_text(str(st.get(key, "") or ""))
            er.set_show_apply_button(True)
            er.connect("apply", lambda w, k=key: self._od_set(k, w.get_text()))
            g2.add(er)
        page.add(g2)

        g3 = group("Event log", "What the dock remembers: power-ups, docking and undocking, temperature, settings changes.")
        tv = Gtk.TextView()
        tv.set_editable(False)
        tv.set_monospace(True)
        entries = snap.get("log") or []
        tv.get_buffer().set_text("\n".join(core.opendock.describe(x) for x in entries[-200:]) or "(nothing yet)")
        sw = Gtk.ScrolledWindow()
        sw.set_min_content_height(240)
        sw.set_child(tv)
        sw.add_css_class("card")
        g3.add(sw)
        g3.add(button_row("Export or clear", "Export writes a JSON file to your home folder. Clear hides the dock's history; the copy kept here is cleared too.",
                          button("Export JSON", self._od_export),
                          button("Clear dock log", lambda: self.confirm("Clear dock log?", "Hide everything the dock has logged so far?", "Clear", self._od_clear, destructive=True), kind="destructive-action")))
        page.add(g3)

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
        rows["name"].set_subtitle(s.get("name") or "—")
        rows["assettag"].set_subtitle(s.get("assettag") or "—")
        rows["site"].set_subtitle(s.get("site") or "—")
        docked = ("yes, for %s" % core._hms(s.get("docked_s", 0))) if s.get("docked") else "no"
        if s.get("vbus_v") is not None:
            docked += " · VBUS sense %.2f V" % s["vbus_v"]
        rows["docked"].set_subtitle(docked)
        rows["temp"].set_subtitle("%s °C" % s.get("temp_c"))
        rows["uptime"].set_subtitle("%s · clock %s" % (core._hms(s.get("uptime_s", 0)), "synced" if s.get("time_synced") else "not synced"))
        log = "%s entries on the dock (newest #%s), %s kept here" % (s.get("log_entries"), s.get("log_last"), snap.get("log_count", 0))
        if s.get("log_volatile"):
            log += " · dev jumper fitted: settings cannot be written"
        rows["log"].set_subtitle(log)

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
