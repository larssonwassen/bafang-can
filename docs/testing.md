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

## Where the simulator's data comes from

Its **framing** is derived from the vendored implementation and independently
verified against it. Its **values are invented** -- plausible numbers for a
250 W / 36 V mid-drive, not a capture from a real M200; the identity strings
are made up too.

There is a circularity worth naming: the simulator builds its payloads with
this package's own `int_to_bytes_le` and `checksum`, so a decoding bug shared
between the simulator and the codecs would be invisible to any test that only
uses the simulator. That is the whole reason layer 3 exists -- the vendored
JavaScript is an oracle written by someone else, from someone else's reading
of the protocol. Layer 4 covers wiring and CLI behaviour; it does not
establish that the byte layouts are right.

Replacing the invented values with a recorded capture is a strict improvement,
and the machinery for it is in place: `bafang-can capture` records every
answer a real bike gives, `import-capture` rebuilds the same thing from a
sniff log, and `--sim-profile` makes the simulator answer from it. See
[profiles.md](profiles.md). Until such a capture exists the defaults stay
labelled as invented, because a plausible guess presented as a measurement is
worse than an obvious guess.

## What this does *not* prove

* That the CANable Pro 2.0 enumerates and passes traffic — that needs the
  adapter. `bafang-can adapters` is the first check when it arrives.
* That an M200 (G210) lays its parameter blocks out the way the M500/M600
  generation does. `bafang-can probe` and the workflow in
  [m200.md](m200.md) exist for exactly that reason.
* Timing behaviour on a loaded bus: the 20 ms inter-frame delay and the 2 s
  request timeout are taken from the upstream projects, not measured here.
