# CANable Pro 2.0 setup

## The adapter

The CANable Pro 2.0 / CANable2 is an STM32G431 with a galvanically isolated
transceiver. Which USB protocol it speaks depends on the firmware image
flashed on it, and different vendors ship different images on the same board:

* **candleLight-FD** speaks the `gs_usb` USB protocol over a vendor-specific
  interface — `--interface gs_usb`, the default.
* **slcan** enumerates as a USB CDC ACM serial port —
  `--interface slcan --channel /dev/tty.usbmodemXXXX`.

**The USB vendor/product id does not tell you which.** A verified example: an
Openlight Labs CANable2 reporting `16d0:117e` — an id other batches use for
gs_usb boards — carries the slcan image from
`github.com/normaldotcom/canable2.git`. Its USB config has two CDC interfaces
(classes `0x02` and `0x0a`) and no vendor-specific one, and `GsUsb.scan()`
does not see it at all. So run `bafang-can adapters` and use the command it
prints, rather than assuming from the product name:

```
slcan: CANable2 b158aa7 github.com/normaldotcom/canable2.git
    open with: bafang-can --interface slcan --channel /dev/cu.usbmodemXXXXX scan
```

Note that the older `bafang_canable_pro` README asks for an STM32F072-based
CANable v1 with candlelight firmware. The Pro 2.0 works with this tool on
either firmware; the Bafang bus is classic CAN at 250 kbit/s, so the FD
capability of the Pro 2.0 is not used.

### Expect an STM32G431 board to be running slcan

`--interface gs_usb` is this tool's default, but on a G431 board slcan is the
more likely firmware, for a reason worth knowing before you plan around it:

* [`candle-usb/candleLight_fw`](https://github.com/candle-usb/candleLight_fw),
  the upstream gs_usb firmware, does not support the part. Its own README:
  "STM32G431-based devices (e.g. CANable-MKS 2.0) are not supported by this
  project yet." Its prebuilt binaries are F042/F072 and STM32G0B1.
* [`normaldotcom/canable2`](https://github.com/normaldotcom/canable2), the
  firmware Openlight Labs boards ship with, is slcan only. It builds no
  candleLight variant, so the vendor image cannot give you gs_usb either.

The only gs_usb firmware for a G431 that this project is aware of is the
third-party HUD ECU Hacker build (`STM32G431-Candlelight2.5-Multiboard.dfu`)
from [netcult.ch](https://www.netcult.ch/elmue/CANable%20Firmware%20Update/),
which is what the vendored `bafang_canable_pro` README recommends. Single
source; weigh that before reflashing.

### Reflashing to gs_usb

Worth doing if you need listen-only that works (see below) or a transmit
self-test; not worth doing just to use the default interface.

1. Fit the BOOT jumper, or hold the BOOT button, and plug in USB. BOOT0 is
   sampled only at reset, so the jumper can stay fitted through the flash.
2. Confirm the ROM bootloader took over: `dfu-util -l` should list
   `[0483:df11]` with `alt=0 @Internal Flash /0x08000000/64*02Kg`.
3. `dfu-util -w -c 1 -i 0 -a 0 -D STM32G431-Candlelight2.5-Multiboard.dfu`
4. Unplug, **remove the jumper**, plug back in, and check with
   `bafang-can adapters` that a `gs_usb:` line replaced the `slcan:` one.

Only ever write `-a 0`. `alt=1` is the option bytes, where read-out protection
lives -- setting RDP level 2 there is irreversible, unlike a bad application
image, which the BOOT jumper always recovers. `alt=2` is one-time programmable.
Neither is part of a firmware update.

Backing out costs nothing: entering the bootloader does not modify flash, so
unplug, remove the jumper and replug returns the board to the image it had.

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
straight into `bafang-can decode-log`. It is the default here only because it
is the more common factory image; if your board carries slcan, as the one
tested here does, there is no reason to reflash it.

### Listen-only does not work on the canable2 slcan firmware

Measured on an Openlight Labs CANable2 running
`github.com/normaldotcom/canable2.git`, against a bus carrying ~165 frames/s:

| mode | frames received in 2 s |
| --- | --- |
| listen-only (`L`) | 0, on every one of four trials |
| normal (`O`) | 330, 330, 331, 330 |

So `sniff --passive` and `monitor --passive` receive **nothing** on this
firmware, and nothing warns you: the adapter acknowledges no slcan command, so
python-can cannot tell a channel that opened in listen-only from one that
never opened. Verified on the wire that the tool does send `S5 L`, so the
request reaches the adapter and the adapter is what ignores it.

Earlier in the same session, two `sniff --passive` captures on the same
hardware *did* record traffic (367 and 177 frames). That contradicts the table
above and is not explained. Until it is, treat `--passive` on an slcan board
as unreliable rather than merely broken, and use gs_usb if you need a
guaranteed non-transmitting listen.

One thing only gs_usb gives you: `GS_CAN_MODE_LOOP_BACK`, an internal loopback
where transmitted frames come straight back with no bus and no second node to
acknowledge them. That is the only way to test the transmit path before the
bike harness exists. slcan has no equivalent, so on an slcan board the
transmit path stays unproven until you are wired to the bike.

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

It prints the exact `--interface`/`--channel` arguments that open what it
found; use those on every command below. It classifies by what the device
actually exposes, not by its USB id, so `gs_usb:` means the gs_usb backend can
really open it and `slcan:` means the board carries the serial firmware.

Then, in this order:

1. `bafang-can sniff --passive --seconds 10` on the powered-on bike. Frames
   appearing proves wiring, polarity and bit rate before you transmit anything.
   `--passive` puts the CAN controller in listen-only mode, so the adapter
   cannot drive the bus at all — not even the dominant ACK bit that a
   normal-mode controller sends for every frame it hears.
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
