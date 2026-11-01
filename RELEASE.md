# Releasing DockPilot 1.0.0 — step by step

Everything below is done from this unzipped folder on your Ubuntu laptop. Three places get
updated, in this order: GitHub (source), GitHub Releases (the .deb), PyPI (pip). Then install
the .deb on your own machine. About 15 minutes.

What is in this folder:

    dockpilot/                 the application (4 Python files)
    packaging/                 .deb build, polkit policy, udev rule, systemd unit, GNOME extension
    tests/                     65 tests
    screenshots/               the six PNGs the README shows
    README.md  PROTOCOL.md  LICENSE  pyproject.toml  requirements.txt  .gitignore
    dockpilot_1.0.0-1_all.deb              → GitHub Release asset (not committed)
    dockpilot-1.0.0-py3-none-any.whl       → PyPI (not committed)
    dockpilot-1.0.0.tar.gz                 → PyPI (not committed)
    RELEASE.md                             this file (commit it; it's useful later)

---

## 1. GitHub — replace the repository contents

The repo currently has the old single-file layout (dockpilot.py, opendock.py at the root).
It becomes the folder layout above. Your existing clone:

    cd ~/dockpilot                      # wherever your clone lives
    git pull

Remove the old layout and copy the new one in (replace /path/to/dockpilot-1.0.0 with where
you unzipped):

    git rm -rq dockpilot.py opendock.py requirements.txt screenshots README.md PROTOCOL.md
    rm -rf dockpilot packaging tests screenshots
    cp -r /path/to/dockpilot-1.0.0/{dockpilot,packaging,tests,screenshots} .
    cp /path/to/dockpilot-1.0.0/{README.md,PROTOCOL.md,LICENSE,pyproject.toml,requirements.txt,.gitignore,RELEASE.md} .

Check nothing unwanted is going in (no .deb, .whl, .tar.gz, build/, dist/, pip_build/):

    git add -A
    git status --short

Commit, tag, push:

    git commit -m "1.0.0: Ubuntu-native DockPilot — final release"
    git tag v1.0.0
    git push
    git push --tags

## 2. GitHub Release — attach the .deb

On github.com → your repo → **Releases** → **Draft a new release**:

- Tag: choose **v1.0.0** (it exists now)
- Title: `DockPilot 1.0.0`
- Description (paste):

      Final release. GTK 4 / libadwaita window that looks like Ubuntu Settings, background
      service, GNOME top-bar tile, OpenDock serial access without group changes, ASIX and
      Realtek chip naming from the vendor driver tables.

      Install:  sudo apt install ./dockpilot_1.0.0-1_all.deb   then log out and in once.
      Uninstall: sudo apt remove dockpilot

- Attach `dockpilot_1.0.0-1_all.deb` (drag it into the assets box)
- **Publish release**

The README's install section links to "latest release", so that link now resolves.

## 3. PyPI — upload the wheel and sdist

Both files are already built and tested. You need twine and your PyPI token (the same one
you used for 0.4.0):

    pip install --upgrade twine          # if not present
    cd /path/to/dockpilot-1.0.0
    twine upload dockpilot-1.0.0-py3-none-any.whl dockpilot-1.0.0.tar.gz

Username `__token__`, password your token. Check: https://pypi.org/project/dockpilot/ shows 1.0.0.

If you ever rebuild the pip package yourself from the repo:

    sh packaging/sync-pip.sh             # stages pip_build/ and runs python3 -m build → dist/
    twine upload dist/*

## 4. Your laptop — install the final .deb

    sudo apt install ./dockpilot_1.0.0-1_all.deb

Then **log out and back in once** (the top-bar tile loads with the Shell). Check:

- Top-right menu shows a **Dock** tile with the link speed
- DockPilot in the app menu opens; Native Mode page reads "Wake-on: disarmed" (or armed), not "pg"
- Dashboard has no "NIC mode" row with stray text
- Logs → Background service shows the service log (no status row)

To remove at any time: `sudo apt remove dockpilot`. Settings stay in `~/.config/dockpilot`.

## 5. Verify from a stranger's point of view (2 minutes)

- Open the GitHub repo page: README renders, six screenshots show, "latest release" link works
- `pip download dockpilot==1.0.0 --no-deps -d /tmp/x` succeeds
- Done. This is the last release; the README says so.

---

### If something goes wrong

- **twine says the version already exists** — PyPI never accepts the same version twice. You
  would bump `version` in pyproject.toml and `APP_VERSION` in dockpilot/dockpilot_core.py to
  1.0.1, rebuild with `sh packaging/sync-pip.sh`, and upload again.
- **apt complains about dependencies** — `sudo apt install -f` pulls them (python3-gi,
  gir1.2-adw-1, ethtool …). They are all in Ubuntu 24.04's own repositories.
- **the tile doesn't appear after re-login** — `gnome-extensions enable dockpilot@opendock.io`
  now works because the Shell has seen the folder; then log out and in once more.
