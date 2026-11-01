#!/bin/sh
# Adds the DockPilot indicator to the user's enabled GNOME extensions. Writes the setting
# directly, so it works even before the Shell has noticed the new extension folder
# (`gnome-extensions enable` refuses until the next login). Takes effect at the next login.
UUID=dockpilot@opendock.io
command -v gsettings >/dev/null 2>&1 || exit 0
cur=$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null) || exit 0
case "$cur" in *"'$UUID'"*) exit 0 ;; esac
case "$cur" in
    "@as []"|"[]") new="['$UUID']" ;;
    *) new=$(printf '%s' "$cur" | sed "s/]\$/, '$UUID']/") ;;
esac
gsettings set org.gnome.shell enabled-extensions "$new"
