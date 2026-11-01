# The DockPilot Dock Protocol (v1)

A dock that can be asked questions. Put a small chip in a dock, give it a USB serial port, and
have it answer ten plain-text commands. DockPilot — and anyone's script — can then ask the dock
who it is, keep a name and an asset tag on it, and read back what happened to it while nobody
was looking.

It is open. Anyone may build a dock that speaks it, or a program that talks to one.

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

## The ten commands

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

## Things the dock says on its own

Two lines can arrive without being asked for. They always start with `EVENT `, so a program can
tell them apart from answers:

```
EVENT hello        the dock noticed you connected
EVENT temp 61      it just crossed a temperature threshold (°C)
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
| `config_change` | a setting was written | 1 name · 2 asset tag · 3 site |
| `time_sync` | a program set the clock | |
| `log_cleared` | `CLEAR LOG` was used | |
| `fault` | the firmware hit a problem and carried on | |

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

The reference firmware for the Raspberry Pi Pico 2 does all of this and is what DockPilot is
tested against.

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

