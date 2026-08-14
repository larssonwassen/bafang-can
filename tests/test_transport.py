"""Adapter detection and listen-only mode.

The interesting case is a board whose USB vendor/product id says one thing and
whose firmware does another. A real Openlight Labs CANable2 (``16d0:117e``)
carrying the slcan image is exactly that: the id is in ``KNOWN_USB_IDS``, but
the device exposes two CDC interfaces and ``GsUsb.scan()`` does not see it.
Reporting it as a gs_usb adapter sends the user to an interface that cannot
open it, so these tests fix the classification by what the device exposes.
"""

from __future__ import annotations

import pytest

from bafang_can import transport
from bafang_can.transport import Adapter, AdapterConfig


class FakeInterface:
    def __init__(self, cls: int) -> None:
        self.bInterfaceClass = cls


class FakeConfig:
    def __init__(self, classes) -> None:
        self._interfaces = [FakeInterface(c) for c in classes]

    def __iter__(self):
        return iter(self._interfaces)


class FakeUsbDevice:
    """Enough of a pyusb device to be classified."""

    def __init__(self, vid, pid, classes, product="CANable2", bus=1, address=1):
        self.idVendor = vid
        self.idProduct = pid
        self.product = product
        self.bus = bus
        self.address = address
        self._configs = [FakeConfig(classes)]

    def __iter__(self):
        return iter(self._configs)


class FakeGsUsb:
    def __init__(self, device):
        self.gs_usb = device


@pytest.fixture
def usb(monkeypatch):
    """Control what pyusb and GsUsb.scan() report, without hardware."""
    state = {"gs_usb": [], "usb": []}

    def fake_scan():
        return [FakeGsUsb(d) for d in state["gs_usb"]]

    def fake_find(find_all=False, idVendor=None, idProduct=None, **kwargs):
        matches = [
            d
            for d in state["usb"]
            if d.idVendor == idVendor and d.idProduct == idProduct
        ]
        return matches if find_all else (matches[0] if matches else None)

    import usb.core
    from gs_usb.gs_usb import GsUsb

    monkeypatch.setattr(GsUsb, "scan", staticmethod(fake_scan))
    monkeypatch.setattr(usb.core, "find", fake_find)
    monkeypatch.setattr(transport, "list_serial_ports", lambda: [])
    return state


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def test_slcan_board_is_not_reported_as_gs_usb(usb, monkeypatch):
    """The regression: 16d0:117e running slcan is a serial port, not gs_usb."""
    board = FakeUsbDevice(0x16D0, 0x117E, classes=[0x02, 0x0A])
    usb["usb"] = [board]  # present on USB, absent from GsUsb.scan()
    monkeypatch.setattr(
        transport,
        "list_serial_ports",
        lambda: [("/dev/cu.usbmodem1", "CANable2")],
    )

    adapters = transport.find_adapters()

    assert [a.interface for a in adapters] == ["slcan"]
    assert transport.list_gs_usb_devices() == []


def test_real_gs_usb_device_is_reported_with_its_index(usb):
    usb["gs_usb"] = [FakeUsbDevice(0x1D50, 0x606F, classes=[0xFF], product="canable")]

    (adapter,) = transport.find_adapters()

    assert adapter.interface == "gs_usb"
    assert adapter.index == 0
    assert "canable" in adapter.description
    assert adapter.problem is None


def test_unopenable_board_is_reported_as_a_problem(usb):
    """Vendor-specific interface, but not one the gs_usb backend can open."""
    usb["usb"] = [FakeUsbDevice(0x16D0, 0x117E, classes=[0xFF])]

    (adapter,) = transport.find_adapters()

    assert adapter.problem is not None
    assert "firmware" in adapter.problem


def test_missing_libusb_is_reported_as_a_setup_problem(usb, monkeypatch):
    import usb.core
    from gs_usb.gs_usb import GsUsb

    def raise_no_backend():
        raise usb.core.NoBackendError("no backend")

    monkeypatch.setattr(GsUsb, "scan", staticmethod(raise_no_backend))

    (adapter,) = transport.find_adapters()

    assert adapter.problem is not None
    assert "libusb" in adapter.problem
    with pytest.raises(transport.MissingLibusb):
        transport.list_gs_usb_devices()


def test_command_hint_names_the_arguments_that_open_the_device():
    slcan = Adapter(interface="slcan", description="x", channel="/dev/cu.usbmodem1")
    assert slcan.command_hint() == "--interface slcan --channel /dev/cu.usbmodem1"
    assert Adapter("gs_usb", "x", index=0).command_hint() == "--interface gs_usb"
    assert Adapter("gs_usb", "x", index=2).command_hint() == "--interface gs_usb --index 2"


# ---------------------------------------------------------------------------
# listen-only
# ---------------------------------------------------------------------------


def test_slcan_listen_only_opens_the_channel_with_L():
    kwargs = AdapterConfig(interface="slcan", channel="/dev/x", listen_only=True).bus_kwargs()
    assert kwargs["listen_only"] is True


def test_listen_only_is_off_unless_asked_for():
    assert "listen_only" not in AdapterConfig(interface="slcan", channel="/dev/x").bus_kwargs()


def test_gs_usb_is_restarted_in_listen_only_mode():
    """python-can always starts gs_usb devices in normal mode; we restart it."""
    from gs_usb.constants import GS_CAN_MODE_LISTEN_ONLY

    class FakeDevice:
        def __init__(self):
            self.calls = []

        def stop(self):
            self.calls.append("stop")

        def start(self, flags):
            self.calls.append(("start", flags))

    class FakeBus:
        def __init__(self):
            self.gs_usb = FakeDevice()

    bus = FakeBus()
    transport._apply_listen_only(bus, "gs_usb")

    assert bus.gs_usb.calls[0] == "stop"
    assert bus.gs_usb.calls[1][0] == "start"
    assert bus.gs_usb.calls[1][1] & GS_CAN_MODE_LISTEN_ONLY


def test_socketcan_listen_only_says_where_to_set_it_instead():
    with pytest.raises(RuntimeError, match="ip link"):
        transport._apply_listen_only(object(), "socketcan")


def test_a_bus_that_cannot_go_listen_only_is_closed_not_returned(monkeypatch):
    """Half-listening is worse than not listening: refuse, do not leak."""

    class FakeBus:
        def __init__(self):
            self.shut_down = False

        def shutdown(self):
            self.shut_down = True

    bus = FakeBus()
    monkeypatch.setattr("can.Bus", lambda **kwargs: bus)

    with pytest.raises(RuntimeError, match="ip link"):
        transport.open_bus(
            AdapterConfig(interface="socketcan", channel="can0", listen_only=True)
        )
    assert bus.shut_down
