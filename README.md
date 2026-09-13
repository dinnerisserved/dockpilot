# DockPilot

A vendor-neutral **admin console for USB-C / Thunderbolt docks and hubs on Linux.**

Docks hide most of what their hardware can actually do, and on Linux no vendor gives you a tool
to change that. DockPilot works out what's inside your dock, unlocks what it safely can, shows you
what's really going on, and lets you control it.

![DockPilot dashboard](screenshots/01-dashboard.png)

## Screenshots

| | |
|---|---|
| **Dashboard** — live throughput, link health, reachability, chip identity | ![Dashboard](screenshots/01-dashboard.png) |
| **Native mode** — detect the chip and unlock the manufacturer's driver | ![Native mode](screenshots/02-native-mode.png) |
| **Wake-on-LAN** — arm the NIC and send magic packets to other machines | ![Wake-on-LAN](screenshots/03-wake-on-lan.png) |
| **USB topology** — a live map of the dock's hubs, ports and devices | ![Topology](screenshots/04-topology.png) |
| **Devices & power** — enable/disable individual devices behind the dock | ![USB & Power](screenshots/05-usb-power.png) |
| **Automation** — rules that fire when the dock connects or disconnects | ![Automation](screenshots/06-automation.png) |

---

## What it does

- **Unlocks hidden features.** Many dock network chips ship in a limited plug-and-play mode.
  DockPilot detects this and offers a one-click, reversible switch to the manufacturer's native
  driver — then checks it actually worked, and puts things back if it didn't.
- **Wake-on-LAN.** Wake your machine over the dock's wired link, and send magic packets to wake
  other machines on your network.
- **Live dashboard.** Throughput, link health, latency, and what chip your dock actually contains.
- **Dock map.** A live diagram of the dock's internals — its hubs, ports, and what's plugged into
  each — scoped to the dock, not your laptop.
- **Per-device control.** Enable or disable individual devices behind the dock; switch port power
  where the hardware supports it.
- **Macros.** Run your own commands when the dock connects or disconnects. No assumptions about
  your setup — you write the commands; five editable templates get you started, and macros can be
  scoped to a specific dock.
- **System Snapshot.** One button writes a single diagnostic file (chips, drivers, USB layout, link
  stats, logs) with IPs and hostname redacted — ready to attach to a bug report.
- **Automation.** Turn Wi-Fi off when you dock, mirror your laptop's MAC, switch audio to the dock.
- **Tidy-up tools.** Clean up leftover network profiles, inspect chip configuration (read-only),
  and tune link speed and offloads where the hardware allows.

## Tested hardware

Every device below was tested on a real machine.

| Device | NIC chip | Ships as | Native mode result |
|---|---|---|---|
| Wavlink WL-UMD05 REV.C | ASIX **AX88179A** | `cdc_ncm` | ✅ unlocks, link comes up |
| UGREEN Revodok Pro (10-in-1) | ASIX **AX88179B** | `cdc_ncm` | ⚠️ engages, but wired link won't come up — auto-reverts, diagnosed |
| TP-Link UE300C | ASIX **AX88179B** | `cdc_ncm` | ⚠️ same as above — diagnosed, reverts |
| Anker USB-C Gigabit adapter | ASIX **AX88179B** | `cdc_ncm` | ⚠️ same as above — diagnosed, reverts |
| uni USB-C Hub 6-in-1 | Realtek **RTL8153** | `r8152` | ✅ already native out of the box |
| TP-Link UE302C (2.5G) | Realtek **RTL8156** | `r8152` | ✅ already native; advertises 2500baseT/Full |
| MOKiN / C-Smartlink DK1903A (Thunderbolt 4, 12-in-1) | Realtek **RTL8156B** | `r8152` | ✅ already native; works fully over Thunderbolt |
| ASIX AX88772B USB-A adapter (10/100) | ASIX **AX88772B** | `asix` | ✅ single USB config — nothing to unlock; correctly offers no native mode |
| Belkin USB-C to 2.5GbE (USB-IF certified) | Realtek **RTL8156B** | `r8152` | ✅ already native; **links at a real 2500 Mb/s** |
| Lemorele 10-in-1 | *(no NIC)* | — | ✅ graceful: "no network chip", topology still drawn |

---

## Install & run

Requires Python 3 with Tk (`python3-tk` on Debian/Ubuntu if your Python lacks it).

### Option 1 — install via pip (recommended)

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install dockpilot
dockpilot
```

### Option 2 — run from source

```bash
git clone https://github.com/dinnerisserved/dockpilot.git && cd dockpilot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 dockpilot.py
```

Optional system tools unlock extra panels (each auto-hides if absent): `ethtool` (link tuning, WoL, EEPROM), `usbutils`, `uhubctl` (per-port power), `network-manager`/`nmcli` (Wi-Fi auto-switch, reconnect), `pactl` (audio).

```bash
sudo apt install ethtool usbutils uhubctl network-manager  # all optional
```

Privileged actions (mode switching, WoL) prompt once via `pkexec` (a polkit dialog), not `sudo` — you never run the whole app as root.

## Configuration

Config lives in `./config/` next to the script — nothing is written to your home directory,
and **no personal data ships with the project**:

- `config/automation.json` — your connect/disconnect rules (created when you set them)
- `config/dock_memory.json` — remembered per-dock native-mode verdicts
- `config/dock_state.json` — captured original MAC (so "reset MAC" always works)

---

## Notes

Detail for people who want it — skip unless you're digging in.

**ASIX vs Realtek.** Realtek chips (RTL8153 / RTL8156) run their native driver happily — they
usually arrive that way already. Current-generation **ASIX AX88179B** parts are a different story:
the native driver binds, but the Ethernet link never comes up. Only the older **AX88179A** works.
Four devices from three brands reproduce this here, and it's independently reported on the Linux
kernel mailing list ("ax88179_178a … Link status is: 0"). DockPilot unlocks the chips that work and,
for the ones that don't, engages native mode, spots the dead link, reverts, and remembers the
verdict so it won't retry blindly.

**How native-mode unlock works.** USB devices can present multiple *configurations*. Many
ASIX/Realtek dock NICs ship on a generic CDC config (driven by `cdc_ncm`/`cdc_ether`), which
hides the chip's advanced features. Their native/vendor config exposes everything, driven by
`ax88179_178a` (ASIX) or `r8152` (Realtek). DockPilot switches by writing the device's
`bConfigurationValue` in sysfs, then re-checks the driver and link, and reverts if the link
doesn't come up. Fully reversible — unplugging the dock always returns it to standard mode.

**`r8152-cfgselector`.** Recent kernels ship a helper that performs the same
`bConfigurationValue` switch for Realtek NICs automatically at plug-in. So modern Realtek
devices increasingly arrive already-native, and DockPilot's unlock is most valuable on
**ASIX**, where no such helper exists.

**Thunderbolt docks.** The TB4 dock tested tunnels **USB** over Thunderbolt rather than
presenting a PCIe NIC, so DockPilot manages it exactly like a USB-C dock — no special
handling needed. It's also the only dock tested where `fwupd` sees dock-side firmware (Intel
USB3.0 Hub, USB4 Retimer); cheap USB-C docks show nothing. It needs a genuine Thunderbolt
cable — a normal USB-C cable silently drops it to USB 2.0 with no error.

**2.5G links.** An RTL8156 only *links* at 2.5G if the other end is 2.5G too; on gigabit
infrastructure it negotiates down to 1000Mb/s. The Belkin adapter confirmed a real 2500Mb/s link.
Note that `ethtool`'s advertised-modes output isn't always trustworthy — one dock claimed
10baseT-only on a 2.5G chip while sysfs correctly reported the real speed, so DockPilot reads
speed from sysfs.

**EEPROM in practice.** Across nine devices, only one exposes a readable EEPROM: the older ASIX
**AX88772B**, where the layout is fully decodable — MAC at `0x0008`, VID/PID at `0x0048`, vendor
strings at `0xC0`. Everything else reads back as all-`FF` (the chip uses one-time eFuse) or reports
no EEPROM access at all. Writing was rejected on every device tested; on the AX88772B the driver
logs *"Failed to enable software MII access"*, so `ethtool -E` can't reach the write path.
DockPilot therefore treats EEPROM as **read-only**, which is all it ever claimed to do.

**Built on.** The Linux `ax88179_178a` and `r8152` drivers; Realtek's own udev rule for
forcing native mode; `uhubctl` for per-port power; `ethtool`, `fwupd`, and the sysfs USB/net
interfaces. What DockPilot adds is the synthesis: detect the chip, attempt the unlock, verify
it works, degrade honestly when it doesn't, and present it in a GUI.

## License

See [LICENSE](LICENSE).

## Disclaimer

DockPilot changes low-level USB/network device state on hardware you own. Mode switches are
reversible and the app is careful, but you use it at your own risk. It never writes firmware
or EEPROM.
