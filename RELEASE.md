# Releasing DockPilot 1.3 — step by step

Three places, in this order: GitHub (source), a GitHub Release (the .deb), PyPI (pip). Then
install the .deb on your own laptop. About 15 minutes. Nothing here touches the dock.

**What 1.3 is.** The DockPilot that works with everything the OpenDock firmware build of
17 November 2026 can do: the VPN, changes made on the dock's page acted on at once, a switch on
every desk option. `dockpilot --version` says `DockPilot 1.3 (build of 17 Nov 2026)`.

The files and what each is for:

    dockpilot-1.3-github.zip     → step 1. The repository contents, nothing built.
    dockpilot_1.3-1_all.deb      → step 2 (attached to the GitHub Release) and step 4 (your laptop).
    dockpilot-1.3-py3-none-any.whl + dockpilot-1.3.tar.gz   → step 3. For PyPI.

Where GitHub and PyPI stand today (checked 17 Nov 2026, 23:30): GitHub main is 1.2, tags v0.2.0,
v0.3.0, v1.0.0, v1.2. PyPI has 0.1.0 … 1.2. Neither has a 1.3.

One thing to know: a dock with the 17 Nov firmware carries its own copy of DockPilot at
`http://opendock.local/dockpilot.deb`. That copy is labelled 1.2 and the dock says "1.2". It has
the same program code as 1.3. The firmware was not rebuilt for the new number.

---

## 1. GitHub — replace the repository contents

**From your existing clone** (the one that tracks github.com/dinnerisserved/dockpilot):

    cd ~/dockpilot                     # wherever your clone lives
    git pull                           # so the push isn't refused with "fetch first"
    git rm -rq .                       # every tracked file goes; .git stays
    unzip -q dockpilot-1.3-github.zip -d /tmp/dp13
    cp -a /tmp/dp13/dockpilot-github/. .   # the zip unpacks into a folder; its *contents* go next to .git
    git add -A
    git status --short

**Or straight from the unzipped folder**, no old clone needed:

    unzip -q dockpilot-1.3-github.zip -d ~/dp13
    cd ~/dp13/dockpilot-github
    git init -q && git remote add origin https://github.com/dinnerisserved/dockpilot.git
    git fetch -q origin
    git reset -q --soft origin/main    # "these files are the next version after what GitHub has"
    git add -A
    git status --short

What `git status` should show, and nothing else:

    M  CHANGELOG.md            M  dockpilot/dockpilot.py         M  pyproject.toml
    M  PROTOCOL.md             M  dockpilot/dockpilot_core.py    A  tests/fake_nm.py
    M  README.md               M  dockpilot/opendock.py          M  tests/test_desk.py
    M  RELEASE.md              M  packaging/dockpilot.desktop    A  tests/test_vpn.py

Twelve lines. No `.deb`, `.whl`, `.tar.gz`, `build/`, `dist/` or `pip_build/`.

Commit as yourself, tag, push:

    git commit -F- <<'MSG'
    1.3: VPN chosen on the dock's page; changes made on the page are acted on at once

    For OpenDock firmware 1.0, build of 17 Nov 2026. The dock's own page is now where things are
    changed; DockPilot carries them out. DockPilot tells the dock which VPNs this laptop has
    (names only) and which is up; the page lists them, stores the seat's choice, and DockPilot
    connects and disconnects through NetworkManager. "Add servers" on the page opens DockPilot's
    file window (opendock://vpn-import) and imports WireGuard .conf files or a provider's .zip.
    The dock counts its stored changes; DockPilot polls once a second and does what differs, with
    no waiting for the next dock. Every desk option has a switch; MTU, search domain, time server
    and time zone are gone; new rule "drop the VPN when I undock"; the saved configuration no
    longer holds VPNs. Window wording matches the page. PROTOCOL.md describes SET USE, SET VPN,
    SET VPNON, the VPN reports, GET VPN and STATUS rev/host/host_seat.
    MSG
    git tag v1.3
    git push origin main
    git push origin v1.3

## 2. GitHub Release — attach the .deb

github.com → dinnerisserved/dockpilot → **Releases** → **Draft a new release**:

- Tag: choose **v1.3** (it exists now, from the push above)
- Title: `DockPilot 1.3`
- Tick **Set as the latest release**
- Description (paste; it is Markdown):

      DockPilot 1.3 is the version that works with everything the OpenDock firmware of
      17 November 2026 can do. If you have no OpenDock, nothing changes for you.

      **The dock's page is where you change things.** Open http://opendock.local while DockPilot
      is running: switch a rule, set an option, pick a VPN. DockPilot notices within a second
      and does it on the laptop. No waiting until the next time you dock.

      **VPN, chosen at the desk.** DockPilot tells the dock the names of the VPNs your laptop
      has. The page lists them, grouped by provider. Select one, press Connect, and DockPilot
      brings it up through NetworkManager — and again every time you dock at that desk.
      Disconnect takes it down. *Add servers* takes WireGuard `.conf` files or a provider's
      `.zip`. The dock keeps only the name of your choice. Your keys never leave the laptop.

      **A switch on every desk option.** VLAN, IP address, DNS servers and proxy can each be
      switched off without losing the value. They apply to the dock's wired connection while
      docked, never to Wi-Fi, and are undone when you unplug. MTU, search domain, time server
      and time zone are gone.

      **Rules.** New: *Drop the VPN when I undock*. The saved configuration no longer holds
      VPNs; the choice on the page decides.

      **Needs** the OpenDock firmware build of 17 November 2026 or later for the VPN and for
      changes from the page. Older firmware keeps working as before.

      **Install.** `sudo apt install ./dockpilot_1.3-1_all.deb`, then log out and in once
      (coming from 1.2: `systemctl --user restart dockpilot` is enough). Uninstall:
      `sudo apt remove dockpilot`. Also on PyPI: `pip install -U "dockpilot[opendock]"`.
      Full list: CHANGELOG.md.

- Attach `dockpilot_1.3-1_all.deb` (drag it into the assets box)
- **Publish release**

## 3. PyPI — upload the wheel and sdist

The two files are already built from this source and tested. You need twine and your PyPI token
(the one you used for 1.2):

    pip install --upgrade twine            # if not present
    twine upload dockpilot-1.3-py3-none-any.whl dockpilot-1.3.tar.gz

Username `__token__`, password your token. Check: https://pypi.org/project/dockpilot/ shows 1.3.

If you ever rebuild the pip package from the repo:

    sh packaging/sync-pip.sh               # stages pip_build/ and runs python3 -m build → dist/
    twine upload dist/*

## 4. Your laptop — install 1.3

    sudo apt install ./dockpilot_1.3-1_all.deb
    systemctl --user restart dockpilot
    dockpilot --version        # DockPilot 1.3 (build of 17 Nov 2026)

Close and reopen the DockPilot window if it was open.

## 5. Verify from a stranger's point of view (2 minutes)

- Open the repo page: README renders, the "latest release" link opens v1.3
- `pip download dockpilot==1.3 --no-deps -d /tmp/x` succeeds
- Done.

---

### If something goes wrong

- **twine says the version already exists** — PyPI never accepts the same version twice. Bump
  `version` in pyproject.toml and `APP_VERSION` in dockpilot/dockpilot_core.py to 1.3.1, run
  `sh packaging/sync-pip.sh` and `sh packaging/build-deb.sh`, and upload again.
- **git push is refused ("fetch first")** — someone (you, on another machine) pushed since your
  `git pull`. `git pull --rebase`, then push again. Never force-push.
- **apt complains about dependencies** — `sudo apt install -f` pulls them (python3-gi,
  gir1.2-adw-1, ethtool …). They are all in Ubuntu 24.04's own repositories.
- **the tile doesn't appear** — the service enables the extension at every start; by hand:
  `gnome-extensions enable dockpilot@opendock.io`, then log out and in once more.
