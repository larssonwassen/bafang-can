# Device profiles: replacing invented data with your bike

The simulator's default values are invented (see
[testing.md](testing.md#where-the-simulators-data-comes-from)). A **device
profile** replaces them with bytes a real motor actually sent, so
`--interface sim` stops being a guess about how an M200 behaves and becomes a
replay of how *yours* does.

That matters for more than demos: once the profile exists, every code change
can be checked against real device data without the bike on the bench.

## Making one

### From the bike (better)

```bash
bafang-can capture -o m200-profile.json --note "Bafang M200 G210, stock firmware"
```

This walks every read command each device answers and stores the raw payload —
nothing is interpreted, so a field this tool decodes wrongly is still recorded
correctly. Commands the motor refuses are simply absent, which is itself
information: it is `probe` output with the bytes attached.

### From a sniff log

```bash
bafang-can sniff -o ride.log --quiet --seconds 120     # record
bafang-can import-capture ride.log -o m200-profile.json
```

Multi-frame answers are reassembled the same way the live client does it, and
traffic between the bike's own nodes (display polling the drive unit and the
battery) is captured too. A passive capture only contains what happened to be
transmitted while recording, so expect it to be patchier than `capture`.

Any candump/ASC/BLF/CSV log works, including ones recorded by `candump` or
another tool.

## Using one

```bash
bafang-can --interface sim --sim-profile m200-profile.json diagnose
bafang-can --interface sim --sim-profile m200-profile.json get Parameter1
```

Anything the profile does not cover falls back to the invented default, so a
partial capture is still useful. Recorded answers win over synthesized ones
even for live data — which means telemetry replayed from a profile is frozen
at its captured value. Drop `--sim-profile` when you want the moving numbers
back.

Inspect one without loading it:

```bash
bafang-can profile m200-profile.json
```

## Sharing

Profiles are plain JSON and are worth sharing — an M200 profile is exactly
what this project is missing, and what would let someone else's code be tested
against a real M200 too.

They contain your bike's serial and customer numbers. Blank those first:

```bash
bafang-can capture -o m200-profile.json --anonymize
```

`--anonymize` replaces the serial and customer number payloads with
`REDACTED`. Nothing else in a profile identifies a bike or a rider — the
parameter blocks are configuration, not identity. `bafang-can profile` shows
whether a file was anonymized.

## Once you have one

The invented defaults in `src/bafang_can/simulator.py` can then be replaced
with the captured values, so even a plain `--interface sim` behaves like the
real motor. Until then the defaults stay marked as invented, because a
plausible guess presented as a measurement is worse than an obvious guess.
