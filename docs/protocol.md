# Bafang CAN service protocol

Reconstructed from the two vendored projects and expressed in
`src/bafang_can/frame.py`, `commands.py` and `protocol.py`. Nothing here comes
from official Bafang documentation.

The closest approach to it is second-hand:
[`OpenSourceEBike/Bafang_M500_M600`](https://github.com/OpenSourceEBike/Bafang_M500_M600)
pulled the command table out of the BESST desktop application's own JavaScript,
so those *names, codes and subcodes* originate with Bafang even though no
layouts do. It independently agrees with this document on the identifier
layout, the node addresses and all eight operation codes. Sixteen of its codes
are in `commands.py` marked `BESST only`, with no payload decoding and with
device applicability inferred rather than observed.

## Physical layer

| Property | Value |
| --- | --- |
| Bit rate | 250 kbit/s |
| Frame format | classic CAN 2.0B, 29-bit identifiers only |
| CAN-FD | never used |
| Termination | 120 Ω at both ends of the bike harness already |

## Identifier

29 bits, five packed fields:

```
 28    24 23    19 18  16 15      8 7       0
+--------+--------+------+---------+---------+
| source | target |  op  |  code   | subcode |
+--------+--------+------+---------+---------+
```

`bafang_canable_pro` writes this as a four-byte array `[0x80|source,
(target<<3)|op, code, subcode]` — the `0x80` is the SocketCAN EFF flag in bit
31, not part of the address. `OpenBafangTool` builds the same word through the
BESST box's USB protocol.

### Node addresses

| Id | Device |
| --- | --- |
| 0x01 | torque sensor |
| 0x02 | drive unit (controller) |
| 0x03 | display |
| 0x04 | battery |
| 0x05 | service tool (BESST box; this tool impersonates it) |
| 0x1f | broadcast |

### Operations

| Code | Meaning |
| --- | --- |
| 0 | write command |
| 1 | read command |
| 2 | normal ack (also carries short read answers) |
| 3 | error ack |
| 4 | multi-frame start |
| 5 | multi-frame continuation (sequence number in *subcode*) |
| 6 | multi-frame end |
| 7 | multi-frame warning (used by the firmware update broadcast) |

## Transfers

### Short read

```
tool -> device   READ_CMD  code/subcode, empty payload
device -> tool   NORMAL_ACK code/subcode, payload (≤ 8 bytes)
```

### Long read

```
device -> tool   MULTIFRAME_START code/subcode, payload[0] = total length
device -> tool   MULTIFRAME       code/<seq>,   8 bytes
...
device -> tool   MULTIFRAME_END   code/<seq>,   remainder
```

The tool must answer **every** frame of the transfer with `NORMAL_ACK`,
payload `0x00`, carrying the *original* command code/subcode — not the
sequence number. A transfer where the acks are missing stalls after the first
few frames.

### Long write

```
tool -> device   WRITE_CMD        code/subcode, payload[0] = total length
tool -> device   MULTIFRAME_START code/subcode, first 8 bytes
tool -> device   MULTIFRAME       0x00/<seq>,   8 bytes
...
tool -> device   MULTIFRAME_END   0x00/<seq>,   remainder
device -> tool   NORMAL_ACK       code/subcode
```

Frames are spaced ~20 ms apart. The drive unit drops frames sent faster.

## Configuration blocks

Three 64-byte blocks on the drive unit, each with the checksum (low byte of
the sum of bytes 0..62) in byte 63.

| Block | Command | Contents |
| --- | --- | --- |
| Parameter0 | 0x60/0x10 | per-level acceleration, per-level assist ratio, ratio upper limit |
| Parameter1 | 0x60/0x11 | battery limits, motor constants, sensors, 9 assist levels, walk assist |
| Parameter2 | 0x60/0x12 | 6 torque profiles, global acceleration level |

Speed parameters live outside those blocks at 0x32/0x03 and have no checksum:
`speed_limit` (0.01 km/h, LE u16), wheel code (2 bytes, table in
`constants.WHEEL_TABLE`), circumference in mm (LE u16).

### Known layout disagreement

`OpenBafangTool` reads Parameter1 bytes 3, 4 and 5 as three single-byte volt
values (`undervoltage`, `undervoltage_under_load`, `battery_recovery_voltage`).
`bafang_canable_pro` reads bytes 3..4 and 5..6 as two LE u16 values in 0.1 V.
This tool uses the 16-bit reading and keeps the four raw bytes in
`Parameter1.undervoltage_bytes` so you can check which one your unit uses
before writing anything voltage related.

`OpenBafangTool` also *reads* the per-level speed limits at offset 49+i but
*writes* them at 48+i. That write offset is a bug; 49+i is used here.

## Live data

| Command | Payload |
| --- | --- |
| 0x32/0x00 | SOC %, trip (0.01 km), cadence, torque (u16), remaining range (0.01 km, 0xFFFF = unknown) |
| 0x32/0x01 | speed (0.01 km/h), current (0.01 A), voltage (0.01 V), controller °C+40, motor °C+40 (0xFF = none) |
| 0x31/0x00 | torque sensor: torque u16, cadence, rolling frame counter (byte 3) |
| 0x34/0x01 | battery: current (signed 0.01 A), voltage (0.01 V), °C+40 |
| 0x12/0x00 | drive unit: live fault code — **disputed**, see below |
| 0x30/0x00 | drive unit: uptime, LE u32 of ~10.026 s ticks since power-on |
| 0x63/0x00 | display → drive unit at 100 Hz: assist levels, level code, buttons |
| 0x63/0x03 | display → drive unit: idle minutes before shutdown |

`0x31/0x00` byte 3 and `0x30/0x00` are decoded here and in no vendored
project. `0x63/0x00` and `0x63/0x03` are pushed by the display without being
asked, so they are readable in listen-only mode.

### A second layout disagreement, on 0x12/0x00

This tool reads the single byte as a **live fault code**: it held `07` on a
bike displaying error 07 and `00` after the fault was repaired, with the
display echoing the same value on `0x13/0x00`.
`OpenSourceEBike/Bafang_M500_M600` reads it as a **bitfield** — bit 0 brake,
bit 1 motor stopped, bit 2 undervoltage. Both agree that zero is healthy and
they cannot both be right above that. Squeezing the brake lever while
capturing separates them; nobody has done it. `codecs.ControllerState` records
the dispute.

## Error codes

Read at 0x60/0x07 as an ASCII digit string, two digits per code (`"0821"` =
codes 8 and 21). Writing to the same code/subcode clears them. Only codes 8,
14 and 21 have a documented meaning in either upstream project; this tool
reports the rest as "known code, no description" rather than guessing.
