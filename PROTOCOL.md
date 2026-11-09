# The DockPilot Dock Protocol (v1)

A dock that can be asked questions. Put a small chip in a dock, give it a USB serial port, and
have it answer plain-text commands. DockPilot — and anyone's script — can then ask the dock who
it is, keep a name and an asset tag on it, store the desk's settings in it, and read back what
happened to it while nobody was looking.

It is open. Anyone may build a dock that speaks it, or a program that talks to one. The reference
dock, its firmware and this document are by [opendocktech.com](https://opendocktech.com).

## What it looks like

The dock is a serial port (`/dev/ttyACM1` on Linux, 115200 baud). You type a line, it answers a
line. That's the whole protocol — you can try it with any serial terminal.

```
> IDENTIFY
< DOCKPILOT 1 0.3.7 opendock-dev d22fd45263a89cf4 OD1-A89CF4

> SET TIME 1790616334
< OK

> STATUS
< {"name": "Desk 22", "assettag": "IT-04412", "site": "LON-3F", "fw": "0.3.7",
   "uptime_s": 8123, "temp_c": 41.2, "time": 1790616334, "time_synced": true,
   "docked": true, "docked_s": 3605, "log_entries": 57, "log_last": 312, "log_volatile": false}

> GET LOG 310
< {"i": 310, "t": 1790612700, "synced": true, "up": 4489, "ev": "undock"}
< {"i": 311, "t": 1790616250, "synced": true, "up": 8039, "ev": "dock"}
< {"i": 312, "t": 1790616334, "synced": true, "up": 8123, "ev": "time_sync"}
< END
```

## The commands

| You send | The dock answers | What it means |
|---|---|---|
| `IDENTIFY` | `DOCKPILOT 1 <firmware> <model> <chip id> <serial>` | "I am a dock." The chip id is burned in at the factory. |
| `PING` | `PONG` | Are you there? |
| `CAPS` | one line of JSON | What can you do? (`temp`, `log`, `log_max`) |
| `STATUS` | one line of JSON | How are you right now? |
| `SET TIME <unix seconds>` | `OK` | Here's the time. The dock has no clock of its own — send this every time you connect. |
| `SET NAME "<text>"` | `OK` | A friendly name. Kept in the dock's flash, survives power loss. |
| `SET ASSETTAG "<text>"` | `OK` | Your inventory number. |
| `SET SITE "<text>"` | `OK` | Where it lives. |
| `GET LOG [from] [max]` | one JSON line per entry, then `END` | What happened, from entry number `from` onward. |
| `CLEAR LOG` | `OK` | Hide everything logged so far. |

Anything else gets `ERR badcmd …`. There is no command that changes what the dock's hardware
does — no power switching, nothing — and the protocol will never gain one.

### Since firmware 0.5

Five more, all reads or settings. A dock says which of them it has in `CAPS`
(`"health": true`, `"stats": true`, `"notes": 4`, `"events": [...]`), and a program only asks
for what is advertised.

| You send | The dock answers | What it means |
|---|---|---|
| `HEALTH` | one line of JSON | The dock's own verdict. `"ok"` is `false` if anything needs a look: faults since boot, a log block lost to a power cut, the write budget spent, running hot, or a reset that wasn't a normal power-up. The rest says why. |
| `STATS` | one line of JSON | Lifetime counters that survive the log wrapping: `boots`, `docks`, `docked_s`, `faults`, `temp_max`. |
| `SET NOTE <n> "<text>"` | `OK` | Four spare fields (`n` = 1–4, 64 bytes each) for whatever the three fixed ones don't cover — cost centre, desk phone, service date. Same rules as `SET NAME`. |
| `GET NOTE <n>` | `{"n": 1, "text": "…"}` | One of them. |
| `GET NOTES` | `{"1": "…", "2": "…", "3": "…", "4": "…"}` | All of them. |

### Since firmware 0.6 — the desk's settings are stored in the dock

The dock keeps the desk's configuration and hands it to whatever docks. It validates and stores;
it never acts on any of it — the laptop applies what it understands while it is docked. A dock
that has this says `"desk": {"opts": 9, "keys": 64, "seats": 8, "rules": 8}` in `CAPS`.

**The desk** (one per dock):

| You send | The dock answers | What it means |
|---|---|---|
| `GET DESK` | one line of JSON | `tag`, every option (`""` = not set), how many `keys` and `seats`. Logged as `desk_read`, at most once a minute. |
| `GET SCHEMA` | one JSON line per option, then `END` | The options this dock knows: `id`, `label`, `hint`, `type` (`int` or `str`), `min`, `max`. Show these; don't hard-code them. |
| `SET TAG "<text>"` | `OK` | What people call this desk. (The same field as `SET NAME`.) |
| `SET OPT <id> "<value>"` | `OK` | One desk option. Checked against the schema: `vlan` 1–4094, `mtu` 576–9000, the rest text up to 64 bytes. `""` clears it. |
| `GET OPT <id>` | `{"id": "vlan", "value": "30"}` | One option. |
| `SET KEY <name> "<value>"` | `OK` | Free storage: 64 keys (32 on firmware 0.6), names `a-z 0-9 _` up to 24 chars, values up to 128 bytes. The dock keeps them; it doesn't need to understand them. `""` removes. |
| `DEL KEY <name>` | `OK` | Remove one. |
| `GET KEYS` | `KEY <name> "<value>"` lines, then `END` | All of them. |
| `EXPORT` | `SET TAG …`, `SET OPT …`, `SET KEY …` lines, then `END` | The whole desk as a script you can send back. Save it to a file; that file *is* the desk. |
| `IMPORT` … `IMPORT END` | `OK` | Between the two, `SET TAG/OPT/KEY` and `DEL KEY` are staged in RAM and nothing is written. `IMPORT END` writes once (one unit of the write budget). The import defines the whole desk: options and keys not in it are gone. `IMPORT ABORT` drops the staging; so does disconnecting. |

**The seat** (one per laptop that keeps anything here): a laptop sends `SEAT <id>` once per connection,
where `<id>` is 16 hex characters it derives from its own secret key (DockPilot hashes the key).
The dock files that laptop's things under the id. It never sees the key, so it never learns who you
are, and a second laptop given the same key is the same seat at every dock.

| You send | The dock answers | What it means |
|---|---|---|
| `SEAT <id>` / `SEAT NONE` | `OK` | This connection's seat. Nothing is written until the seat sets something. Cleared when you disconnect. |
| `GET SEAT` | one line of JSON | `rules` (`{"wifi_off": 1, …}`), `cmd` (`{"dock": [lines], "undock": [lines]}`), `snap` (the saved configuration, text the laptop wrote), `snap_t` (when), `autosave`, `new` (no record yet). `{"seat": null}` if none selected. |
| `GET RULES` | one JSON line per rule, then `END` | The rules this dock knows: `id`, `label`, `hint`. Show these; a newer dock may have more. |
| `SET RULE <id> 0\|1` | `OK` | One switch. |
| `SET CMD dock\|undock "<lines>"` | `OK` | Your own commands, `\n` between lines: up to 4 lines of 120 bytes. |
| `SET SNAP "<text>"` | `OK` | The saved configuration, up to 400 bytes, whatever the laptop wants to restore later. Stamped with the dock's clock. |
| `SET AUTOSAVE 0\|1` | `OK` | Save again every time I undock (the laptop does the saving; this is the switch). |
| `FORGET SEAT` | `OK` | Delete this seat from the dock. |

Values are quoted like `SET NAME`: `"…"` with `\"`, `\\` and (new) `\n`. Writes follow the same
flash discipline as everything else — the same field no more than once in 10 s, the shared write
budget — and reads never cost anything. A ninth seat gets `ERR full`; so does a 65th key.

### The web face (firmware 1.0, the C line)

The same dock also answers over the network link it presents on the USB cable. `CAPS` says so:
`"web": {"host": "opendock.local", "ip": "169.254.77.1"}`.

| Request | Answer |
|---|---|
| `GET /` | the OpenDock page |
| `POST /api?seat=<16 hex>` with command lines in the body | the reply lines, exactly as the serial face prints them, `END` included; the seat applies to the whole request |
| `GET /api?c=<command>` | one *read* command, URL-encoded — `curl 'http://opendock.local/api?c=GET%20DESK'`. A command that writes is answered `403 ERR forbidden`: writes go by `POST` with the header, so that no web page can write by merely pointing a browser at a URL |
| `POST /update` with a `.uf2` as the body | one line: `OK update <blocks> blocks, <bytes> bytes; restarting`, then the dock restarts into the new firmware — or `ERR badfile …` / `ERR noslot …` / `ERR readonly …` and nothing changes |

Same commands, same errors, same flash budget. Events are not pushed over HTTP; poll `STATUS`.

Every `POST` must carry the header `X-OpenDock: 1` (any value), or the answer is `403` with
`ERR forbidden missing X-OpenDock header`. From the command line that is
`curl -H 'X-OpenDock: 1' --data-binary 'GET DESK' http://opendock.local/api`. The header is what
keeps other web pages out: a browser only sends a custom header to another site after asking it
(a CORS preflight), and the dock never answers that question. There are no CORS headers at all.

**Keys as URLs.** Every key is also a web address, so a script on any device that can reach the
cable reads or writes it with nothing but HTTP — no protocol, no quoting, no app:

| Request | Answer |
|---|---|
| `GET /key/<name>` | the bare value and a newline, `text/plain`; `404` + `ERR nokey` if there is no such key |
| `POST /key/<name>` with the value as the body (header required) | `OK`; an empty body removes the key |
| `DELETE /key/<name>` (header required) | `OK` |
| `GET /keys` | the `GET KEYS` reply: `KEY <name> "<value>"` lines, then `END` |

The same rules as `SET KEY`: names `a-z 0-9 _` up to 24, values up to 128 bytes of UTF-8 on one
line (a newline in the body ends the value), the 10 s gap per key, the write budget, read-only
docks refuse, and every write is a `desk_change` in the log.
```
curl http://opendock.local/key/badge_reader
curl -H 'X-OpenDock: 1' --data 'room 4.12' http://opendock.local/key/room
curl -H 'X-OpenDock: 1' -X DELETE http://opendock.local/key/room
```

**The app, carried by the dock.** A dock that has DockPilot baked in says so in `CAPS`
(`"web": {…, "app": "1.2"}`) and serves it: `GET /dockpilot.deb` (Ubuntu package) and
`GET /dockpilot.whl` (Python wheel), each with its real file name in `Content-Disposition`. The
page offers the .deb wherever DockPilot is needed. Linux only for now.

**Firmware updates.** `POST /update` is the only way a running dock takes new firmware, and the
dock never overwrites itself: the file goes into the other firmware slot, the dock restarts into
it, and the new firmware must confirm itself (log `fw_update` `v` 2) or the boot ROM starts the
previous one again. The firmware installed last is the one that runs, whatever its version says.
Accepted: a plain `.uf2` for the RP2350 (`opendock-dev.uf2` / `opendock-carrier.uf2`). The
installer file is refused; it is for a dock with no firmware (`ERR noslot`). Files are on
[opendocktech.com](https://opendocktech.com).
`curl -H 'X-OpenDock: 1' --data-binary @opendock-dev.uf2 http://opendock.local/update`

## Things the dock says on its own

A few lines can arrive without being asked for. They always start with `EVENT `, so a program can
tell them apart from answers:

```
EVENT hello        the dock noticed you connected
EVENT temp 61      it just crossed a temperature threshold (°C)
EVENT dock         a laptop was just attached      (firmware 0.5 and later)
EVENT undock       it was just removed
```

## What's in the log

Each entry is one JSON line: `i` (its number, always counting up), `t` (time, or `null` if the
dock didn't know the time yet), `up` (seconds since the dock powered on), `ev` (what happened),
and sometimes `v` (a detail).

| `ev` | Meaning | `v` |
|---|---|---|
| `boot` | the dock started | why: 1 power on · 2 brownout · 6 watchdog |
| `dock` / `undock` | a laptop was attached / removed | |
| `host_connect` / `host_disconnect` | a program opened / closed the port | |
| `temp_high` / `temp_ok` | temperature crossed 60 °C going up / 55 °C coming down | °C |
| `config_change` | a setting was written | 1 name (= the desk tag) · 2 asset tag · 3 site · 4–7 note 1–4 |
| `time_sync` | a program set the clock | |
| `log_cleared` | `CLEAR LOG` was used | |
| `fault` | the firmware hit a problem and carried on | |
| `desk_change` | the desk was written (firmware 0.6) | 2 option · 3 key · 4 import · 5 seat · 6 seat forgotten (1 is unused: the tag logs as `config_change` 1) |
| `desk_read` | the desk was handed to a laptop (`GET DESK`), at most once a minute | |
| `fw_update` | a firmware update (firmware 1.0, C line) | 1 new firmware written, restarting into it · 2 new firmware running (it confirmed itself) |

The dock keeps the newest 2048 entries. To collect the log without fetching anything twice,
remember the last `i` you saw and ask `GET LOG <that+1>`. `STATUS` tells you the newest number
(`log_last`), so you can see whether there is anything new before asking.

## Errors

`ERR <code> <what went wrong>` — five codes:

| Code | Meaning | What to do |
|---|---|---|
| `badcmd` | the dock doesn't know that command | don't send it again |
| `badarg` | a missing or malformed argument | fix the command |
| `range` | a value is out of range (e.g. a name over 64 bytes) | shorten it |
| `toolong` | the line was over 512 bytes | send shorter lines |
| `busy` | not right now — the message says why, and how long to wait if waiting helps | wait that long, try once more |

## If you are building a dock

Any chip with a USB serial port will do. Your firmware needs to:

1. Answer `IDENTIFY` with the signature line, and answer the other nine commands as above.
2. Accept lines ending in LF, CR or CRLF, up to 512 bytes, in UTF-8. Reply exactly one line per
   command (`GET LOG` is lines-then-`END`). Never put a reply in the middle of another.
3. Keep `name`, `assettag` and `site` (64 bytes each) and the log where they survive power loss.
4. Number log entries upward, forever. Never reuse a number.
5. Write to flash only when something changes, never on a timer, and refuse a program that
   writes too often with `ERR busy` rather than wearing the flash out.
6. Send `EVENT hello` when a program connects, and never send events to a program that hasn't
   asserted DTR.
7. Control nothing. If it changes hardware state, it is not this protocol.

The reference firmware (RP2350 / Raspberry Pi Pico 2, open source, from
[opendocktech.com](https://opendocktech.com)) does all of this and is what DockPilot is tested against.

## If you are writing a program

1. Send only `IDENTIFY` to a port you don't recognise; leave it alone if the signature doesn't
   come back. Never open a port at 1200 baud (on many boards that means "reboot into the
   bootloader").
2. Send `SET TIME` right after `IDENTIFY`, every time.
3. Parse replies as JSON and ignore fields you don't know — new fields will appear.
4. Treat any line starting `EVENT ` as an event, wherever it arrives.
5. When `ERR busy` says how long to wait, wait that long and try once more.

## Twenty lines of Python

```python
import json, serial, time

port = serial.Serial("/dev/ttyACM1", 115200, timeout=2)
time.sleep(0.5)                                  # let the dock say hello
port.reset_input_buffer()

def ask(cmd):
    port.write((cmd + "\n").encode())
    while True:
        line = port.readline().decode().strip()
        if not line.startswith("EVENT "):        # events can arrive at any time; skip them
            return line                          # ("" means the dock didn't answer in time)

print(ask("IDENTIFY"))
print(ask("SET TIME %d" % time.time()))
status = json.loads(ask("STATUS"))
print(status["name"], status["docked"], status["temp_c"])

port.write(b"GET LOG 1\n")
while True:
    line = port.readline().decode().strip()
    if line in ("END", ""):
        break
    if line.startswith("{"):
        print(json.loads(line))
```

`opendock.py` in this repo is the full-grown version of that: discovery, reconnection, the log
collected incrementally, and a command line (`python3 opendock.py list | status | log | set | watch`).

