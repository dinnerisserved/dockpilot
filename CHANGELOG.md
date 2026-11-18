# Changelog

## 1.3 — 17 November 2026

For the OpenDock firmware build of 17 November 2026 (still firmware 1.0.0). With older firmware 1.3 behaves
like 1.2. Nothing changes for docks without an OpenDock board.

- **VPN, chosen on the dock's page.** DockPilot tells the dock which VPNs this laptop has (names only,
  kept in the dock's RAM) and which one is up; the dock's page lists them and stores the seat's choice;
  select one and press Connect, and DockPilot brings it up now and every time you dock; Disconnect takes it down.
  WireGuard first; any VPN NetworkManager already has is listed too. The keys never leave the laptop.
- **Add servers** from the page: the page opens `opendock://vpn-import`, DockPilot (registered for the
  scheme) opens its own file window, and the chosen `.conf` files or provider `.zip` go into
  NetworkManager — named after the file, grouped by provider, never connecting by themselves.
  Only the action's name is read from an `opendock://` link; nothing else in it is used.
- **Changes made in the dock are acted on at once.** The dock counts its stored changes (`STATUS` `rev`);
  DockPilot polls once a second and, when it moves, reads the desk and its seat again and does what
  differs: a rule switched, an option set or switched off, the VPN choice. No waiting for the next dock.
- New rule from the dock: **Drop the VPN when I undock** (off = the VPN stays with you).
- The saved configuration no longer holds or restores VPNs (the choice above decides).
- Window: "At this desk" is gone, Dock Name, Activity, a switch on every desk option, a VPN row that
  opens the dock's page. The log no longer shows "desk handed to a laptop".
- `OPENDOCK_PORTS=/dev/ttyX[,…]` makes DockPilot ask exactly those serial ports (tests, odd set-ups).
- A dock with the 17 Nov firmware carries its own copy of DockPilot (`http://opendock.local/dockpilot.deb`).
  That copy is labelled 1.2 and has the same program code as 1.3.

## 1.2 — November 2026

### OpenDock (firmware 0.6 → 1.0)
- Four tabs: Dock / Setup / Rules / Records. Desk tag, nine validated desk options, key storage
  (64 keys), Export / Apply from file.
- Seat key: six words; your rules, own commands and saved configuration live on the dock under a key
  only your laptops know. Show my key / Use a key from another laptop / Forget.
- Rules act when switched, not only at dock/undock; switches never block the window.
- Desk options applied to the wired connection while docked and undone at undock; snapshot/restore.
- `opendock` command line: `desk`, `get`, `set`, `del`, `keys`, `export`, `import` (on .deb and pip).
- Dock tab shows the dock's own page (`http://opendock.local`) and opendocktech.com;
  **Firmware → Update…** sends a new firmware file to the dock (two slots, automatic fallback).
- The dock's own network link is recognised and ignored when looking for its Ethernet chip.
- Health, stats, notes and dock events (firmware 0.5); usage timeline; IT report record.

### App
- New app icon and symbolic top-bar icon; the Dock tile in Quick Settings lights for an OpenDock alone.
- Top-bar extension allowed on GNOME 45–50 and enabled by the service itself at every start.
- Contact: hello@opendocktech.com.

### Removed
- Dock profiles and scenes (replaced by the desk-in-the-dock model).

### Docs
- PROTOCOL.md: firmware 0.5, 0.6 and 1.0 (desk commands, web face, `/update`, `X-OpenDock` header,
  key URLs, `fw_update` event). README rewritten for OpenDock.
