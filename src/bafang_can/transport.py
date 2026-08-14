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
from collections.abc import Iterable
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
        bus = can.Bus(**kwargs)
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
        gs.stop()
        gs.start(GS_CAN_MODE_LISTEN_ONLY | GS_CAN_MODE_HW_TIMESTAMP)
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
