"""Realtime telemetry taken from broadcasts rather than asked for.

Some firmware answers no realtime read and instead pushes the same payloads
onto the bus unsolicited. Measured on a CR X210.350.FC: ``probe`` reports
ControllerRealtime0/1 and SensorRealtime as unsupported, while all three
arrive continuously as broadcasts, so ``monitor`` timed out on a bus that was
carrying exactly the data it wanted. These are the real frames from that bike.
"""

from __future__ import annotations

import pytest

from bafang_can.cli import _format_realtime, _parse_can_id
from bafang_can.constants import CanOperation, DeviceId
from bafang_can.frame import BafangId, BafangMessage
from bafang_can.system import BroadcastRealtime


class FakeClient:
    """Just enough of BafangClient to register and drive a listener."""

    def __init__(self) -> None:
        self.listeners = []

    def add_listener(self, callback):
        self.listeners.append(callback)

    def broadcast(self, code, subcode, data, source=DeviceId.DRIVE_UNIT, timestamp=1.0):
        message = BafangMessage(
            id=BafangId(
                source=int(source),
                target=int(DeviceId.BROADCAST),
                operation=int(CanOperation.WRITE_CMD),
                code=code,
                subcode=subcode,
            ),
            data=bytes.fromhex(data) if isinstance(data, str) else data,
            timestamp=timestamp,
        )
        for listener in self.listeners:
            listener(message)


@pytest.fixture
def client():
    return FakeClient()


def test_nothing_is_reported_before_the_first_broadcast(client):
    """Absent is not zero: a node that never broadcasts must not be invented."""
    realtime = BroadcastRealtime(client)
    assert realtime.snapshot() == {}


def test_controller_broadcasts_are_decoded(client):
    realtime = BroadcastRealtime(client)
    # Captured from the bike: 32/00 and 32/01 arriving unasked.
    client.broadcast(0x32, 0x00, "37000000f1040410")
    client.broadcast(0x32, 0x01, "000000001e143fff")

    snapshot = realtime.snapshot()

    assert snapshot["controller0"].remaining_capacity == 0x37
    assert snapshot["controller1"].speed == 0.0
    assert "controller0_error" not in snapshot


def test_sensor_and_battery_broadcasts_are_decoded(client):
    realtime = BroadcastRealtime(client)
    client.broadcast(0x31, 0x00, "ed040095", source=DeviceId.TORQUE_SENSOR)
    client.broadcast(0x34, 0x01, "0000a30e3d", source=DeviceId.BATTERY)

    snapshot = realtime.snapshot()

    assert snapshot["sensor"].torque == 0x04ED
    assert snapshot["battery"].voltage == pytest.approx(37.47, abs=0.01)


def test_the_latest_broadcast_wins(client):
    realtime = BroadcastRealtime(client)
    client.broadcast(0x31, 0x00, "ed040095", source=DeviceId.TORQUE_SENSOR, timestamp=1.0)
    client.broadcast(0x31, 0x00, "f1040096", source=DeviceId.TORQUE_SENSOR, timestamp=2.0)

    assert realtime.snapshot()["sensor"].torque == 0x04F1
    assert realtime.seen["sensor"] == 2.0


def test_unrelated_traffic_is_ignored(client):
    realtime = BroadcastRealtime(client)
    client.broadcast(0x63, 0x00, "55150001", source=DeviceId.DISPLAY)
    client.broadcast(0x12, 0x00, "07")

    assert realtime.snapshot() == {}


def test_a_payload_the_codec_rejects_is_surfaced_not_swallowed(client):
    """A short broadcast means this firmware lays the payload out differently."""
    realtime = BroadcastRealtime(client)
    client.broadcast(0x32, 0x01, "0000")

    snapshot = realtime.snapshot()

    assert "controller1" not in snapshot
    assert "needs 8 bytes" in snapshot["controller1_error"]


def test_a_good_broadcast_clears_an_earlier_decode_error(client):
    realtime = BroadcastRealtime(client)
    client.broadcast(0x32, 0x01, "0000")
    client.broadcast(0x32, 0x01, "000000001e143fff")

    snapshot = realtime.snapshot()

    assert "controller1_error" not in snapshot
    assert snapshot["controller1"].voltage == pytest.approx(51.5, abs=0.1)


def test_formatting_says_no_data_rather_than_printing_an_empty_line():
    assert _format_realtime({}) == "no data"


# ---------------------------------------------------------------------------
# cross-checking two independently broadcast voltages
# ---------------------------------------------------------------------------


def test_disagreeing_voltages_are_flagged(client):
    """The real case: 51.5 V from the controller, 37.47 V from the battery."""
    realtime = BroadcastRealtime(client)
    client.broadcast(0x32, 0x01, "000000001e143fff")
    client.broadcast(0x34, 0x01, "0000a30e3d", source=DeviceId.BATTERY)

    warning = realtime.snapshot()["layout_warning"]

    assert "51.50 V" in warning
    assert "37.47 V" in warning
    assert "unverified" in warning


def test_agreeing_voltages_are_not_flagged(client):
    realtime = BroadcastRealtime(client)
    # 0x0EA3 = 3747 -> 37.47 V in the controller's voltage field too.
    client.broadcast(0x32, 0x01, "00000000a30e3fff")
    client.broadcast(0x34, 0x01, "0000a30e3d", source=DeviceId.BATTERY)

    assert "layout_warning" not in realtime.snapshot()


def test_one_voltage_alone_cannot_be_cross_checked(client):
    realtime = BroadcastRealtime(client)
    client.broadcast(0x32, 0x01, "000000001e143fff")

    assert "layout_warning" not in realtime.snapshot()


def test_the_warning_is_shown_in_the_monitor_line():
    line = _format_realtime({"layout_warning": "controller and battery disagree"})

    assert "no data" in line
    assert "! controller and battery disagree" in line


# ---------------------------------------------------------------------------
# decode accepts identifiers in the form every CAN tool prints them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["02F83200", "0x02F83200", "02f83200", "#02F83200", " 02F83200 "],
)
def test_bare_hex_identifiers_are_accepted(text):
    """sniff and candump print bare hex; int(text, 0) rejects the leading 0."""
    assert _parse_can_id(text) == 0x02F83200


def test_hex_is_not_misread_as_decimal():
    assert _parse_can_id("12345678") == 0x12345678


def test_a_non_identifier_exits_with_a_message_not_a_traceback():
    with pytest.raises(SystemExit, match="not a CAN identifier"):
        _parse_can_id("nonsense")
