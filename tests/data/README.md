# Test captures

Recorded from a real bike, kept here because tests assert against them.
`captures/` is deliberately git-ignored, so anything worth keeping lives here
instead.

## `display-interaction-excerpt.log`

99 frames of a `DP C340.CAN` display talking to a G210 drive unit
(`CR X210.350.FC`) on a 36 V `BT360` pack, recorded 2026-08-17 through an
Openlight Labs CANable2 running the `normaldotcom/canable2` slcan firmware.
An excerpt of `captures/display-interaction-2026-08-17.log`, kept contiguous
so the sequence counters stay meaningful.

It is here for two reasons.

**It is damaged in every way `bafang_can.quality` knows how to detect**, which
makes it the regression corpus for that module:

| defect | how many | what it is |
| --- | --- | --- |
| identifier wider than 29 bits | 2 | `2F830100`, `28308000` -- the slcan reader resynchronising after a dropped serial byte. python-can parses the hex with no range check, and `BafangId.decode` would mask them into plausible messages from devices that are not on the bus. |
| gaps in the `31/00` rolling counter | 12 frames | Real frames the corruption destroyed. The torque sensor numbers its broadcasts, so the missing numbers are proof of loss rather than an inference. |
| frames closer together than 268 us | 24 | Shorter than the shortest frame at 250 kbit/s, so these are host read times from a drained buffer, not arrival times. |

**It carries one of every message the bus produces** -- 22 distinct
identifiers -- so the codecs are checked against bytes a real bike sent rather
than bytes we invented. The internal cross-checks that come with it are worth
more than any single field:

* the battery reports 6111 of 11112 mAh remaining in `34/00`, and independently
  reports 55% in the same frame; the drive unit independently reports 55% in
  `32/00`. 6111/11112 is 55.0%.
* the torque sensor reports 1265 ADC counts in `31/00`; the drive unit reports
  the same 1265 in `32/00`.
* the drive unit reports 51.50 V in `32/01` while the battery reports 37.47 V
  in `34/01` -- a factor of 1.374. That disagreement is real and is *not* a
  decoding bug: it is the voltage-sense gain error worked through in
  `docs/m200.md`, preserved here so the warning that reports it stays tested.

Nothing here identifies a bike or a rider. The display never reads `0x60/0x03`
or `0x60/0x04` during normal running, so no serial or customer number is on the
wire in this window.

## `bench-repaired-excerpt.log`

120 frames of the same G210 the day after, recorded 2026-08-18 through the same
adapter reflashed to candleLight 2.5 (gs_usb), in listen-only mode. The battery
is disconnected and the harness runs from a bench supply, so only the torque
sensor and display are on the bus with the drive unit.

It is the healthy counterpart to the capture above, and the pair is what
establishes that `12/00` carries the live fault code:

| | faulty (`display-interaction-excerpt.log`) | repaired (this file) |
| --- | --- | --- |
| `02FF1200` drive unit `12/00` | `07` | `00` |
| `03FF1300` display `13/00` | `07` | `00` |
| `02F83201` voltage field | 51.50 V against a 37.47 V pack | 40.10--40.20 V on a bench supply |

Nothing in either upstream project documents `12/00` as the fault code. Two
captures of one bike in two states do.

This file is also clean where the other is damaged -- no lost frames, no corrupt
identifiers, no impossible timing -- so `LinkQuality` is tested against a
capture it should pass as well as one it should fail. That matters: a detector
only ever tested on broken input is not known to accept good input.

It contains no serial or customer numbers, for the same reason as the file
above.

## `ride-*.log`

Three excerpts of one 102 s road ride, recorded 2026-08-20 through the same
candleLight 2.5 adapter in passive mode, on the same G210 -- but with the
36 V pack fitted and the bike actually moving. Every other capture in this
directory was taken on a stand or off a bench supply.

The full log is clean: 16807 frames, no lost broadcasts, no duplicates, no
impossible timing. That is worth stating on its own, because it is the gs_usb
receive path validated at road speed with a motor pulling 15 A a metre away
from the wiring.

| file | window | what it is for |
| --- | --- | --- |
| `ride-bus-errors-excerpt.log` | 2.7 s, 450 frames | All eight error frames of the ride, and the peak current that comes with them. |
| `ride-above-cutoff-excerpt.log` | 3.0 s, 493 frames | 24.1 to 26.6 km/h with the motor still pulling. |
| `ride-coasting-excerpt.log` | 3.0 s, 493 frames | The rider stops pedalling while the bike rolls on at 25 km/h. |

**The error frames are the point of the first file.** They are not corruption:
the adapter is reporting that a frame on the wire was malformed, and it names
the fault -- frame format errors and bit stuffing errors. All eight arrive
inside 2.1 s, and that window is the highest motor current of the whole ride:
15.1 A, with the pack sagging from 37.2 V to 34.2 V. Nothing else in the
capture points at the motor. The drive unit reports no fault, every node keeps
broadcasting, and no sequence counter skips.

python-can writes one identifier, `20000080`, for every error frame regardless
of what the controller said, so the *class* does not survive a candump log even
though the payload does. The tests assert that the class reads as unknown
offline rather than as the bus error that stamp would imply.

**The second file is what a written speed limit does to a motor.** 45 km/h was
written to `32/03` earlier that day; stock firmware cuts assist at 25.0. Here
the drive unit holds 11--13 A through 25.5, 25.9, 26.2 and 26.6 km/h. The
`32/03` broadcast is in the excerpt too, so the capture carries its own proof
of what the bike was configured to do.

**The third file identifies `0x32/0x08`,** which appears in no published table.
It reads like watts and peaks at 581 across the ride. Through this window the
drive unit reports 0.0 A for every single sample -- the motor is doing nothing
-- and the field still reads 42, then 34, then falls to 0 as the rider stops
pedalling, while the bike is still rolling at 25 km/h. So it is not motor
power and it is not speed. It is rider-side. The scale is *not* claimed: fitting
it against `(torque - rest) * cadence` over the whole ride gives a coefficient
spanning an order of magnitude, because the torque broadcast is instantaneous
within a pedal stroke and this field is not.

These carry no serial or customer numbers: they are broadcast traffic, and
nothing on this bus broadcasts an identity. The battery's own frames
(`34/00`, `34/01`, `34/02`) are here, which no other fixture has, and they
cross-check against the drive unit: both report 52% state of charge, and the
pack's terminal voltage agrees with the drive unit's sense reading to within
2%. That last one is the regression test for the 1.374x gain fault in
`docs/m200.md` -- the same comparison that read 51.50 V against a 37.47 V pack
before the repair.
