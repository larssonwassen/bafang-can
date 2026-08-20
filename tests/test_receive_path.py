"""The receive thread must stay tight against the wire.

A capture recorded before this was split lost 53% of the torque sensor's
broadcasts. The cause was not the bus and not the adapter: listeners ran on the
receive thread, so printing a line or writing a log entry held the reader off
the serial port long enough for the kernel buffer to overflow, and the frames
were discarded before anything in this program saw them.
"""

from __future__ import annotations

import threading
import time

import can
import pytest

from bafang_can.constants import CanOperation, DeviceId
from bafang_can.frame import BafangId
from bafang_can.protocol import BafangClient


def sensor_frame(counter: int) -> can.Message:
    return can.Message(
        arbitration_id=BafangId(
            source=int(DeviceId.TORQUE_SENSOR),
            target=int(DeviceId.BROADCAST),
            operation=int(CanOperation.WRITE_CMD),
            code=0x31,
            subcode=0x00,
        ).encode(),
        data=bytes([0xF1, 0x04, 0x00, counter]),
        is_extended_id=True,
        timestamp=1.0 + counter * 0.0115,
    )


class ScriptedBus:
    """Yields a fixed list of frames, then blocks like an idle bus."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.drained = threading.Event()
        self.recv_calls = 0

    def recv(self, timeout=None):
        self.recv_calls += 1
        if self._messages:
            return self._messages.pop(0)
        self.drained.set()
        time.sleep(0.005)
        return None

    def send(self, message, timeout=None):
        pass

    def shutdown(self):
        pass


@pytest.fixture()
def scripted():
    """Build a client over a scripted bus. Not started: the caller registers
    its listeners first, or the frames drain before anything is listening."""
    created: list[BafangClient] = []

    def build(messages, listener=None):
        bus = ScriptedBus(messages)
        client = BafangClient(bus, send_acks=False)
        created.append(client)
        if listener is not None:
            client.add_listener(listener)
        client.start()
        return bus, client

    yield build
    for client in created:
        client.close()


def test_a_slow_listener_does_not_hold_up_the_reader(scripted):
    """The reader must finish draining while the listener is still working."""
    released = threading.Event()
    first_seen = threading.Event()

    def slow(message):
        first_seen.set()
        released.wait(timeout=5)

    bus, client = scripted([sensor_frame(n) for n in range(50)], slow)
    try:
        assert first_seen.wait(timeout=5), "the listener never ran"
        # The listener is now parked inside the first message. If dispatch were
        # inline, the bus would still hold 49 unread frames.
        assert bus.drained.wait(timeout=5), "the reader stalled behind the listener"
        assert client.quality.frames == 50
        assert client.quality.lost == 0
    finally:
        released.set()
        client.close()


def test_every_message_still_reaches_the_listener(scripted):
    seen: list[int] = []
    done = threading.Event()

    def collect(message):
        seen.append(message.data[3])
        if len(seen) == 30:
            done.set()

    _bus, client = scripted([sensor_frame(n) for n in range(30)], collect)
    try:
        assert done.wait(timeout=5), f"only {len(seen)} of 30 messages arrived"
        assert seen == list(range(30))
    finally:
        client.close()


def test_messages_queued_at_shutdown_are_still_delivered(scripted):
    """A capture that ends on a burst must still write every frame it has."""
    seen: list[int] = []
    gate = threading.Event()

    def blocked(message):
        gate.wait(timeout=5)
        seen.append(message.data[3])

    bus, client = scripted([sensor_frame(n) for n in range(20)], blocked)
    try:
        assert bus.drained.wait(timeout=5)
    finally:
        gate.set()
        client.close()

    assert len(seen) == 20


def test_a_corrupt_identifier_is_dropped_and_counted(scripted):
    """python-can's slcan backend parses the id with no range check."""
    corrupt = can.Message(
        arbitration_id=0x2F830100,
        data=bytes.fromhex("04d40f"),
        is_extended_id=True,
        timestamp=1.0,
    )
    seen: list[object] = []

    bus, client = scripted([corrupt, sensor_frame(1), corrupt], seen.append)
    try:
        assert bus.drained.wait(timeout=5)
        time.sleep(0.1)  # let the dispatch thread catch up
        assert client.quality.invalid_ids == 2
        assert len(seen) == 1
    finally:
        client.close()


def test_frame_loss_is_recorded_as_it_happens(scripted):
    """The sensor's counter makes loss visible live, not only in a log."""
    bus, client = scripted([sensor_frame(0x0E), sensor_frame(0x10)])
    try:
        assert bus.drained.wait(timeout=5)
        assert client.quality.lost == 1
        assert not client.quality.healthy
    finally:
        client.close()


def error_frame(classes: int, data: bytes) -> can.Message:
    """What a driver hands up when the controller reports trouble.

    python-can masks the error flag off the identifier and sets
    ``is_error_frame`` instead, so the class bits arrive in
    ``arbitration_id`` -- which is why the live path can name the fault and a
    candump log cannot.
    """
    return can.Message(
        arbitration_id=classes,
        data=data,
        is_extended_id=False,
        is_error_frame=True,
        timestamp=2.0,
    )


def test_an_error_frame_is_counted_rather_than_discarded(scripted):
    """It used to be dropped one line before anything could count it.

    An error frame is the only direct evidence the bus produces about its own
    electrical health, and on this bike they arrive in bursts under motor
    load.
    """
    # CAN_ERR_PROT | CAN_ERR_BUSERROR, data[2] = frame format error.
    frame = error_frame(0x88, bytes.fromhex("0000020000000001"))
    seen: list[object] = []

    bus, client = scripted([frame, sensor_frame(1), frame], seen.append)
    try:
        assert bus.drained.wait(timeout=5)
        time.sleep(0.1)  # let the dispatch thread catch up
        assert client.quality.bus_errors == 2
        assert client.quality.invalid_ids == 0
        assert len(seen) == 1, "an error frame is not a message from a node"
    finally:
        client.close()


def test_the_live_path_can_name_the_error_class(scripted):
    """The identifier survives here, so the report is specific."""
    frame = error_frame(0x88, bytes.fromhex("0000040000000001"))

    bus, client = scripted([frame])
    try:
        assert bus.drained.wait(timeout=5)
        time.sleep(0.1)
        described = client.quality.errors[0].describe()
    finally:
        client.close()

    assert "protocol violation" in described
    assert "bit stuffing error" in described
