# CANable Pro 2.0 setup

## The adapter

The CANable Pro 2.0 is an STM32G431 with a galvanically isolated transceiver.
It ships with **candleLight-FD** firmware, which speaks the `gs_usb` USB
protocol — that is what `--interface gs_usb` (the default) uses. If your board
has been reflashed to the **slcan/CANtact** firmware it enumerates as a USB CDC
serial port instead; use `--interface slcan --channel /dev/tty.usbmodemXXXX`.

Note that the older `bafang_canable_pro` README asks for an STM32F072-based
CANable v1 with candlelight firmware. The Pro 2.0 works with this tool because
both firmwares expose the same gs_usb interface; the Bafang bus is classic CAN
at 250 kbit/s, so the FD capability of the Pro 2.0 is not used.

### Which firmware, and why it matters

candleLight and gs_usb are not alternatives: candleLight is the firmware on
the adapter's STM32, gs_usb is the USB protocol that firmware speaks (named
after the Linux kernel driver for it). The real choice is between the
candleLight/gs_usb firmware your board ships with and the slcan firmware you
could flash instead.

| | gs_usb (candleLight) | slcan |
| --- | --- | --- |
| Wire format | binary frames | ASCII over a CDC serial port |
| Throughput | limited by the CAN bus | ~3x byte overhead, limited by the UART |
| Timestamps | from the adapter | host-side only |
| Error frames | yes | usually not |
| Linux | native kernel driver, real SocketCAN | needs the `slcand` daemon |
| macOS / Windows | needs libusb | any serial port, no libusb |
| Debugging | opaque binary | human-readable |

For a Bafang bus -- 250 kbit/s, request/response, low volume -- slcan is
entirely adequate. gs_usb is the better choice for sniffing a busy bus, and on
Linux it gives you SocketCAN, which means `candump` captures that feed
straight into `bafang-can decode-log`. It is also what the Pro 2.0 ships with,
so it is the default here purely to avoid making you flash anything.

## Host prerequisites

macOS:

```bash
brew install libusb
```

Debian/Ubuntu:

```bash
sudo apt install libusb-1.0-0
```

Linux users can also load the mainline `gs_usb` kernel driver and use
SocketCAN instead:

```bash
sudo ip link set can0 up type can bitrate 250000
```

then run with `--interface socketcan --channel can0`.

## Wiring

Bafang systems break the CAN pair out on the display/BESST connector (the
higher-density connector at the motor, or the display's diagnostic branch).
You need three things connected:

* CAN-H  → CANable CAN-H
* CAN-L  → CANable CAN-L
* GND    → CANable GND (the isolated side)

The adapter must **not** feed power into the bike, and the bike must be
powered from its own battery while you work — the drive unit does not answer
with the system switched off.

Termination: the bike harness is already terminated at both ends. Leave the
CANable's 120 Ω termination jumper **off** when tapping into a complete bike.
Turn it on only on a bench harness where the motor is the sole other node.

## First contact

```bash
bafang-can adapters      # is the adapter visible at all
bafang-can scan          # which nodes answer
bafang-can probe         # which commands this firmware implements
bafang-can diagnose      # read-only health report
```

If `scan` finds nothing:

1. Confirm the bike is switched on (display lit).
2. Swap CAN-H and CAN-L — reversed pairs are the most common mistake.
3. Confirm 250 kbit/s (`--bitrate 250000`, the default).
4. Run `bafang-can sniff --passive` — if the display and drive unit are
   talking to each other you will see traffic even when nothing answers you,
   which proves the wiring and bit rate are right and points at addressing.

## The day the adapter arrives

Before touching the bike, confirm the host sees the adapter and the tool can
open it:

```bash
bafang-can adapters
```

If it reports a gs_usb device, the firmware is candleLight(-FD) and the
default `--interface gs_usb` is right. If it reports a serial port instead,
the board carries slcan firmware: add `--interface slcan --channel <port>` to
every command below.

Then, in this order:

1. `bafang-can sniff --passive --seconds 10` on the powered-on bike. Frames
   appearing proves wiring, polarity and bit rate before you transmit anything.
2. `bafang-can scan` — which nodes answer the tool.
3. `bafang-can probe` — which commands this firmware actually implements.
   Save the output; it is the map for everything after.
4. `bafang-can dump -o baseline-$(date +%F).json` — back up before any write.
5. `bafang-can capture -o m200-profile.json --anonymize` — record every answer
   the bike gives, so the simulator can replay your motor instead of the
   invented defaults. See [profiles.md](profiles.md).
6. `bafang-can diagnose` — the read-only health report.

Only then start changing things, one field at a time, per
[m200.md](m200.md).

If long writes are refused but short ones work, raise the inter-frame gap:
`--write-delay 0.05`.
