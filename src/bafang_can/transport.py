"""CAN adapter handling, built around python-can.

Target adapter is the **CANable Pro 2.0 / CANable2** (STM32G431, galvanically
isolated). Which USB protocol it speaks depends on which firmware image is
flashed, and both are shipped by different vendors on the same board:

* the candleLight-FD image speaks ``gs_usb`` -- a vendor-specific USB
  interface, opened with ``--interface gs_usb``;
* the slcan image enumerates as a USB CDC ACM serial port and is opened with
  ``--interface slcan --channel /dev/tty...``.

You cannot tell them apart by USB vendor/product id: the Openlight Labs
CANable2 (``16d0:117e``) ships the slcan image under an id that other batches
use for gs_usb. :func:`find_adapters` therefore classifies by what the device
actually exposes -- ``GsUsb.scan()``, which is the same enumeration
python-can's gs_usb backend indexes into, and the USB interface descriptors.

The Bafang bus is classic CAN 2.0B at 250 kbit/s. CAN-FD is never used, so the
FD capability of the Pro 2.0 is irrelevant here beyond needing a firmware that
can fall back to classic frames (candleLight-FD does).

Wiring: the CANable's CAN-H/CAN-L go to the Bafang CAN pair; the adapter must
not supply power to the bike. The bike has its own 120 Ohm terminations at the
ends of the harness, so leave the CANable's termination jumper OFF unless you
are on a bench harness with no other terminator.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .constants import BITRATE

log = logging.getLogger(__name__)

#: USB ids seen on CANable-class boards, whichever firmware they carry.
#: Membership here says "this could be the adapter", never which protocol it
#: speaks -- see :func:`find_adapters` for that.
KNOWN_USB_IDS = (
    (0x1D50, 0x606F),  # candleLight / CANable
    (0x1209, 0x2323),  # CANable / candleLight (Registry id)
    (0x16D0, 0x117E),  # Openlight Labs CANable2
    (0x0483, 0x374B),  # STM32 CDC (reflashed boards)
)

#: USB interface classes that mean "this enumerates as a serial port".
_CDC_CLASSES = (0x02, 0x0A)

DEFAULT_INTERFACE = "gs_usb"


@dataclass
class AdapterConfig:
    interface: str = DEFAULT_INTERFACE
    channel: str | int | None = None
    bitrate: int = BITRATE
    #: gs_usb device index when several adapters are attached.
    index: int = 0
    #: Put the CAN controller in listen-only mode: it never drives the bus,
    #: not even the dominant bit in the ACK slot of frames it receives. This
    #: is what makes a passive sniff genuinely passive.
    listen_only: bool = False
    extra: dict[str, Any] | None = None

    def bus_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "interface": self.interface,
            "bitrate": self.bitrate,
        }
        if self.interface == "gs_usb":
            # python-can's gs_usb backend selects the device by index; the
            # channel argument is only a label.
            kwargs["channel"] = self.channel if self.channel is not None else self.index
            kwargs["index"] = self.index
        elif self.channel is not None:
            kwargs["channel"] = self.channel
        if self.listen_only and self.interface == "slcan":
            # slcan opens the channel with 'L' instead of 'O'.
            kwargs["listen_only"] = True
        if self.extra:
            kwargs.update(self.extra)
        return kwargs


@dataclass(frozen=True)
class Adapter:
    """An attached adapter, described by how it can actually be opened."""

    interface: str
    description: str
    channel: str | None = None
    index: int | None = None
    #: Set when the device is recognisable but not usable as it stands.
    problem: str | None = None

    def command_hint(self) -> str:
        """The ``--interface``/``--channel`` arguments that open this device."""
        if self.channel is not None:
            return f"--interface {self.interface} --channel {self.channel}"
        if self.index:
            return f"--interface {self.interface} --index {self.index}"
        return f"--interface {self.interface}"


def list_serial_ports() -> list[tuple[str, str]]:
    """Serial ports that look like a CAN adapter, as (device, description)."""
    try:
        from serial.tools import list_ports  # type: ignore import-not-found
    except ImportError:  # pragma: no cover - optional dependency
        return []
    ports = []
    for port in list_ports.comports():
        looks_right = (port.vid, port.pid) in KNOWN_USB_IDS or "canable" in (
            port.description or ""
        ).lower()
        if looks_right or "usbmodem" in port.device or "ttyACM" in port.device:
            ports.append((port.device, port.description or ""))
    return ports


class MissingLibusb(RuntimeError):
    """libusb is not installed, so gs_usb devices cannot be enumerated."""


_MISSING_LIBUSB = (
    "pyusb found no libusb backend. Install it with "
    "'brew install libusb' on macOS, or "
    "'apt install libusb-1.0-0' on Debian/Ubuntu."
)


def _device_name(dev, vid: int, pid: int) -> str:
    try:
        return dev.product or f"{vid:04x}:{pid:04x}"
    except Exception:  # pragma: no cover - permissions vary by OS
        return f"{vid:04x}:{pid:04x}"


def _speaks_cdc(dev) -> bool:
    """True when the device exposes a USB CDC serial interface."""
    try:
        for cfg in dev:
            for intf in cfg:
                if intf.bInterfaceClass in _CDC_CLASSES:
                    return True
    except Exception:  # pragma: no cover - descriptors need permissions on some hosts
        return False
    return False


def list_gs_usb_devices() -> list[str]:
    """Descriptions of adapters that really do speak the gs_usb protocol.

    Authority is ``GsUsb.scan()``: it is the same enumeration python-can's
    gs_usb backend indexes into, so anything it lists is something
    ``--interface gs_usb`` can open, and anything it omits is not -- regardless
    of what the USB vendor/product id suggests.

    Raises :class:`MissingLibusb` when pyusb has no backend -- that is a setup
    problem with a specific fix, not "no adapter attached".
    """
    try:
        import usb.core  # type: ignore import-not-found
        from gs_usb.gs_usb import GsUsb  # type: ignore import-not-found
    except ImportError:  # pragma: no cover - optional dependency
        return []
    try:
        devices = GsUsb.scan()
    except usb.core.NoBackendError as exc:  # pragma: no cover - host setup
        raise MissingLibusb(_MISSING_LIBUSB) from exc
    found = []
    for gs in devices:
        dev = gs.gs_usb
        name = _device_name(dev, dev.idVendor, dev.idProduct)
        found.append(f"{name} (bus {dev.bus} addr {dev.address})")
    return found


def _unusable_usb_devices() -> list[Adapter]:
    """CANable-looking USB devices that are neither gs_usb nor a serial port.

    A board whose firmware exposes a vendor-specific interface that
    ``GsUsb.scan()`` does not recognise lands here: visible, plausibly the
    adapter, and openable by nothing. Saying so beats silence.
    """
    try:
        import usb.core  # type: ignore import-not-found
        from gs_usb.gs_usb import GsUsb  # type: ignore import-not-found
    except ImportError:  # pragma: no cover - optional dependency
        return []
    try:
        known = {(g.gs_usb.bus, g.gs_usb.address) for g in GsUsb.scan()}
    except usb.core.NoBackendError:  # pragma: no cover - host setup
        raise MissingLibusb(_MISSING_LIBUSB) from None
    unusable = []
    for vid, pid in KNOWN_USB_IDS:
        for dev in usb.core.find(find_all=True, idVendor=vid, idProduct=pid) or []:
            if (dev.bus, dev.address) in known or _speaks_cdc(dev):
                continue
            unusable.append(
                Adapter(
                    interface="gs_usb",
                    description=f"{_device_name(dev, vid, pid)} "
                    f"({vid:04x}:{pid:04x}, bus {dev.bus} addr {dev.address})",
                    problem="not recognised as a gs_usb device and it exposes no "
                    "serial port, so no backend here can open it. Check which "
                    "firmware it carries.",
                )
            )
    return unusable


def open_bus(config: AdapterConfig):
    """Open a python-can bus, raising a readable error if that is not possible."""
    if config.interface == "sim":
        from .simulator import SimBus

        extra = config.extra or {}
        return SimBus(**extra)

    try:
        import can  # type: ignore import-not-found
    except ImportError as exc:  # pragma: no cover - dependency check
        raise RuntimeError(
            "python-can is not installed. Install this package with "
            "'pip install -e .' or 'pip install python-can'."
        ) from exc

    kwargs = config.bus_kwargs()
    log.debug("opening CAN bus: %s", kwargs)
    try:
        with _relaxed_sample_point(config.interface), _tolerant_detach(config.interface):
            bus = _open_with_retry(can, kwargs, config.interface)
    except Exception as exc:
        raise RuntimeError(_diagnose(config, exc)) from exc

    if config.listen_only:
        try:
            _apply_listen_only(bus, config.interface)
        except Exception:
            # Half-listening is worse than not listening: the caller asked for
            # a mode where the adapter cannot disturb the bus, so refuse
            # rather than hand back a bus that still ACKs.
            bus.shutdown()
            raise
    return bus


#: How long to keep retrying a gs_usb open while the device re-enumerates.
GS_USB_OPEN_ATTEMPTS = 6
GS_USB_OPEN_PAUSE = 0.5


@contextmanager
def _tolerant_detach(interface: str):
    """Do not let a failed kernel-driver detach abort a gs_usb open.

    ``gs_usb.start()`` detaches the kernel driver from interface 0 whenever the
    OS reports one attached. Detaching is a Linux concept; on macOS the call
    returns "[Errno 13] Access denied" and libusb goes on to claim the
    interface perfectly well without it. Measured on a candleLight 2.5 board:
    every open raises this, and every open succeeds once it is ignored.

    Only failures of the detach itself are swallowed. If the interface really
    cannot be claimed, the claim that follows still raises.
    """
    if interface != "gs_usb":
        yield
        return

    try:
        import usb.core  # type: ignore import-not-found
    except ImportError:  # pragma: no cover - optional dependency
        yield
        return

    original = usb.core.Device.detach_kernel_driver

    def tolerant(self, number):
        try:
            return original(self, number)
        except usb.core.USBError as exc:
            log.debug("ignoring kernel-driver detach failure: %s", exc)
            return None

    usb.core.Device.detach_kernel_driver = tolerant
    try:
        yield
    finally:
        usb.core.Device.detach_kernel_driver = original


def _open_with_retry(can, kwargs: dict[str, Any], interface: str):
    """Open the bus, riding out the re-enumeration a gs_usb start provokes.

    ``gs_usb.start()`` issues a USB reset, so the adapter disappears and comes
    back at a new address. A command run straight after another then races that
    re-enumeration and fails with "[Errno 19] No such device" even though
    nothing is wrong. Measured: consecutive opens fail without this and all
    succeed with it.
    """
    if interface != "gs_usb":
        return can.Bus(**kwargs)

    last: Exception | None = None
    for attempt in range(GS_USB_OPEN_ATTEMPTS):
        try:
            return can.Bus(**kwargs)
        except Exception as exc:
            last = exc
            if attempt + 1 < GS_USB_OPEN_ATTEMPTS:
                # Back off progressively: how long the device stays away
                # varies, and a fixed short pause loses the race.
                pause = GS_USB_OPEN_PAUSE * (attempt + 1)
                log.debug(
                    "gs_usb open attempt %d failed (%s); the adapter is "
                    "probably still re-enumerating, retrying in %.1fs",
                    attempt + 1,
                    exc,
                    pause,
                )
                time.sleep(pause)
    assert last is not None
    raise last


#: Sample points to try, in order. 87.5% is the CiA recommendation and what
#: python-can's gs_usb backend asks for; the rest are ordinary values a CAN
#: bus runs happily at when the first is arithmetically out of reach.
SAMPLE_POINTS = (87.5, 85.0, 80.0, 75.0)


@contextmanager
def _relaxed_sample_point(interface: str):
    """Let the gs_usb backend fall back when 87.5% has no solution.

    python-can's gs_usb backend hardcodes an 87.5% sample point and gives up
    if no bit timing hits it. Whether one exists depends on the adapter's CAN
    clock, so a perfectly ordinary bitrate can be unreachable: measured on a
    candleLight 2.5 board whose clock is 160 MHz, 250 kbit/s needs 640/brp
    time quanta, and python-can's classic-CAN rules (prescaler <= 32, bit time
    <= 25 quanta, tseg1 <= 16) leave only 20 quanta, where 87.5% would need a
    tseg1 of 17. So ``--interface gs_usb --bitrate 250000`` could not open at
    all, while 85% hits 250000 bit/s exactly.

    This widens the search rather than changing the answer: 87.5% is still
    tried first and still wins whenever it is achievable.
    """
    if interface != "gs_usb":
        yield
        return

    try:
        import can  # type: ignore import-not-found
    except ImportError:  # pragma: no cover - dependency check happens in open_bus
        yield
        return

    original = can.BitTiming.from_sample_point

    def with_fallback(f_clock, bitrate, sample_point=87.5):
        attempts = (sample_point, *(p for p in SAMPLE_POINTS if p != sample_point))
        for point in attempts:
            try:
                timing = original(f_clock=f_clock, bitrate=bitrate, sample_point=point)
            except ValueError:
                continue
            if point != sample_point:
                log.info(
                    "no bit timing for %d bit/s at a %.1f%% sample point on a "
                    "%d Hz clock; using %.1f%% instead (brp=%d tseg1=%d tseg2=%d)",
                    bitrate, sample_point, f_clock, point,
                    timing.brp, timing.tseg1, timing.tseg2,
                )
            return timing
        raise ValueError(
            f"no bit timing for {bitrate} bit/s on this adapter's {f_clock} Hz "
            f"clock at any of the sample points {SAMPLE_POINTS}."
        )

    can.BitTiming.from_sample_point = staticmethod(with_fallback)
    try:
        yield
    finally:
        can.BitTiming.from_sample_point = original


#: gs_usb control request that sets the device mode, and its "start" value.
_GS_USB_BREQ_MODE = 2
_GS_USB_MODE_START = 1


def _set_gs_usb_mode(gs, flags: int, required: int = 0) -> int:
    """Change the mode of an already-open gs_usb device.

    ``GsUsb.start()`` looks like the way to do this, but it begins with a USB
    reset. That invalidates the handle of a bus we have already opened, and on
    macOS the device re-enumerates at a new address, so the call fails with
    "[Errno 19] No such device" and the bus is left unusable. Sending the mode
    control transfer that ``start()`` would send, without the reset, changes
    the mode in place. Verified on a candleLight 2.5 board by putting an open
    bus into loopback and reading its own frames back.

    Returns the flags the device actually granted, which is the requested set
    masked by what it reports supporting. ``required`` names the bits that
    carry the point of the call -- losing an optional extra like hardware
    timestamping is fine, losing listen-only is not.
    """
    feature = gs.device_capability.feature
    granted = flags & feature
    if required and granted & required != required:
        raise RuntimeError(
            f"this adapter does not support the requested mode "
            f"(asked 0x{required:x}, supports 0x{feature:x})"
        )
    from gs_usb.gs_usb_structures import DeviceMode  # type: ignore import-not-found

    gs.device_flags = granted
    gs.gs_usb.ctrl_transfer(
        0x41, _GS_USB_BREQ_MODE, 0, 0, DeviceMode(_GS_USB_MODE_START, granted).pack()
    )
    return granted


def _apply_listen_only(bus, interface: str) -> None:
    """Put an already-open bus into listen-only mode.

    slcan handles this in :meth:`AdapterConfig.bus_kwargs` (python-can opens
    the channel with ``L``). gs_usb has the mode bit but python-can's backend
    always starts the device with ``GS_CAN_MODE_NORMAL``, so restart it here;
    the bit timing was set by a separate control request and survives.
    """
    if interface == "slcan":
        return  # already opened with 'L'
    if interface == "gs_usb":
        from gs_usb.constants import (  # type: ignore import-not-found
            GS_CAN_MODE_HW_TIMESTAMP,
            GS_CAN_MODE_LISTEN_ONLY,
        )

        gs = getattr(bus, "gs_usb", None)
        if gs is None:  # pragma: no cover - depends on python-can internals
            raise RuntimeError(
                "this python-can gs_usb backend does not expose the device, "
                "so listen-only mode cannot be set"
            )
        _set_gs_usb_mode(
            gs,
            GS_CAN_MODE_LISTEN_ONLY | GS_CAN_MODE_HW_TIMESTAMP,
            required=GS_CAN_MODE_LISTEN_ONLY,
        )
        return
    if interface == "sim":
        return  # the simulator has no bus to disturb
    raise RuntimeError(
        f"listen-only mode is not available on --interface {interface}. "
        "On SocketCAN set it on the link instead: "
        "'sudo ip link set can0 type can bitrate 250000 listen-only on'."
    )


def _diagnose(config: AdapterConfig, exc: Exception) -> str:
    lines = [f"Could not open the CAN adapter ({config.interface}): {exc}"]
    if config.interface == "gs_usb":
        try:
            devices = list_gs_usb_devices()
        except MissingLibusb as missing:
            lines.append(str(missing))
            devices = []
        if devices:
            lines.append("gs_usb devices seen on USB: " + ", ".join(devices))
            lines.append(
                "If opening still fails, another process may hold the device, "
                "or libusb has no permission to claim it."
            )
        else:
            lines.append(
                "No gs_usb device found on USB. Check that the CANable is "
                "plugged in and running candleLight(-FD) firmware, and that "
                "libusb is installed (macOS: 'brew install libusb')."
            )
        ports = list_serial_ports()
        if ports:
            lines.append(
                "A serial CAN adapter is present: "
                + ", ".join(f"{d} ({desc})" for d, desc in ports)
                + " -- that firmware is slcan; retry with "
                f"--interface slcan --channel {ports[0][0]}"
            )
    elif config.interface == "slcan":
        ports = list_serial_ports()
        lines.append(
            "Serial ports that look like CAN adapters: "
            + (", ".join(d for d, _ in ports) if ports else "none found")
        )
    return "\n".join(lines)


def find_adapters() -> list[Adapter]:
    """Every attached device that could be the adapter, and how to open it.

    A single physical board yields at most one entry: what it enumerates as is
    decided by the firmware on it, not by its USB id.
    """
    adapters: list[Adapter] = []
    try:
        for index, name in enumerate(list_gs_usb_devices()):
            adapters.append(Adapter(interface="gs_usb", description=name, index=index))
    except MissingLibusb as exc:
        adapters.append(
            Adapter(
                interface="gs_usb",
                description="cannot enumerate",
                problem=str(exc),
            )
        )
    else:
        adapters.extend(_unusable_usb_devices())
    for device, desc in list_serial_ports():
        adapters.append(
            Adapter(interface="slcan", description=desc or device, channel=device)
        )
    return adapters


def describe_adapters() -> Iterable[str]:
    """Human readable list of everything that could be the adapter."""
    for adapter in find_adapters():
        line = f"{adapter.interface}: {adapter.description}"
        if adapter.problem:
            yield f"{line} -- {adapter.problem}"
        else:
            yield f"{line}  [{adapter.command_hint()}]"
