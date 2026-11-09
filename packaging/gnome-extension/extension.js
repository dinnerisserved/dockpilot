// DockPilot top-bar indicator (GNOME Shell 45+).
// Reads ~/.cache/dockpilot/status.json, written once a second by the dockpilotd user service.
// It never talks to hardware itself — it only shows the file and opens the app.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const STATUS = GLib.build_filenamev([GLib.get_user_cache_dir(), 'dockpilot', 'status.json']);

function readStatus() {
    try {
        const [ok, bytes] = GLib.file_get_contents(STATUS);
        if (!ok) return null;
        const s = JSON.parse(new TextDecoder().decode(bytes));
        // stale if the service stopped writing (> 10 s old)
        if (s.time && (Date.now() / 1000 - s.time) > 10) s.stale = true;
        return s;
    } catch (e) {
        return null;
    }
}

const DockToggle = GObject.registerClass(
class DockToggle extends QuickMenuToggle {
    _init() {
        super._init({
            title: 'Dock',
            subtitle: 'no dock',
            iconName: 'dockpilot-symbolic',
            toggleMode: false,
        });
        this.menu.setHeader('dockpilot-symbolic', 'DockPilot');
        this._nic = new PopupMenu.PopupMenuItem('', {reactive: false});
        this._od = new PopupMenu.PopupMenuItem('', {reactive: false});
        this.menu.addMenuItem(this._nic);
        this.menu.addMenuItem(this._od);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const open = new PopupMenu.PopupMenuItem('Open DockPilot');
        open.connect('activate', () => {
            try {
                const app = Gio.DesktopAppInfo.new('dockpilot.desktop');
                if (app) app.launch([], null);
                else GLib.spawn_command_line_async('dockpilot');
            } catch (e) { logError(e, 'dockpilot'); }
        });
        this.menu.addMenuItem(open);
        this.connect('clicked', () => open.activate(null));
        this.refresh();
    }

    refresh() {
        const s = readStatus();
        if (!s || s.stale) {
            this.subtitle = 'service off';
            this.checked = false;
            this._nic.label.text = 'DockPilot service is not running';
            this._od.label.text = '';
            return;
        }
        const nic = s.nic || {};
        const od = s.opendock || {};
        // a dock is "there" if either half is: its network chip, or an OpenDock brain on the serial port
        if (nic.iface) {
            const up = !!nic.carrier;
            this.checked = up;
            this.subtitle = up ? `${nic.speed || 'up'}` : 'link down';
            this._nic.label.text = up
                ? `${nic.iface} · ${nic.speed || ''} · ↓${(nic.rx_mbps || 0).toFixed(1)} ↑${(nic.tx_mbps || 0).toFixed(1)} Mb/s`
                : `${nic.iface} · link down`;
        } else if (od.connected) {
            this.checked = true;
            this.subtitle = od.docked === false ? 'OpenDock · idle' : 'OpenDock';
            this._nic.label.text = 'No network chip on this dock';
        } else {
            this.checked = false;
            this.subtitle = 'no dock';
            this._nic.label.text = 'No dock';
        }
        if (od.connected) {
            let t = `OpenDock ${od.name || od.serial || ''}`;
            if (od.temp_c !== undefined && od.temp_c !== null) t += ` · ${od.temp_c} °C`;
            if (od.docked !== undefined) t += od.docked ? ' · docked' : ' · idle';
            this._od.label.text = t;
        } else {
            this._od.label.text = '';
        }
    }
});

const DockIndicator = GObject.registerClass(
class DockIndicator extends SystemIndicator {
    _init() {
        super._init();
        this._toggle = new DockToggle();
        this.quickSettingsItems.push(this._toggle);
        // The icon in the top bar itself: shown only while docked at an OpenDock whose
        // seat has the "Show icon in top bar" rule on (status.opendock.tile).
        this._icon = this._addIndicator();
        this._icon.icon_name = 'dockpilot-symbolic';
        this._icon.visible = false;
    }
    refresh() {
        this._toggle.refresh();
        const s = readStatus();
        const od = (s && !s.stale && s.opendock) || {};
        this._icon.visible = !!od.tile;
    }
});

export default class DockPilotExtension extends Extension {
    enable() {
        this._indicator = new DockIndicator();
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 2, () => {
            this._indicator.refresh();
            return GLib.SOURCE_CONTINUE;
        });
        this._indicator.refresh();
    }
    disable() {
        if (this._timer) { GLib.source_remove(this._timer); this._timer = null; }
        if (this._indicator) {
            this._indicator.quickSettingsItems.forEach(i => i.destroy());
            this._indicator.destroy();
            this._indicator = null;
        }
    }
}
