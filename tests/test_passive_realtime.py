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
    client.broadcast(0x63, 0x03, "05", source=DeviceId.DISPLAY)
    client.broadcast(0x32, 0x02, "00")

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


# ---------------------------------------------------------------------------
# broadcasts the bus carries that the tool used to ignore
#
# Every payload below is bytes a real G210 system put on the wire, taken from
# tests/data/display-interaction-excerpt.log. The point of each test is a
# cross-check: two nodes that measure the same thing and agree.
# ---------------------------------------------------------------------------


def test_battery_capacity_is_read_from_its_broadcast(client):
    """34/00 was already decodable and simply never wired up."""
    realtime = BroadcastRealtime(client)
    client.broadcast(0x34, 0x00, "682bdf17373164", source=DeviceId.BATTERY)

    capacity = realtime.snapshot()["battery_capacity"]

    assert capacity.full_capacity == 11112  # mAh
    assert capacity.capacity_left == 6111
    assert capacity.soh == 100


def test_the_battery_and_the_drive_unit_agree_about_charge(client):
    """6111/11112 is 55.0%, which is what both nodes independently report.

    Two devices deriving the same number from different fields is the strongest
    evidence available that these layouts are right, short of a reference tool.
    """
    realtime = BroadcastRealtime(client)
    client.broadcast(0x34, 0x00, "682bdf17373164", source=DeviceId.BATTERY)
    client.broadcast(0x32, 0x00, "37000000f1040410")

    snapshot = realtime.snapshot()
    capacity = snapshot["battery_capacity"]
    ratio = capacity.capacity_left / capacity.full_capacity

    assert capacity.rsoc == snapshot["controller0"].remaining_capacity == 55
    assert ratio == pytest.approx(0.55, abs=0.005)


def test_the_sensor_and_the_drive_unit_agree_about_torque(client):
    realtime = BroadcastRealtime(client)
    client.broadcast(0x31, 0x00, "f1040055", source=DeviceId.TORQUE_SENSOR)
    client.broadcast(0x32, 0x00, "37000000f1040410")

    snapshot = realtime.snapshot()

    assert snapshot["sensor"].torque == snapshot["controller0"].torque == 1265


def test_the_sensor_message_counter_is_kept(client):
    """Byte 3 is what makes frame loss detectable; neither vendor reads it."""
    realtime = BroadcastRealtime(client)
    client.broadcast(0x31, 0x00, "f1040055", source=DeviceId.TORQUE_SENSOR)

    assert realtime.snapshot()["sensor"].counter == 0x55


def test_a_three_byte_sensor_payload_has_no_counter_to_report(client):
    """The vendor parsers expect 3 bytes. Absent must not become zero."""
    realtime = BroadcastRealtime(client)
    client.broadcast(0x31, 0x00, "f10400", source=DeviceId.TORQUE_SENSOR)

    assert realtime.snapshot()["sensor"].counter is None


def test_speed_parameters_are_read_from_the_broadcast(client):
    """32/03 arrives unasked, so the wheel setup needs no active read."""
    realtime = BroadcastRealtime(client)
    client.broadcast(0x32, 0x03, "c409c0019d08")

    speed = realtime.snapshot()["speed_parameters"]

    assert speed.speed_limit == 25.0
    assert speed.wheel.text == "28"
    assert speed.circumference == 2205
    assert speed.validate() == []


def test_the_battery_answer_to_the_display_is_read(client):
    """64/01 is not a broadcast: it is the battery answering the display.

    The display polls for it about ten times a second, so the answer is on
    the wire whether or not this tool asks -- which matters in listen-only mode,
    where it cannot ask.
    """
    realtime = BroadcastRealtime(client)
    message = BafangMessage(
        id=BafangId(
            source=int(DeviceId.BATTERY),
            target=int(DeviceId.DISPLAY),
            operation=int(CanOperation.NORMAL_ACK),
            code=0x64,
            subcode=0x01,
        ),
        data=bytes.fromhex("0b0062175a02"),
        timestamp=1.0,
    )
    for listener in client.listeners:
        listener(message)

    charging = realtime.snapshot()["battery_charging"]

    assert charging.charge_cycles == 11
    assert charging.max_uncharged_hours == 5986
    assert charging.to_dict()["last_uncharged"] == "25d 2h"


def test_the_monitor_line_shows_the_new_values():
    from bafang_can import codecs

    line = _format_realtime(
        {
            "battery_capacity": codecs.BatteryCapacity.decode(
                bytes.fromhex("682bdf17373164")
            ),
            "speed_parameters": codecs.SpeedParameters.decode(
                bytes.fromhex("c409c0019d08")
            ),
            "battery_charging": codecs.BatteryChargingInfo.decode(
                bytes.fromhex("0b0062175a02")
            ),
        }
    )

    assert "6111/11112 mAh" in line
    assert '28"' in line
    assert "11 charge cycles" in line


def test_an_absent_temperature_sensor_reads_as_unavailable():
    """0xFF means no sensor. This bike has no motor thermistor."""
    from bafang_can import codecs

    line = _format_realtime(
        {"controller1": codecs.ControllerRealtime1.decode(
            bytes.fromhex("000000001e143fff")
        )}
    )

    assert "motor n/a" in line
    assert "None" not in line


# ---------------------------------------------------------------------------
# the live fault code
#
# Established by capturing the same bike twice: while it displayed error 07
# with assist disabled it broadcast 07 here, and after the voltage-sense fault
# was repaired it broadcast 00. Both captures are in captures/.
# ---------------------------------------------------------------------------


def test_a_healthy_controller_reports_no_fault(client):
    realtime = BroadcastRealtime(client)
    client.broadcast(0x12, 0x00, "00")

    state = realtime.snapshot()["state"]

    assert state.ok
    assert state.error_code == 0
    assert state.description == "no fault"


def test_the_fault_this_bike_showed_is_named(client):
    """Code 7 was on the display for the whole overvoltage investigation.

    Its description was vendored in this repository throughout, and the tool
    reported "no description available" instead.
    """
    realtime = BroadcastRealtime(client)
    client.broadcast(0x12, 0x00, "07")

    state = realtime.snapshot()["state"]

    assert not state.ok
    assert state.description == "Over voltage protection"
    # The bike's own manual, which is where this recommendation now comes from.
    assert "Remove the battery" in state.recommendation


def test_the_fault_is_shown_in_the_monitor_line():
    from bafang_can import codecs

    line = _format_realtime({"state": codecs.ControllerState(error_code=7)})

    assert "error 7: Over voltage protection" in line


def test_a_healthy_controller_adds_no_noise_to_the_monitor_line():
    from bafang_can import codecs

    line = _format_realtime({"state": codecs.ControllerState(error_code=0)})

    assert line == "no data"


def test_the_two_real_captures_show_the_fault_appearing_and_clearing():
    """The whole basis for decoding 12/00: one bike, two states.

    Nothing upstream documents this message. These two files do.
    """
    from pathlib import Path

    from bafang_can.frame import CAN_EFF_MASK
    from bafang_can.quality import iter_frames

    def state_of(name):
        data_dir = Path(__file__).parent / "data"
        client = FakeClient()
        realtime = BroadcastRealtime(client)
        for can_id, payload, timestamp in iter_frames(data_dir / name):
            if can_id > CAN_EFF_MASK:
                continue
            message = BafangMessage(
                id=BafangId.decode(can_id), data=payload, timestamp=timestamp
            )
            for listener in client.listeners:
                listener(message)
        return realtime.snapshot()["state"]

    faulty = state_of("display-interaction-excerpt.log")
    repaired = state_of("bench-repaired-excerpt.log")

    assert faulty.error_code == 7
    assert faulty.description == "Over voltage protection"
    assert not faulty.ok
    assert repaired.ok


# ---------------------------------------------------------------------------
# the output shaft counter, calibrated against a drill of known speed
# ---------------------------------------------------------------------------


def test_the_shaft_counter_is_read_from_32_07(client):
    realtime = BroadcastRealtime(client)
    client.broadcast(0x32, 0x07, "00000000005a06")

    shaft = realtime.snapshot()["shaft"]

    assert shaft.counts == 1626
    assert shaft.revolutions == pytest.approx(25.4, abs=0.05)


def test_broadcasts_a_second_apart_give_a_true_shaft_speed(client):
    """64 counts per revolution, measured over 23 s of steady rotation.

    60 rpm is one turn a second, so the counter advances 64 in that second.
    """
    realtime = BroadcastRealtime(client)
    client.broadcast(0x32, 0x07, "00000000000000", timestamp=10.0)
    client.broadcast(0x32, 0x07, "00000000004000", timestamp=11.0)

    assert realtime.snapshot()["shaft_rpm"] == pytest.approx(60, abs=0.5)


def test_the_measured_bench_rotation_reads_back_as_its_cadence(client):
    """The calibration itself: 94.46 counts/s was a steady 88-89 rpm.

    This is the number the scale was derived from, so it is the one that must
    keep coming back out.
    """
    realtime = BroadcastRealtime(client)
    client.broadcast(0x32, 0x07, "00000000000000", timestamp=0.0)
    counts = round(94.46 * 23)
    client.broadcast(
        0x32, 0x07, bytes(5).hex() + f"{counts & 0xFF:02x}{counts >> 8:02x}",
        timestamp=23.0,
    )

    assert realtime.snapshot()["shaft_rpm"] == pytest.approx(88.6, abs=0.5)


def test_a_jittered_pair_of_frames_cannot_invent_a_huge_speed(client):
    """The adapter reorders a few frames a second; adjacent pairs are unsafe.

    Taken naively, a 35 ms gap turned a real 550 rpm into a reported 1621.
    """
    realtime = BroadcastRealtime(client)
    counts = 0
    time = 10.0
    for _ in range(12):  # a steady 550 rpm, one broadcast every 96 ms
        client.broadcast(0x32, 0x07, bytes(5).hex() + f"{counts & 0xFF:02x}{counts >> 8:02x}", timestamp=time)
        counts += 53
        time += 0.096
    # now one arriving far too soon after the last
    client.broadcast(0x32, 0x07, bytes(5).hex() + f"{counts & 0xFF:02x}{counts >> 8:02x}", timestamp=time - 0.06)

    assert realtime.snapshot()["shaft_rpm"] == pytest.approx(550, abs=60)


def test_no_speed_is_reported_until_the_baseline_is_long_enough(client):
    realtime = BroadcastRealtime(client)
    client.broadcast(0x32, 0x07, "00000000000000", timestamp=10.0)
    client.broadcast(0x32, 0x07, "00000000000500", timestamp=10.1)

    assert "shaft_rpm" not in realtime.snapshot()


def test_a_single_broadcast_reports_no_speed(client):
    """An accumulator alone is a distance, not a rate."""
    realtime = BroadcastRealtime(client)
    client.broadcast(0x32, 0x07, "00000000005a06")

    assert "shaft_rpm" not in realtime.snapshot()


def test_the_counter_wrapping_does_not_produce_a_negative_speed():
    """u16, so it wraps every 1092 revolutions -- 18 minutes of pedalling."""
    from bafang_can import codecs

    before = codecs.OutputShaftCounter(counts=0xFFC0)
    after = codecs.OutputShaftCounter(counts=0x0020)

    assert after.rpm_since(before, seconds=1.0) == pytest.approx(90, abs=1)


def test_the_shaft_speed_is_shown_in_the_monitor_line():
    assert "shaft   550 rpm" in _format_realtime({"shaft_rpm": 550.0})


# ---------------------------------------------------------------------------
# what the rider is asking for
#
# Captured on a DP C340.CAN by stepping from assist 5 down to walk one press
# at a time, then holding + to switch the lamp on. The bytes below are that
# sequence in order.
# ---------------------------------------------------------------------------


ASSIST_SEQUENCE = [
    ("55030001", "5"),
    ("55170001", "4"),
    ("55150001", "3"),
    ("550d0001", "2"),
    ("550b0001", "1"),
    ("55000001", "0"),
    ("55060001", "walk"),
]


@pytest.mark.parametrize(("payload", "expected"), ASSIST_SEQUENCE)
def test_each_assist_level_decodes(client, payload, expected):
    """The codes are a lookup table, not a scale: level 5 is 3, level 1 is 11."""
    realtime = BroadcastRealtime(client)
    client.broadcast(0x63, 0x00, payload, source=DeviceId.DISPLAY)

    assert realtime.snapshot()["display"].assist_level == expected


def test_the_display_reports_how_many_levels_it_has(client):
    """0x55 & 0x0F is 5, which is what this display is configured for."""
    realtime = BroadcastRealtime(client)
    client.broadcast(0x63, 0x00, "55030001", source=DeviceId.DISPLAY)

    assert realtime.snapshot()["display"].assist_levels == 5


def test_the_lamp_and_the_button_are_separate_bits(client):
    """Holding + to switch the lamp on walked byte 2 through 00, 02, 03, 01."""
    realtime = BroadcastRealtime(client)
    seen = []
    for payload in ("55000001", "55000201", "55000301", "55000101"):
        client.broadcast(0x63, 0x00, payload, source=DeviceId.DISPLAY)
        state = realtime.snapshot()["display"]
        seen.append((state.button_up, state.light))

    assert seen == [(False, False), (True, False), (True, True), (False, True)]


def test_an_unknown_assist_code_is_not_guessed_at(client):
    """The table is per-display and only three configurations are known."""
    realtime = BroadcastRealtime(client)
    client.broadcast(0x63, 0x00, "55ff0001", source=DeviceId.DISPLAY)

    assert realtime.snapshot()["display"].assist_level == "unknown (255)"


def test_an_unknown_level_count_does_not_invent_a_table(client):
    realtime = BroadcastRealtime(client)
    client.broadcast(0x63, 0x00, "57030001", source=DeviceId.DISPLAY)

    snapshot = realtime.snapshot()["display"]

    assert snapshot.assist_levels == 7
    assert snapshot.assist_level == "unknown (3)"


def test_the_assist_level_is_shown_in_the_monitor_line():
    from bafang_can import codecs

    line = _format_realtime(
        {"display": codecs.DisplayRealtime.decode(bytes.fromhex("55060101"))}
    )

    assert "assist walk/5" in line
    assert "light on" in line


def test_a_counter_restart_is_not_mistaken_for_a_wrap():
    """Observed on the bench: 10833 -> 158, and 1689 -> 0.

    Treated as a wrap, the first of those is 857 revolutions inside one 96 ms
    broadcast.
    """
    from bafang_can import codecs

    before = codecs.OutputShaftCounter(counts=10833)
    after = codecs.OutputShaftCounter(counts=158)

    assert after.rpm_since(before, seconds=0.096) is None


def test_a_genuine_wrap_from_the_top_of_the_range_still_counts():
    from bafang_can import codecs

    before = codecs.OutputShaftCounter(counts=0xFFC0)
    after = codecs.OutputShaftCounter(counts=0x0020)

    assert after.rpm_since(before, seconds=1.0) == pytest.approx(90, abs=1)


def test_a_restart_clears_the_reported_speed(client):
    """A stale rpm from before the restart is worse than none at all."""
    realtime = BroadcastRealtime(client)

    def counts(n):
        return bytes(5).hex() + f"{n & 0xFF:02x}{n >> 8:02x}"

    client.broadcast(0x32, 0x07, counts(0), timestamp=0.0)
    client.broadcast(0x32, 0x07, counts(64), timestamp=1.0)
    assert realtime.snapshot()["shaft_rpm"] == pytest.approx(60, abs=0.5)

    client.broadcast(0x32, 0x07, counts(3), timestamp=2.0)

    assert "shaft_rpm" not in realtime.snapshot()


def test_an_implausible_shaft_speed_is_discarded(client):
    """A stale frame at the head of a log leaves a 1357-count jump.

    Taken at face value that is 7977 rpm on a crank spindle.
    """
    realtime = BroadcastRealtime(client)

    def counts(n):
        return bytes(5).hex() + f"{n & 0xFF:02x}{n >> 8:02x}"

    client.broadcast(0x32, 0x07, counts(2478), timestamp=0.0)
    client.broadcast(0x32, 0x07, counts(3835), timestamp=0.5)

    assert "shaft_rpm" not in realtime.snapshot()


def test_a_drill_speed_is_still_credible(client):
    """518 rpm was really measured; the bound must not reject it."""
    realtime = BroadcastRealtime(client)

    def counts(n):
        return bytes(5).hex() + f"{n & 0xFF:02x}{n >> 8:02x}"

    client.broadcast(0x32, 0x07, counts(0), timestamp=0.0)
    client.broadcast(0x32, 0x07, counts(553), timestamp=1.0)

    assert realtime.snapshot()["shaft_rpm"] == pytest.approx(518, abs=2)


# ---------------------------------------------------------------------------
# the drive unit's power-on counter, 0x30/0x00
# ---------------------------------------------------------------------------


def test_the_uptime_counter_is_read_from_30_00(client):
    realtime = BroadcastRealtime(client)

    client.broadcast(0x30, 0x00, "35000000")

    uptime = realtime.snapshot()["uptime"]
    assert uptime.ticks == 53
    assert uptime.seconds == pytest.approx(531.4, abs=0.5)


def test_the_uptime_counter_is_little_endian(client):
    """0x0135 ticks, not 0x3501 -- byte order the wrong way round is 60x out."""
    realtime = BroadcastRealtime(client)

    client.broadcast(0x30, 0x00, "35010000")

    assert realtime.snapshot()["uptime"].ticks == 0x0135


def test_captures_from_one_power_cycle_agree_on_the_boot_instant():
    """Four real captures, taken across nine minutes of one power cycle.

    Each pairs the wall-clock timestamp of a 0x30/0x00 broadcast with the tick
    value it carried. Subtracting the uptime must land them all on the same
    power-on instant -- that agreement is the whole reason the field is worth
    decoding, and it is what a damaged time base breaks.

    The four really span 5.5 s, which is the counter's own resolution: it is
    only broadcast once per tick, so where inside a tick a capture starts is
    not observable. One tick is the tightest honest bound. The capture in the
    same set with a mangled time base misses by 20000 s.
    """
    from bafang_can import codecs

    observed = [
        (1787053351.577, 1),  # bench-slowspin
        (1787053600.278, 26),  # bench-slowspin2
        (1787053726.126, 38),  # bench-slowspin3
        (1787053876.181, 53),  # bench-cadence
    ]
    booted = [
        codecs.SystemUptime(ticks=ticks).booted_at(timestamp)
        for timestamp, ticks in observed
    ]

    assert max(booted) - min(booted) < codecs.UPTIME_TICK_SECONDS


def test_the_uptime_is_reported_on_its_own_line():
    from bafang_can import codecs

    line = _format_realtime({"uptime": codecs.SystemUptime(ticks=53)})

    assert "drive unit up 9 min this power cycle" in line
