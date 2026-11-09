# Changelog

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
