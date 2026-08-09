# bafang-can

Diagnose, calibrate and configure a **Bafang M200 (G210)** over CAN with a
**CANable Pro 2.0**.

The two existing open-source Bafang tools each solve half of this problem:

* [`andrey-pr/OpenBafangTool`](https://github.com/andrey-pr/OpenBafangTool)
  (MIT, TypeScript/Electron) has the most carefully worked-out parameter block
  layouts, the wheel diameter table and sane parameter limits — but talks to
  the bike through the ~$100 BESST box.
* [`mdi-9/bafang_canable_pro`](https://github.com/mdi-9/bafang_canable_pro)
  (GPLv3, Node.js) talks to a cheap CANable over gs_usb and knows commands
  OpenBafangTool does not (Parameter0, controller state, torque-sensor
  calibration, firmware update) — but is a web UI wrapped around a server.

Both are vendored here as submodules under `vendor/` and are the reference for
everything in `src/`. This repository is a single scriptable Python CLI that
takes the protocol knowledge from both, drops the BESST hardware requirement,
and adds the parts a workshop session actually needs: capability probing,
JSON backup/restore, verified read-modify-write, and per-motor safety limits.

## Install

```bash
git clone --recurse-submodules <this repo>
cd bafang-can
python3 -m venv .venv && .venv/bin/pip install -e .
```

macOS also needs libusb for the gs_usb transport: `brew install libusb`.
See [docs/hardware.md](docs/hardware.md) for wiring and firmware notes.

## Use

```bash
bafang-can adapters                 # is the CANable visible
bafang-can scan                     # which nodes answer on the bus
bafang-can info                     # hardware/software/serial of every node
bafang-can probe                    # which commands this firmware implements
bafang-can diagnose                 # read-only health report
bafang-can monitor                  # live speed/voltage/current/torque/cadence
bafang-can sniff --passive          # decode traffic without transmitting
bafang-can errors                   # stored fault codes with descriptions
bafang-can dump -o baseline.json    # full configuration backup
```

Changing things — every write is a dry run until `--apply`:

```bash
bafang-can get Parameter1
bafang-can set Parameter1.current_limit=12 --apply
bafang-can set Parameter1.assist_levels.0.current_limit=25 --apply
bafang-can wheel --diameter 27.5 --circumference 2215 --speed-limit 25 --apply
bafang-can calibrate torque --apply
bafang-can restore baseline.json --apply
```

## How writes work

Every configuration write is read-modify-write. The block is read from the
drive unit, only the fields you named are patched, the checksum is recomputed
and the block is sent back — the bytes whose meaning nobody has decoded yet
are never touched. After writing, the block is read back and compared, and
`set` exits non-zero if the device does not report what you asked for.

Guard rails, in order:

1. A block that reads back with a bad checksum is not written on top of
   (`--force` overrides).
2. Values outside the M200 profile limits in `src/bafang_can/profiles/m200.py`
   stop the write and print why (`--force` overrides).
3. Writes need `--apply`; calibration and error clearing also need a typed
   confirmation unless `-y`.

## Layout

```
src/bafang_can/
  constants.py    device ids, operations, wheel table, error codes
  frame.py        29-bit identifier encode/decode, checksum, string helpers
  commands.py     merged read/write command tables from both projects
  codecs.py       parameter block parsing and read-modify-write encoding
  transport.py    CANable Pro 2.0 / gs_usb / slcan / socketcan via python-can
  protocol.py     requests, acks, multi-frame transfers, timeouts
  system.py       high-level device access, dump and restore
  profiles/m200.py  M200 (G210) limits and diagnostic checklist
  cli.py          the command line interface
docs/             protocol notes, hardware setup, M200 workflow
vendor/           the two upstream projects, as submodules
tests/            protocol and codec tests against a simulated drive unit
```

## Status and honesty about the M200

The protocol layer is exercised by tests against a simulated drive unit
(`.venv/bin/python -m pytest`). The parameter *semantics* come from motors of
the M500/M600 generation: neither upstream project lists the M200 as tested
hardware, and Bafang has moved fields between firmware generations. Read
[docs/m200.md](docs/m200.md) before writing anything, back up first, and change
one field at a time.

## Licence

GPL-3.0-or-later, because `bafang_canable_pro` is GPLv3 and this work is
derived from it. `OpenBafangTool` is MIT, which is compatible with that.
