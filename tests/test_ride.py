"""What a moving bike says that a bench cannot.

Every capture the rest of this suite is built on was recorded with the bike on
a stand or the wheel off the ground: no road load, no real battery under
current, and no speed worth the name. The three excerpts in ``tests/data``
named ``ride-*`` come from one 102 s road ride on 2026-08-20, recorded through
a candleLight 2.5 in passive mode with the pack connected.

They are here because three things in this repository could not be checked any
other way: that the drive unit really does keep assisting past the speed the
firmware used to cut at, that the battery codecs decode a pack that is
actually delivering current, and that the bus itself starts failing when the
motor pulls hard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bafang_can import codecs
from bafang_can.quality import analyse_log, iter_candump

DATA = Path(__file__).parent / "data"

BUS_ERRORS = DATA / "ride-bus-errors-excerpt.log"
ABOVE_CUTOFF = DATA / "ride-above-cutoff-excerpt.log"
COASTING = DATA / "ride-coasting-excerpt.log"

CONTROLLER_STATE = 0x02F83201  # 32/01, the drive unit's electrics
CONTROLLER_TRIP = 0x02F83200  # 32/00, its trip and torque figures
SENSOR = 0x01F83100  # 31/00, the torque sensor
BATTERY_STATE = 0x04F83401  # 34/01, current, volts, pack temperature
BATTERY_CAPACITY = 0x04F83400  # 34/00, mAh and state of charge
RIDER_POWER = 0x02F83208  # 32/08, the field identified below


def frames(path: Path) -> list[tuple[int, bytes, float]]:
    return list(iter_candump(path))


def decoded(path: Path, can_id: int, codec) -> list[tuple[float, object]]:
    return [
        (timestamp, codec.decode(data))
        for ident, data, timestamp in frames(path)
        if ident == can_id
    ]


def values(path: Path, can_id: int, codec):
    return [value for _, value in decoded(path, can_id, codec)]


class TestErrorFramesUnderLoad:
    """Eight error frames, and they are not scattered at random."""

    def test_the_burst_is_counted_as_bus_errors_not_as_corruption(self):
        """These used to be filed as corrupt identifiers from an slcan reader.

        This capture came off a gs_usb adapter, which has no serial framing to
        resynchronise, and the frames are error frames: the controller saying
        a frame on the wire was malformed.
        """
        quality = analyse_log(BUS_ERRORS)

        assert quality.bus_errors == 8
        assert quality.invalid_ids == 0
        assert not quality.healthy

    def test_the_controller_names_the_fault(self):
        quality = analyse_log(BUS_ERRORS)
        described = [error.describe() for error in quality.errors]

        assert any("frame format error" in text for text in described)
        assert any("bit stuffing error" in text for text in described)

    def test_a_candump_log_cannot_report_the_error_class(self):
        """python-can writes one identifier for every error frame.

        ``CanutilsLogWriter`` stamps ``20000080`` whatever the controller
        actually reported, so offline the class is genuinely unknown and is
        reported as unknown rather than as the bus error that stamp implies.
        """
        quality = analyse_log(BUS_ERRORS)

        assert all(error.classes is None for error in quality.errors)
        assert all(error.data for error in quality.errors)

    def test_they_arrive_together(self):
        """2.1 s apart at the widest, inside a 2.7 s excerpt."""
        quality = analyse_log(BUS_ERRORS)
        stamps = [error.timestamp for error in quality.errors]

        assert max(stamps) - min(stamps) < 2.2

    def test_the_motor_was_pulling_hard_at_the_time(self):
        """The reason to keep these frames rather than drop them.

        Nothing else in the capture points at the motor: the drive unit reports
        no fault, the sequence counters show no loss, and every node keeps
        broadcasting. The errors are the only evidence, and they land in the
        window where the pack is sagging under 15 A.
        """
        state = values(BUS_ERRORS, CONTROLLER_STATE, codecs.ControllerRealtime1)

        assert max(value.current for value in state) > 14
        assert min(value.voltage for value in state) < 34.5

    def test_the_warning_says_where_to_look(self):
        warnings = analyse_log(BUS_ERRORS).warnings()

        assert any("error frames" in text for text in warnings)
        assert any("motor current" in text for text in warnings)

    def test_a_capture_without_them_stays_clean(self):
        """The same ride, three seconds later, with the same adapter."""
        quality = analyse_log(COASTING)

        assert quality.bus_errors == 0
        assert quality.healthy


class TestBatteryAgainstTheController:
    """A real pack, delivering real current, checked against the drive unit.

    Every battery assertion in this suite before this file was made against a
    36 V pack sitting at rest, or against a bench supply with no pack at all.
    """

    def test_both_nodes_agree_on_the_state_of_charge(self):
        """52% from the battery's own gauge, 52% from the drive unit."""
        pack = values(ABOVE_CUTOFF, BATTERY_CAPACITY, codecs.BatteryCapacity)
        drive = values(ABOVE_CUTOFF, CONTROLLER_TRIP, codecs.ControllerRealtime0)

        assert {value.rsoc for value in pack} == {52}
        assert {value.remaining_capacity for value in drive} == {52}

    def test_the_repaired_voltage_sense_agrees_with_the_pack(self):
        """The regression test for the fault worked through in docs/m200.md.

        Before the repair the drive unit read 51.50 V against a 37.47 V pack, a
        factor of 1.374. Here the two never differ by more than 2%, which is
        the wiring drop between the pack terminals and the controller, not a
        gain error.
        """
        pack = values(ABOVE_CUTOFF, BATTERY_STATE, codecs.BatteryState)
        drive = values(ABOVE_CUTOFF, CONTROLLER_STATE, codecs.ControllerRealtime1)
        pack_volts = sum(v.voltage for v in pack) / len(pack)
        drive_volts = sum(v.voltage for v in drive) / len(drive)

        assert pack_volts == pytest.approx(drive_volts, rel=0.02)

    def test_the_pack_reports_discharge_as_a_negative_current(self):
        """Sign convention, and it only shows up with a load on the pack."""
        pack = values(BUS_ERRORS, BATTERY_STATE, codecs.BatteryState)

        assert max(value.current for value in pack) < 0
        assert min(value.current for value in pack) < -14

    def test_the_two_shunts_agree_on_how_hard_the_motor_is_pulling(self):
        """Different measurement points, within 2% at the peak."""
        pack = values(BUS_ERRORS, BATTERY_STATE, codecs.BatteryState)
        drive = values(BUS_ERRORS, CONTROLLER_STATE, codecs.ControllerRealtime1)

        assert abs(min(value.current for value in pack)) == pytest.approx(
            max(value.current for value in drive), rel=0.02
        )

    def test_the_pack_temperature_is_plausible_and_steady(self):
        pack = values(ABOVE_CUTOFF, BATTERY_STATE, codecs.BatteryState)

        assert {value.temperature for value in pack} == {22}


class TestAssistAboveTheLegalCutoff:
    """What writing 45 km/h to 32/03 actually changes.

    Stock firmware stops assisting at 25.0 km/h. The write was made on
    2026-08-20 and this excerpt is from the ride after it: the drive unit is
    still delivering double-digit current well past the point it used to cut.
    """

    def test_the_bike_is_above_the_stock_cutoff(self):
        state = values(ABOVE_CUTOFF, CONTROLLER_STATE, codecs.ControllerRealtime1)

        assert max(value.speed for value in state) > 26

    def test_the_motor_is_still_delivering_current_up_there(self):
        """Not one stray sample: a run of them, then the rider eases off.

        The excerpt ends with the current falling away to 2.7 A while the bike
        holds 26.5 km/h, which is what stopping pedalling looks like -- so the
        claim is about how many samples sustain current above the cutoff, not
        about the minimum.
        """
        state = values(ABOVE_CUTOFF, CONTROLLER_STATE, codecs.ControllerRealtime1)
        fast = [value for value in state if value.speed > 25.0]
        pulling = [value for value in fast if value.current > 10]

        assert fast, "the excerpt is supposed to cover the fast part of the ride"
        assert len(pulling) >= 5
        assert max(value.speed for value in pulling) > 26

    def test_the_speed_limit_on_the_wire_is_the_one_that_was_written(self):
        """32/03 keeps broadcasting it, so the capture carries its own proof."""
        speeds = values(ABOVE_CUTOFF, 0x02F83203, codecs.SpeedParameters)

        assert speeds, "32/03 broadcasts every 2 s and one should be in here"
        assert all(value.speed_limit == 45.0 for value in speeds)
        assert all(value.circumference == 2205 for value in speeds)


class TestTheUnnamedPowerField:
    """0x32/0x08 -- in no published table, and this ride narrows it down.

    It is two bytes, little-endian, and reads like watts: it peaks at 581 in
    the ride this excerpt comes from. The question is whose watts, and the
    coast-down answers it.
    """

    def test_it_is_not_motor_power(self):
        """Zero motor current, non-zero field. That rules the motor out.

        Across the coasting excerpt the drive unit reports 0.0 A for every
        sample, and the field is still 42 and then 34 while the rider is
        turning the cranks.
        """
        state = values(COASTING, CONTROLLER_STATE, codecs.ControllerRealtime1)
        field = [
            int.from_bytes(data, "little")
            for ident, data, _ in frames(COASTING)
            if ident == RIDER_POWER
        ]

        assert {value.current for value in state} == {0.0}
        assert max(field) > 0

    def test_it_falls_to_zero_when_the_rider_stops_pedalling(self):
        """And stays there while the bike rolls on at 25 km/h.

        Speed alone does not drive it, which is the other half of the argument:
        the bike is still moving fast through every one of these samples.
        """
        samples: list[tuple[int, int, float]] = []
        cadence = 0
        speed = 0.0
        for ident, data, _ in frames(COASTING):
            if ident == SENSOR:
                cadence = codecs.SensorRealtime.decode(data).cadence
            elif ident == CONTROLLER_STATE:
                speed = codecs.ControllerRealtime1.decode(data).speed
            elif ident == RIDER_POWER:
                samples.append((int.from_bytes(data, "little"), cadence, speed))

        coasting = [value for value, cadence, _ in samples if cadence == 0]
        assert coasting, "the rider stops pedalling in this excerpt"
        assert set(coasting) == {0}
        assert min(speed for _, cadence, speed in samples if cadence == 0) > 15

    def test_the_scale_is_not_claimed(self):
        """Torque times cadence does not fit, and the fit is not forced.

        Fitting the field against ``(torque - rest) * cadence`` over the whole
        ride gives a coefficient spanning an order of magnitude between the
        10th and 90th percentile, because the torque broadcast is instantaneous
        within a pedal stroke and this field is not. Until a capture separates
        them, the units stay unclaimed: this test exists to record that the
        codec deliberately does not decode this field.
        """
        assert not hasattr(codecs, "RiderPower")


class TestReplayingTheRide:
    """A recorded ride, put back on a simulated bus.

    The simulator's own numbers are invented -- that is stated at the top of
    ``simulator.py`` -- so anything read from it proves wiring and nothing
    about a bike. Replaying a capture closes that gap for the passive path:
    the frames are bytes a real G210 sent, arriving at the pace it sent them.
    """

    def bus(self, path: Path):
        from bafang_can.simulator import SimBus

        return SimBus(chatter=False, replay=str(path))

    def test_only_the_bikes_own_traffic_is_replayed(self):
        """An answer addressed to the tool would satisfy a request nobody made."""
        from bafang_can.constants import DeviceId
        from bafang_can.frame import BafangId

        loaded = self.bus(ABOVE_CUTOFF).replay_frames

        assert loaded
        assert all(
            BafangId.decode(can_id).target != int(DeviceId.TOOL)
            for can_id, _, _ in loaded
        )

    def test_error_frames_are_not_replayed(self):
        """Reproducing an old ride's bus errors would be inventing a fault."""
        from bafang_can.quality import CAN_ERROR_STAMP

        loaded = self.bus(BUS_ERRORS).replay_frames

        assert analyse_log(BUS_ERRORS).bus_errors == 8
        assert all(can_id != CAN_ERROR_STAMP for can_id, _, _ in loaded)

    def test_the_replayed_frames_are_the_recorded_ones(self):
        """Byte for byte, in order, including the 45 km/h speed broadcast."""
        from bafang_can.constants import DeviceId
        from bafang_can.frame import BafangId

        recorded = [
            (can_id, data)
            for can_id, data, _ in frames(ABOVE_CUTOFF)
            if BafangId.decode(can_id).target != int(DeviceId.TOOL)
        ]
        loaded = [
            (can_id, data) for can_id, data, _ in self.bus(ABOVE_CUTOFF).replay_frames
        ]

        assert loaded == recorded
        assert any(can_id == 0x02F83203 for can_id, _ in loaded)

    def test_a_long_pause_is_shortened(self):
        """A capture can contain a red light. A replay that hangs is useless."""
        from bafang_can.simulator import SimBus

        assert SimBus.MAX_REPLAY_GAP <= 1.0

    def test_the_cli_can_monitor_a_recorded_ride(self, capsys):
        """End to end, with no bike and no adapter."""
        from bafang_can.cli import main

        code = main(
            [
                "--interface",
                "sim",
                "--sim-replay",
                str(ABOVE_CUTOFF),
                "monitor",
                "--passive",
                "--seconds",
                "2",
            ]
        )
        out = capsys.readouterr().out

        assert code == 0
        assert "km/h" in out
        # The simulator invents a 14000 mAh pack. This one is the real BT360
        # reporting 11112 mAh, which is proof the numbers came off the log and
        # not out of simulator.py. 32/03 is not asserted here: it broadcasts
        # every 2 s and a short window need not contain one.
        assert "11112 mAh" in out
