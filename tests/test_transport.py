"""Adapter detection and listen-only mode.

The interesting case is a board whose USB vendor/product id says one thing and
whose firmware does another. A real Openlight Labs CANable2 (``16d0:117e``)
carrying the slcan image is exactly that: the id is in ``KNOWN_USB_IDS``, but
the device exposes two CDC interfaces and ``GsUsb.scan()`` does not see it.
Reporting it as a gs_usb adapter sends the user to an interface that cannot
open it, so these tests fix the classification by what the device exposes.
"""

from __future__ import annotations

import struct
import time

import can
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


class FakeCapability:
    def __init__(self, feature):
        self.feature = feature


class FakeUsbHandle:
    def __init__(self):
        self.transfers = []

    def ctrl_transfer(self, request_type, request, value, index, data):
        self.transfers.append((request_type, request, bytes(data)))
        return len(data)


class FakeGsDevice:
    """A gs_usb device that fails the way the real one does.

    ``GsUsb.start()`` begins with a USB reset, which on real hardware
    invalidates the handle of an already-open bus. Raising here is what makes
    this test able to catch a regression to the stop/start approach.
    """

    def __init__(self, feature=0xFFFF):
        self.device_capability = FakeCapability(feature)
        self.device_flags = None
        self.gs_usb = FakeUsbHandle()

    def stop(self):
        raise AssertionError("stop() must not be called on an open bus")

    def start(self, flags):
        raise AssertionError("start() resets the device and breaks the open bus")


class FakeGsBus:
    def __init__(self, feature=0xFFFF):
        self.gs_usb = FakeGsDevice(feature)


def test_gs_usb_listen_only_is_set_without_resetting_the_device():
    """The reset inside start() kills the handle of an already open bus."""
    from gs_usb.constants import GS_CAN_MODE_LISTEN_ONLY

    bus = FakeGsBus()
    transport._apply_listen_only(bus, "gs_usb")

    (request_type, request, payload), = bus.gs_usb.gs_usb.transfers
    assert request_type == 0x41
    assert request == transport._GS_USB_BREQ_MODE
    assert bus.gs_usb.device_flags & GS_CAN_MODE_LISTEN_ONLY
    # The payload is (mode, flags) little-endian; mode 1 is "start".
    assert payload[:4] == b"\x01\x00\x00\x00"


def test_an_adapter_without_listen_only_support_says_so():
    from gs_usb.constants import GS_CAN_MODE_LISTEN_ONLY

    bus = FakeGsBus(feature=0xFFFF & ~GS_CAN_MODE_LISTEN_ONLY)

    with pytest.raises(RuntimeError, match="does not support the requested mode"):
        transport._apply_listen_only(bus, "gs_usb")


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


# ---------------------------------------------------------------------------
# gs_usb frame timestamps
#
# python-can's two backends disagree about what Message.timestamp means and
# nothing downstream can tell which it got: slcan stamps time.time(), gs_usb
# passes through the adapter's free-running microsecond counter.
# ---------------------------------------------------------------------------


class FakeTimestampBus:
    """A bus that hands out whatever device timestamps it is given."""

    def __init__(self, timestamps):
        self._timestamps = list(timestamps)
        self.shut_down = False

    def recv(self, timeout=None):
        if not self._timestamps:
            return None
        return can.Message(
            arbitration_id=0x02F83201, is_extended_id=True, data=bytes(8),
            timestamp=self._timestamps.pop(0),
        )

    def shutdown(self):
        self.shut_down = True


def test_device_timestamps_become_epoch_times(monkeypatch):
    """A gs_usb counter is uptime, not wall clock. Anchor it to the epoch."""
    monkeypatch.setattr(transport.time, "time", lambda: 1_700_000_000.0)
    bus = transport._WallClockTimestamps(FakeTimestampBus([12.0, 12.5, 13.0]))

    stamps = [bus.recv().timestamp for _ in range(3)]

    assert stamps[0] == 1_700_000_000.0
    # Spacing from the adapter is preserved exactly; only the origin moves.
    assert stamps[1] - stamps[0] == pytest.approx(0.5)
    assert stamps[2] - stamps[0] == pytest.approx(1.0)


def test_the_counter_wrapping_does_not_send_time_backwards(monkeypatch):
    """32 bits of microseconds wraps every 71.6 minutes.

    CanutilsLogWriter clamps any timestamp older than the last one it wrote, so
    an unhandled wrap freezes the clock for the rest of the capture.
    """
    monkeypatch.setattr(transport.time, "time", lambda: 1_700_000_000.0)
    almost = transport.GS_USB_TIMESTAMP_WRAP - 0.001
    bus = transport._WallClockTimestamps(FakeTimestampBus([0.0 + 1e-6, almost, 0.002]))

    stamps = [bus.recv().timestamp for _ in range(3)]

    assert stamps[1] > stamps[0]
    assert stamps[2] > stamps[1]
    assert stamps[2] - stamps[1] == pytest.approx(0.003, abs=1e-4)


def test_an_adapter_without_hardware_timestamps_falls_back_to_host_time():
    """Without the mode bit every frame arrives stamped 0.0.

    Writing a whole capture at time zero is worse than a coarse host time, so
    the real clock is used. Not monkeypatched: patching time.time() globally
    also patches it for logging, which this path uses to warn.
    """
    before = time.time()
    bus = transport._WallClockTimestamps(FakeTimestampBus([0.0, 0.0]))

    stamps = [bus.recv().timestamp for _ in range(2)]

    assert before <= stamps[0] <= stamps[1] <= time.time()
    assert stamps[0] > 1_600_000_000  # an epoch time, not a device counter


def test_attributes_of_the_wrapped_bus_stay_reachable():
    inner = FakeTimestampBus([])
    bus = transport._WallClockTimestamps(inner)

    bus.shutdown()

    assert inner.shut_down


def test_frames_arriving_out_of_order_are_not_mistaken_for_a_wrap():
    """This adapter delivers ~4 frames per 5 s up to 65 ms behind the last.

    Treating any backwards step as a wrap turned a 15 s capture into a
    reported 55849 s one, because each reordering added 4295 s.
    """
    bus = transport._WallClockTimestamps(
        FakeTimestampBus([1.048497, 0.983383, 1.050000])
    )

    stamps = [bus.recv().timestamp for _ in range(3)]

    assert stamps[1] < stamps[0]  # the reordering is preserved, not clamped
    assert stamps[2] - stamps[0] == pytest.approx(0.001503, abs=1e-5)
    # and nothing has been shifted by a wrap
    assert max(stamps) - min(stamps) < 1.0


def test_a_real_wrap_is_still_detected():
    """A genuine wrap drops by nearly the whole counter range, not by 65 ms.

    It has to happen mid-capture to be read as a wrap: a backwards step on the
    second frame is treated as a stale anchor instead, because the adapter only
    ever resets its counter at the start of a session and a wrap 71 minutes in
    cannot land on frame two.
    """
    almost = transport.GS_USB_TIMESTAMP_WRAP - 0.001
    bus = transport._WallClockTimestamps(
        FakeTimestampBus([almost - 0.2, almost - 0.1, almost, 0.002])
    )

    stamps = [bus.recv().timestamp for _ in range(4)]

    assert stamps == sorted(stamps)
    assert stamps[3] - stamps[2] == pytest.approx(0.003, abs=1e-4)


# ---------------------------------------------------------------------------
# a USB frame the gs_usb library cannot unpack
# ---------------------------------------------------------------------------


class ExplodingBus:
    """Raises struct.error on chosen reads, like a short gs_usb frame does."""

    def __init__(self, script):
        self._script = list(script)

    def recv(self, timeout=None):
        if not self._script:
            return None
        item = self._script.pop(0)
        if item == "short":
            raise struct.error("unpack requires a buffer of 24 bytes")
        return can.Message(
            arbitration_id=0x02F83201, is_extended_id=True,
            data=bytes(8), timestamp=item,
        )


def test_a_short_usb_frame_does_not_kill_the_capture():
    """python-can lets struct.error escape recv, ending the run on frame one."""
    bus = transport._SkipMalformedFrames(ExplodingBus(["short", 1.0, 2.0]))

    first = bus.recv()
    rest = [bus.recv(), bus.recv()]

    assert first is None  # the bad frame, dropped
    assert [m.timestamp for m in rest] == [1.0, 2.0]
    assert bus.malformed == 1


def test_repeated_malformed_frames_are_all_counted():
    bus = transport._SkipMalformedFrames(ExplodingBus(["short", "short", 1.0]))

    while bus.recv() is None:
        pass

    assert bus.malformed == 2


def test_the_timestamp_wrapper_tolerates_a_dropped_frame():
    """The two gs_usb wrappers have to compose: None must pass through."""
    bus = transport._WallClockTimestamps(
        transport._SkipMalformedFrames(ExplodingBus(["short", 5.0, 5.5]))
    )

    assert bus.recv() is None
    first, second = bus.recv(), bus.recv()

    assert second.timestamp - first.timestamp == pytest.approx(0.5)


def test_a_stale_first_frame_does_not_freeze_the_capture_clock():
    """The frame we anchor on can be a straggler from before the USB reset.

    Measured: a 30 s capture whose first frame carried an old timestamp had
    every later frame land before the anchor, so CanutilsLogWriter clamped all
    4817 of them to the opening timestamp. The file spanned 5.1 s of frozen
    clock, and 83% of it read as faster than the bus can carry.
    """
    bus = transport._WallClockTimestamps(
        FakeTimestampBus([812.5, 0.010, 0.106, 0.202])
    )

    stamps = [bus.recv().timestamp for _ in range(4)]

    assert stamps == sorted(stamps), "timestamps must not run backwards"
    assert stamps[3] - stamps[1] == pytest.approx(0.192, abs=1e-4)


def test_a_mid_capture_counter_restart_re_anchors():
    bus = transport._WallClockTimestamps(
        FakeTimestampBus([10.0, 10.1, 10.2, 0.05, 0.15])
    )

    stamps = [bus.recv().timestamp for _ in range(5)]

    assert stamps == sorted(stamps)
    assert stamps[4] - stamps[3] == pytest.approx(0.1, abs=1e-4)


def test_frames_queued_before_the_reset_are_drained_at_open():
    """They are indistinguishable from live traffic once recorded.

    A capture that kept them opened with 14 stale sensor broadcasts whose
    counters ran 0x20..0x2E before the live stream resumed at 0x90 -- reported
    as 97 lost frames, which was not what had happened.
    """
    bus = FakeTimestampBus([float(n) for n in range(500)])

    discarded = transport._drain_stale_frames(bus)

    assert discarded > 0
    assert transport.GS_USB_SETTLE == pytest.approx(0.25)


def test_draining_copes_with_a_silent_bus():
    """No traffic at all must not hang the open."""
    bus = FakeTimestampBus([])

    assert transport._drain_stale_frames(bus) == 0
