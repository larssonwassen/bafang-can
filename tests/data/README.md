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
