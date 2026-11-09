# Releasing DockPilot 1.2 — step by step

Three places, in this order: GitHub (source), a GitHub Release (the .deb), PyPI (pip). Then
install the .deb on your own laptop. About 15 minutes. Nothing here touches the dock.

The three files and what each is for:

    dockpilot-1.2-github.zip     → step 1. The repository contents, nothing built.
    dockpilot_1.2-1_all.deb      → step 2. Attached to the GitHub Release. (Also inside the dock:
                                   http://opendock.local/dockpilot.deb is this same file.)
    dockpilot-1.2.zip            → step 3. The repo plus build/dockpilot_1.2-1_all.deb and
                                   dist/dockpilot-1.2-py3-none-any.whl + dockpilot-1.2.tar.gz for PyPI.

Where GitHub stands today (checked 8 Nov 2026): main is 1.0.0 in the folder layout
(dockpilot/, packaging/, tests/, screenshots/), tagged v1.0.0, with the 1.0.0 .deb, .whl and
.tar.gz committed at the root. 1.2 keeps the layout, replaces every file, adds CHANGELOG.md and
packaging/dockpilot-symbolic.svg, and stops committing built files (.gitignore now excludes them;
the 1.0.0 ones stay reachable under the v1.0.0 tag). PyPI has 0.1.0 … 1.0.0.

---

## 1. GitHub — replace the repository contents

Either way below ends the same: history continues from what GitHub has, the 1.0.0 built files
disappear, no force-push.

**From your existing clone** (the one that tracks github.com/dinnerisserved/dockpilot):

    cd ~/dockpilot                     # wherever your clone lives
    git pull                           # so the push isn't refused with "fetch first"
    git rm -rq .                       # every tracked file goes (so the 1.0.0 .deb/.whl/.tar.gz really leave); .git stays
    unzip -q ~/Downloads/dockpilot-1.2-github.zip -d /tmp/dp12
    cp -a /tmp/dp12/dockpilot-github/. .   # the zip unpacks into a folder; its *contents* go next to .git
    git add -A
    git status --short

**Or straight from the unzipped folder**, no old clone needed:

    unzip -q ~/Downloads/dockpilot-1.2-github.zip -d ~/dp12
    cd ~/dp12/dockpilot-github
    git init -q && git remote add origin https://github.com/dinnerisserved/dockpilot.git
    git fetch -q origin
    git reset -q --soft origin/main    # "these files are the next version after what GitHub has"
    git add -A
    git status --short

What `git status` should show: `D` for `dockpilot-1.0.0-py3-none-any.whl`, `dockpilot-1.0.0.tar.gz`,
`dockpilot_1.0.0-1_all.deb`; `A` for `CHANGELOG.md` and `packaging/dockpilot-symbolic.svg`;
`M` for the rest. No `.deb`, `.whl`, `.tar.gz`, `build/`, `dist/` or `pip_build/` being added.

Commit as yourself, tag, push:

    git commit -F- <<'MSG'
    1.2: desk settings and your rules are stored in the dock; web page, firmware updates, keys as URLs

    For OpenDock firmware 1.0 (was 0.4). The dock stores the desk's tag and nine network options
    and gives them to whichever laptop plugs in; your rules and a saved configuration are stored
    in the dock under a key only your laptops know. Four tabs: Dock, Setup, Rules, Records. The
    dock has its own web page on the USB cable (http://opendock.local), takes firmware updates
    from a file and falls back to the previous firmware if the new one does not start, gives
    every one of its 64 keys a web address, and serves its own DockPilot .deb. opendock command
    line: desk, get, set, del, keys, export, import. New icons; the Quick Settings tile lights up
    for an OpenDock alone; the top-bar extension is enabled by the service, GNOME 45–50.
    Profiles and scenes removed. PROTOCOL.md covers firmware 0.5, 0.6 and 1.0.
    MSG
    git tag v1.2
    git push origin main
    git push origin v1.2

The repo page should now render the new README with the six screenshots.

## 2. GitHub Release — attach the .deb

github.com → dinnerisserved/dockpilot → **Releases** → **Draft a new release**:

- Tag: choose **v1.2** (it exists now, from the push above)
- Title: `DockPilot 1.2`
- Description (paste; it is Markdown):

      In 1.0.0 an OpenDock could tell you its name, its asset tag and what had happened to it.
      With firmware 1.0 it does a great deal more, and DockPilot 1.2 is the app for it. The
      generic dock tool — chip naming, native mode, offloads, automation — is unchanged.

      **The desk's settings are stored in the dock.** The dock keeps a desk tag and nine network
      options: VLAN, MTU, address, DNS, time server, proxy, VPN. Whichever laptop plugs in,
      DockPilot reads them, sets them on the wired connection, and removes them again when the
      laptop is unplugged. One text file holds a whole desk: *Export* writes it, *Apply from file*
      loads it into the next dock.

      **Your own settings follow you from desk to desk.** Wi-Fi off while docked, sound through
      the dock, lock or mute when you unplug, your own commands, and a saved configuration —
      screens, sound, VPN — put back every time you dock. They are stored in the dock, filed under
      a key that only your laptops know; the dock has no idea who you are. *Show my key* gives you
      six words. Type them into DockPilot on another laptop and both laptops get the same
      settings at any dock.

      **Four tabs: Dock, Setup, Rules, Records.** Health, temperature, power on the laptop port,
      lifetime counters, hours docked per day from the dock's own log, and the configuration
      record an inventory system receives.

      **Any browser can open the dock.** Firmware 1.0 adds a small network link on the USB cable.
      http://opendock.local shows the same information on Windows and Mac too, with nothing
      installed.

      **Firmware updates from a file.** Dock → Firmware → Update… in DockPilot, or on the dock's
      own page. The dock keeps two copies of its firmware. A new file goes into the copy that is
      not running, the dock restarts with it, and if it does not start properly the dock starts
      the old copy again by itself. There is no button on the case.

      **64 keys, each with its own web address.** Room for anything else you want to keep in the
      dock: `opendock set room 4.12`, or `curl http://opendock.local/key/room` from any device on
      the cable. Badge readers, room bookings, your own scripts.

      **The installer is in the dock.** Plug a new laptop in and download DockPilot from
      http://opendock.local/dockpilot.deb. Linux only for now.

      **Also.** The `opendock` command line has `desk`, `get`, `set`, `del`, `keys`, `export` and
      `import`. New app icon and top-bar icon. The Quick Settings tile lights up for an OpenDock
      alone. The top-bar extension is enabled by the background service itself and works on
      GNOME 45 to 50. Dock profiles and scenes have been removed; the dock replaces them.
      PROTOCOL.md covers firmware 0.5, 0.6 and 1.0, for anyone building a dock or a script.

      **Install.** `sudo apt install ./dockpilot_1.2-1_all.deb`, then log out and in once.
      Uninstall: `sudo apt remove dockpilot`. Also on PyPI: `pip install "dockpilot[opendock]"`.
      Full list: CHANGELOG.md.

      Docks, firmware files and the developer guide: https://opendocktech.com

- Attach `dockpilot_1.2-1_all.deb` (drag it into the assets box)
- **Publish release**

The README's "latest release" link now points at this.

## 3. PyPI — upload the wheel and sdist

Unzip `dockpilot-1.2.zip`; the two files are in its `dist/` folder, already built and tested
against the same source. You need twine and your PyPI token (the one you used for 1.0.0):

    pip install --upgrade twine            # if not present
    cd /path/to/dockpilot-1.2
    twine upload dist/dockpilot-1.2-py3-none-any.whl dist/dockpilot-1.2.tar.gz

Username `__token__`, password your token. Check: https://pypi.org/project/dockpilot/ shows 1.2.

If you ever rebuild the pip package from the repo:

    sh packaging/sync-pip.sh               # stages pip_build/ and runs python3 -m build → dist/
    twine upload dist/*

## 4. Your laptop — install the final .deb

    sudo apt install ./dockpilot_1.2-1_all.deb

Then **log out and back in once**. Check:

- Top-right Quick Settings shows the **Dock** tile, lit when the OpenDock is plugged in
- DockPilot → OpenDock → Dock tab: Firmware 1.0.0 with **Update…**, the Panel and Website rows,
  opendocktech.com in the footer
- Terminal: `opendock keys` lists your keys

To remove at any time: `sudo apt remove dockpilot`. Settings stay in `~/.config/dockpilot`.

## 5. Verify from a stranger's point of view (2 minutes)

- Open the repo page: README renders, six screenshots show, the "latest release" link opens v1.2
- `pip download dockpilot==1.2 --no-deps -d /tmp/x` succeeds
- Done.

---

### If something goes wrong

- **twine says the version already exists** — PyPI never accepts the same version twice. You
  would bump `version` in pyproject.toml and `APP_VERSION` in dockpilot/dockpilot_core.py to 1.2.1,
  run `sh packaging/sync-pip.sh` and `sh packaging/build-deb.sh`, and upload again.
- **git push is refused ("fetch first")** — someone (you, on another machine) pushed since your
  `git pull`. `git pull --rebase`, then push again. Never force-push.
- **apt complains about dependencies** — `sudo apt install -f` pulls them (python3-gi,
  gir1.2-adw-1, ethtool …). They are all in Ubuntu 24.04's own repositories.
- **the tile doesn't appear** — the service enables the extension at every start; by hand:
  `gnome-extensions enable dockpilot@opendock.io`, then log out and in once more.
