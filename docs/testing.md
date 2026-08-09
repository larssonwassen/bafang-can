# How this is validated without a motor

Four layers, in increasing distance from the code:

## 1. Unit tests

`tests/test_frame.py`, `tests/test_codecs.py` — identifier packing, checksums,
field offsets, scaling.

## 2. Round-trip invariants

`test_decode_encode_is_the_identity_on_random_blocks` decodes 200 random
64-byte blocks per parameter block and asserts that encoding them again
produces the *same bytes*. This is the invariant that protects a real motor:
if any field scales badly on the way out, or an offset is right in one
direction and wrong in the other, a random block does not survive.

It found one real defect: the boolean fields (`coaster_brake`,
`displayless_mode`, `lamps_always_on`) used to rewrite their byte as 0 or 1,
so a byte holding, say, 0x3D would come back as 0x01 even though the user
changed nothing. Flags now only write when the value actually changed — see
`Block._put_flag`.

## 3. Differential tests against the vendored JavaScript

The Python codecs were written by reading the upstream projects, not by
importing them, so both directions are checked against the real thing under
node:

* `tests/test_differential.py` — feeds 50 random payloads per message type
  through `vendor/bafang_canable_pro/bafang-parser.js` and compares every
  field. Disagreements that are deliberate are listed in `KNOWN_DIFFERENCES`
  with the reason.
* `tests/test_differential_write.py` — drives the vendored
  `bafang-serializer.js` with a faked CAN bus that records frames, and asserts
  that the frames this tool emits for a multi-frame write are **identical**
  (identifiers, operation codes, sequence numbers, payload split). It also
  feeds the vendor serializer known values and checks that our decoder reads
  them back correctly, which pins the byte offsets from both sides.

These tests skip automatically when node or the submodules are missing.

Finding from this exercise: the vendor parser exposes `speed_limit_enabled`
at Parameter1 byte 36, which this tool was missing. It is now decoded and
encoded.

## 4. End-to-end against the simulator

`tests/test_cli.py` runs the real argument parser, protocol stack and codecs
against `bafang_can/simulator.py`, which implements the same framing rules as
the bus: single-frame reads, multi-frame reads that must be acknowledged frame
by frame, multi-frame writes, error acks for unsupported commands, and
background traffic between the bike's own nodes that we must observe without
acknowledging.

You can drive it by hand exactly like the real thing:

```bash
bafang-can --interface sim diagnose
bafang-can --interface sim monitor
bafang-can --interface sim --sim-errors 8,21 errors
bafang-can --interface sim --sim-idle monitor      # bike standing still
```

With `--sim-state bike.json` the simulated motor keeps its configuration
between invocations, so the whole hardware workflow can be rehearsed:

```bash
bafang-can --interface sim --sim-state bike.json dump -o baseline.json
bafang-can --interface sim --sim-state bike.json set Parameter1.current_limit=12 --apply
bafang-can --interface sim --sim-state bike.json get Parameter1.current_limit
bafang-can --interface sim --sim-state bike.json restore baseline.json --apply
```

## What this does *not* prove

* That the CANable Pro 2.0 enumerates and passes traffic — that needs the
  adapter. `bafang-can adapters` is the first check when it arrives.
* That an M200 (G210) lays its parameter blocks out the way the M500/M600
  generation does. `bafang-can probe` and the workflow in
  [m200.md](m200.md) exist for exactly that reason.
* Timing behaviour on a loaded bus: the 20 ms inter-frame delay and the 2 s
  request timeout are taken from the upstream projects, not measured here.
