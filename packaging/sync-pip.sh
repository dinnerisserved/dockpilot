#!/bin/sh
# Stages the pip package under pip_build/ from the same files the .deb ships, then builds it.
# One command for a release:
#     sh packaging/sync-pip.sh && twine upload dist/*
set -e
cd "$(dirname "$0")/.."
P=pip_build/dockpilot_pkg
rm -rf pip_build dist && mkdir -p $P/packaging/gnome-extension
cp dockpilot/*.py $P/
cp packaging/70-opendock.rules packaging/dockpilot.service packaging/com.opendock.dockpilot.policy packaging/dockpilot.desktop packaging/dockpilot.svg packaging/dockpilot-symbolic.svg $P/packaging/
cp packaging/gnome-extension/metadata.json packaging/gnome-extension/extension.js $P/packaging/gnome-extension/
cat > $P/__init__.py <<'PY'
"""dockpilot — the pip packaging of DockPilot. Same files as the .deb; see README."""
PY
cat > $P/__main__.py <<'PY'
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def main():
    import dockpilot
    sys.exit(dockpilot.main())


def daemon():
    import dockpilot_core
    sys.exit(dockpilot_core.service_main())


def cli():
    import opendock
    sys.exit(opendock.main())


if __name__ == "__main__":
    main()
PY
python3 -m build -q
ls -la dist/
