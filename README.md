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
everything in `src/`. A third source is used but not vendored:
[`OpenSourceEBike/Bafang_M500_M600`](https://github.com/OpenSourceEBike/Bafang_M500_M600),
which extracted the command table out of the **BESST desktop application's own
JavaScript** and published PCAN logs of an M500 powering up and down. That is
the closest thing to a vendor list anyone has released; it contributes sixteen
command codes neither vendored project implements, corroborates three things
this project had decoded on its own, and disagrees with it about one — see
"Cross-referencing the M500/M600 work" in [docs/m200.md](docs/m200.md). This repository is a single scriptable Python CLI that
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

## Try it without hardware

A simulated bike is built in, implementing the same framing rules as the real
bus, so every command below works before the adapter arrives:

```bash
bafang-can --interface sim diagnose
bafang-can --interface sim monitor
bafang-can --interface sim --sim-state bike.json set Parameter1.current_limit=12 --apply
```

## Use

```bash
bafang-can adapters                 # is the CANable visible
bafang-can scan                     # which nodes answer on the bus
bafang-can info                     # hardware/software/serial of every node
bafang-can probe                    # which commands this firmware implements
bafang-can diagnose                 # read-only health report
bafang-can monitor                  # live speed/voltage/current/torque/cadence
bafang-can monitor --passive        # same, from broadcasts, transmitting nothing
bafang-can sniff --passive          # decode traffic without transmitting
bafang-can errors                   # stored fault codes with descriptions
bafang-can dump -o baseline.json    # full configuration backup
bafang-can sniff -o ride.log        # record the bus to a candump log
bafang-can decode-log ride.log      # decode a recorded capture offline
```

Recording your bike so the simulator replays it instead of invented values
(see [docs/profiles.md](docs/profiles.md)):

```bash
bafang-can capture -o m200.json --anonymize    # ask the bike everything
bafang-can import-capture ride.log -o m200.json  # or rebuild it from a sniff
bafang-can --interface sim --sim-profile m200.json diagnose
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
  simulator.py    a simulated bike, for working without hardware
  capture.py      recording a real bike into a replayable device profile
  cli.py          the command line interface
docs/             protocol, hardware, M200 workflow, testing, device profiles
vendor/           the two upstream projects, as submodules
tests/            unit, round-trip, differential (vs vendored JS) and CLI tests
```

## Validation

Run `pytest` (238 tests, ~77 s). Eight layers, described in
[docs/testing.md](docs/testing.md):

1. Unit tests for identifiers, checksums, offsets and scaling.
2. Round-trip fuzz: 200 random blocks per parameter block must decode and
   re-encode to the same bytes. This caught a real defect where boolean fields
   rewrote their byte as 0/1 and discarded whatever else it held.
3. Differential tests that run the **vendored JavaScript** under node and
   compare field by field in both directions. The multi-frame write frames
   this tool emits are byte-identical to the vendor serializer's. This also
   found a missing field (`speed_limit_enabled`, Parameter1 byte 36).
4. End-to-end CLI tests against the built-in simulator.
5. Assertions against **two real captures of one G210**, faulty and repaired,
   kept in `tests/data`. Two nodes independently reporting the same charge and
   the same torque check the codecs in a way no shared bug can fake; the pair
   is what establishes the live fault code; and one is damaged and one is clean,
   so the capture-integrity checks are tested in both directions.
6. Bench tests on a real adapter with no bike attached, which settled how the
   firmware is identified and made `sniff --passive` genuinely passive.
7. A session on a real bike, which proved the transport and addressing and
   found that one realtime block does *not* match this generation.
8. Hardware loopback on a gs_usb adapter, where frames this tool emits are
   transmitted and read back byte for byte — the transmit path, finally.

## What one real bike showed

Run against a **G210** drive unit with a `DP C340.CAN` display,
`SR PA450.32.ST.C` sensor and `BT360` battery — that is, exactly the hardware
this project targets. Note that `info` reports its model as `CR X210.350.FC`,
which is the controller's model string; the label on the board underside reads
`G210` above it. The two are the same unit, not different generations.

The transport, addressing and multi-frame reassembly all work; `scan` and
`info` return real data from every node. Two findings matter for anyone
pointing this at similar hardware:

* That firmware **answers identity reads only** — 5 of 16 known commands. No
  parameter block, no realtime read, no stored error code, from any source id.
  It does however **broadcast** the realtime data it will not answer, which is
  what `monitor --passive` reads — including the **live fault code**, settled by
  capturing the same bike faulty and repaired (`07` then `00` on `12/00`).
* Its `ControllerRealtime1` voltage field read **51.0 V while the battery
  independently reported 37.4 V** on a 36 V pack. That disagreement, surfaced
  by `monitor --passive`, turned out to be a real hardware fault rather than a
  decoding bug: the drive unit's voltage sense read **1.363× high**, so it
  tripped its (perfectly sensible) 47 V overvoltage threshold at 34.5 V of
  actual pack and disabled assist permanently. The cause was a contamination
  bridge across one divider resistor — no component had failed. Repaired, the
  same reading came back to +0.70%. [docs/m200.md](docs/m200.md) has the full
  method, including the two measurement techniques that gave misleading
  answers along the way. `ControllerRealtime0` passed the equivalent check,
  its torque field agreeing with the torque sensor's own broadcast to within
  four counts.
* Both captures from that session were **damaged, and nothing said so**. One
  lost 53% of its frames; the other recorded 2.2 s of bus traffic as if it had
  taken 40 ms, having drained a stale serial buffer rather than read the wire.
  Neither is visible by looking at the file. The torque sensor numbers its
  broadcasts, so loss is provable rather than inferred, and `sniff`,
  `decode-log` and `import-capture` now report it —
  see [docs/hardware.md](docs/hardware.md#frame-loss-on-slcan).
* The tool described **24 error codes as undocumented that were vendored here
  all along**, code 7 among them. That is the code this bike displayed for the
  entire overvoltage investigation, and it means "over voltage protection".
  `bafang_canable_pro` carries 33 descriptions with recommendations;
  `OpenBafangTool` carries 3. Both are now merged — and on code 14 they
  genuinely disagreed. The vehicle manual — a third source, and the only one
  authoritative for this bike — settles it as controller overtemperature, and
  supplies more actionable text for eight other codes.

* **The bike is deliberately restricted — in the display, not the protocol.**
  The vehicle manual states that wheel size, speed and assist levels are
  *locked*, so the brand really did restrict it. But that lock is a menu that
  declines to change them: the drive unit broadcasts those same values unasked
  every two seconds and accepts writes to them. The manual's figures, 28″ and
  25 km/h, are exactly what the `32/03` decode produces — two documents sharing
  no code path agreeing on both fields.
* **Nothing on the bus itself looks locked.** Across 42000
  captured frames there is no challenge, no nonce, no high-entropy payload and
  no authentication of any kind; the assist-level encoding, the fault-code
  table and the model string are all stock Bafang. The one identity exchange is
  a single boot-time read of the drive unit's serial number by the display,
  which stops once answered — pairing check and part-number lookup are
  indistinguishable from one bike. The real obstacle to swapping a part is that
  this firmware answers **no parameter block**, so a replacement has to arrive
  already configured.
* **That silence is not a refusal.** `probe` now separates ERROR_ACK from
  no-answer instead of collapsing both to "unsupported", and probes an
  unassigned code as a control. Measured across all three nodes — drive unit,
  torque sensor and display — there is **not one ERROR_ACK on the bus**: every
  unanswered command behaves exactly like a command that does not exist. The
  torque sensor, a different product with no tuning surface worth locking,
  shows the identical pattern, which is hard to reconcile with a
  derestriction lock and easy to reconcile with a protocol generation that
  pushes rather than answers.
* **The bike is configurable after all, for the two settings that matter.**
  The drive unit will not answer a `32/03` read — and it *broadcasts* `32/03`
  every two seconds, and the write command for wheel size and speed limit is
  that same `32/03`. `wheel` was unusable here only because it started with a
  read that timed out; it now falls back to the broadcast. This is also what
  owners fitting a generic Bafang display are seeing: such a display writes
  those values, it never needed to read them.
* **The stored fault log was on the display, and the tool never asked.** The
  drive unit does not answer `60/07`, so this was recorded as "no readable
  fault log". The `DP C340.C` answers it with the full history — on this bike a
  stored **code 7, over voltage protection**, the exact fault of the
  investigation above, plus a keypad-circuit code. `errors` and `diagnose` now
  ask both and report which answered. The reasoning and the
  experiment that would settle it are in
  [docs/m200.md](docs/m200.md#what-the-bus-does-at-power-on-and-the-question-of-a-locked-firmware).

The bus turned out to carry far more unasked than the tool was reading. Eleven
message types decode — including pack capacity and state of health, charge-cycle
count, the wheel and speed-limit configuration, and a drive-unit uptime counter
that dates a capture's power-on instant and so catches a damaged clock without
trusting the host — and twelve more are recorded undecoded so the next person
starts from evidence.
[docs/m200.md](docs/m200.md#what-the-bus-carries-unasked) lists all twenty-three
with periods, payloads and the cross-checks that confirm them.

## Status and honesty about the M200

What is verified is the *protocol*: framing, checksums, offsets, and agreement
with both upstream implementations. What is not verified is that an M200
(G210) lays its parameter blocks out like the M500/M600 generation that both
upstream projects target — neither lists the M200 as tested hardware, and
Bafang has moved fields between firmware generations. Read
[docs/m200.md](docs/m200.md) before writing anything, back up first, and change
one field at a time.

## Licence

GPL-3.0-or-later, because `bafang_canable_pro` is GPLv3 and this work is
derived from it. `OpenBafangTool` is MIT, which is compatible with that.
