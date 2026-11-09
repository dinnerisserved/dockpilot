#!/bin/sh
# Builds dockpilot_<version>-1_all.deb from this checkout. Needs only dpkg-deb (and fakeroot
# if available). Everything DockPilot needs at run time comes from Ubuntu's own packages.
set -e
cd "$(dirname "$0")/.."
VER=$(python3 -c "import re;print(re.search(r'APP_VERSION = \"([^\"]+)\"', open('dockpilot/dockpilot_core.py').read()).group(1))")
B=build/deb
EXT=dockpilot@opendock.io
rm -rf build && mkdir -p $B/DEBIAN $B/usr/lib/dockpilot $B/usr/bin $B/usr/share/applications \
      $B/usr/share/icons/hicolor/scalable/apps $B/usr/share/icons/hicolor/symbolic/apps $B/usr/share/polkit-1/actions $B/usr/share/doc/dockpilot \
      $B/usr/lib/systemd/user $B/usr/lib/udev/rules.d $B/usr/share/gnome-shell/extensions/$EXT

install -m 644 dockpilot/dockpilot.py dockpilot/dockpilot_core.py dockpilot/opendock.py $B/usr/lib/dockpilot/
install -m 755 dockpilot/dockpilot_helper.py $B/usr/lib/dockpilot/
install -m 755 packaging/dockpilot.launcher $B/usr/bin/dockpilot
install -m 755 packaging/enable-extension.sh $B/usr/lib/dockpilot/
install -m 755 packaging/dockpilotd.launcher $B/usr/bin/dockpilotd
install -m 755 packaging/opendock.launcher $B/usr/bin/opendock
install -m 644 packaging/dockpilot.desktop $B/usr/share/applications/
install -m 644 packaging/dockpilot.svg $B/usr/share/icons/hicolor/scalable/apps/
install -m 644 packaging/dockpilot-symbolic.svg $B/usr/share/icons/hicolor/symbolic/apps/
install -m 644 packaging/com.opendock.dockpilot.policy $B/usr/share/polkit-1/actions/
install -m 644 packaging/dockpilot.service $B/usr/lib/systemd/user/
install -m 644 packaging/70-opendock.rules $B/usr/lib/udev/rules.d/
install -m 644 packaging/gnome-extension/metadata.json packaging/gnome-extension/extension.js $B/usr/share/gnome-shell/extensions/$EXT/
install -m 644 README.md PROTOCOL.md LICENSE $B/usr/share/doc/dockpilot/

cat > $B/DEBIAN/control <<CTRL
Package: dockpilot
Version: $VER-1
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, gir1.2-adw-1, python3-serial, ethtool, pkexec, iproute2, udev, libnotify-bin
Recommends: uhubctl, usbutils, network-manager, gnome-shell-extension-prefs
Maintainer: OpenDock <hello@opendocktech.com>
Homepage: https://github.com/dinnerisserved/dockpilot
Description: see and manage your laptop docking station
 DockPilot shows what is inside a USB-C dock (network chip, ports, power), lets
 you tune the dock's network adapter (offloads, speed, MAC pass-through, Wake-on-LAN)
 and talks to OpenDock docks over the open DockPilot Dock Protocol.
 .
 It looks like Ubuntu Settings (GTK 4 / libadwaita), runs a small background
 service so rules work with no window open, and adds a dock indicator to the
 GNOME top bar. Privileged changes go through a helper with a fixed list of
 operations, authorised through polkit.
CTRL
cat > $B/DEBIAN/postinst <<'PI'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q || true
    # the OpenDock serial port belongs to the logged-in user from now on
    command -v udevadm >/dev/null 2>&1 && { udevadm control --reload >/dev/null 2>&1 || true; udevadm trigger --subsystem-match=tty >/dev/null 2>&1 || true; }
    # background service: on for every user at their next login; also started for users logged in now
    command -v systemctl >/dev/null 2>&1 && systemctl --global enable dockpilot.service >/dev/null 2>&1 || true
    if command -v loginctl >/dev/null 2>&1; then
        for u in $(loginctl list-users --no-legend 2>/dev/null | awk '{print $2}'); do
            uid=$(id -u "$u" 2>/dev/null) || continue
            [ -S "/run/user/$uid/bus" ] || continue
            su -s /bin/sh "$u" -c "XDG_RUNTIME_DIR=/run/user/$uid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus systemctl --user daemon-reload; XDG_RUNTIME_DIR=/run/user/$uid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus systemctl --user restart dockpilot.service" >/dev/null 2>&1 || true
            # the top-bar indicator: enabled for the user; takes effect after logging out and in
            su -s /bin/sh "$u" -c "XDG_RUNTIME_DIR=/run/user/$uid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus /usr/lib/dockpilot/enable-extension.sh" >/dev/null 2>&1 || true
        done
    fi
fi
PI
cat > $B/DEBIAN/prerm <<'PR'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "deconfigure" ]; then
    command -v systemctl >/dev/null 2>&1 && systemctl --global disable dockpilot.service >/dev/null 2>&1 || true
    if command -v loginctl >/dev/null 2>&1; then
        for u in $(loginctl list-users --no-legend 2>/dev/null | awk '{print $2}'); do
            uid=$(id -u "$u" 2>/dev/null) || continue
            [ -S "/run/user/$uid/bus" ] || continue
            su -s /bin/sh "$u" -c "XDG_RUNTIME_DIR=/run/user/$uid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus systemctl --user stop dockpilot.service; XDG_RUNTIME_DIR=/run/user/$uid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus gnome-extensions disable dockpilot@opendock.io" >/dev/null 2>&1 || true
        done
    fi
fi
PR
cat > $B/DEBIAN/postrm <<'PO'
#!/bin/sh
set -e
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q || true
command -v udevadm >/dev/null 2>&1 && udevadm control --reload >/dev/null 2>&1 || true
rm -rf /usr/lib/dockpilot/__pycache__ 2>/dev/null || true
rmdir /usr/lib/dockpilot 2>/dev/null || true
PO
chmod 755 $B/DEBIAN/postinst $B/DEBIAN/prerm $B/DEBIAN/postrm
find $B -type d -exec chmod 755 {} +
fakeroot dpkg-deb --build --root-owner-group $B "build/dockpilot_${VER}-1_all.deb" 2>/dev/null || dpkg-deb --build --root-owner-group $B "build/dockpilot_${VER}-1_all.deb"
ls -la build/*.deb
