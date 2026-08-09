"""CAN adapter handling, built around python-can.

Target adapter is the **CANable Pro 2.0** (STM32G431, galvanically isolated).
It ships with candleLight-FD firmware, which speaks the ``gs_usb`` USB
protocol, so the default interface here is ``gs_usb``. If the adapter has been
reflashed to the slcan/CANtact firmware it enumerates as a USB CDC serial port
instead and ``--interface slcan`` is the right choice.

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

#: USB ids used by CANable / candleLight class devices.
KNOWN_USB_IDS = (
    (0x1D50, 0x606F),  # candleLight / CANable (gs_usb)
    (0x1209, 0x2323),  # CANable slcan (CDC ACM)
    (0x16D0, 0x117E),  # CANable Pro (some batches)
    (0x0483, 0x374B),  # STM32 CDC (reflashed boards)
)

DEFAULT_INTERFACE = "gs_usb"


@dataclass
class AdapterConfig:
    interface: str = DEFAULT_INTERFACE
    channel: str | int | None = None
    bitrate: int = BITRATE
    #: gs_usb device index when several adapters are attached.
    index: int = 0
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
        if self.extra:
            kwargs.update(self.extra)
        return kwargs


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


def list_gs_usb_devices() -> list[str]:
    """Descriptions of attached gs_usb (candleLight class) devices.

    Raises :class:`MissingLibusb` when pyusb has no backend -- that is a setup
    problem with a specific fix, not "no adapter attached".
    """
    try:
        import usb.core  # type: ignore import-not-found
    except ImportError:  # pragma: no cover - optional dependency
        return []
    found = []
    for vid, pid in KNOWN_USB_IDS:
        try:
            devices = usb.core.find(find_all=True, idVendor=vid, idProduct=pid) or []
        except usb.core.NoBackendError as exc:  # pragma: no cover - host setup
            raise MissingLibusb(
                "pyusb found no libusb backend. Install it with "
                "'brew install libusb' on macOS, or "
                "'apt install libusb-1.0-0' on Debian/Ubuntu."
            ) from exc
        for dev in devices:
            try:
                name = dev.product or f"{vid:04x}:{pid:04x}"
            except Exception:  # pragma: no cover - permissions vary by OS
                name = f"{vid:04x}:{pid:04x}"
            found.append(f"{name} (bus {dev.bus} addr {dev.address})")
    return found


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
        return can.Bus(**kwargs)
    except Exception as exc:
        raise RuntimeError(_diagnose(config, exc)) from exc


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
                "No candleLight/gs_usb device found on USB. Check that the "
                "CANable is plugged in and running candleLight(-FD) firmware, "
                "and that libusb is installed (macOS: 'brew install libusb')."
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


def describe_adapters() -> Iterable[str]:
    """Human readable list of everything that could be the adapter."""
    try:
        for name in list_gs_usb_devices():
            yield f"gs_usb: {name}"
    except MissingLibusb as exc:
        yield f"gs_usb: cannot enumerate -- {exc}"
    for device, desc in list_serial_ports():
        yield f"slcan: {device} ({desc})"
