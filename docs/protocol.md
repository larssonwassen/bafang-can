# Bafang CAN service protocol

Reconstructed from the two vendored projects and expressed in
`src/bafang_can/frame.py`, `commands.py` and `protocol.py`. Nothing here comes
from official Bafang documentation.

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
| 0x31/0x00 | torque sensor: torque u16, cadence |
| 0x34/0x01 | battery: current (signed 0.01 A), voltage (0.01 V), °C+40 |

## Error codes

Read at 0x60/0x07 as an ASCII digit string, two digits per code (`"0821"` =
codes 8 and 21). Writing to the same code/subcode clears them. Only codes 8,
14 and 21 have a documented meaning in either upstream project; this tool
reports the rest as "known code, no description" rather than guessing.
