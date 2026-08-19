"""Detecting a damaged capture without a reference to compare it against.

Every number asserted here was measured on real captures first. The fixture in
``tests/data`` is an excerpt of one of them, kept because the tool that made it
reported nothing wrong at the time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bafang_can.frame import CAN_EFF_MASK
from bafang_can.quality import (
    LinkQuality,
    analyse_log,
    iter_candump,
    shortest_frame_time,
)

FIXTURE = Path(__file__).parent / "data" / "display-interaction-excerpt.log"

#: A torque sensor broadcast: source 0x01, target broadcast, WRITE_CMD 31/00.
SENSOR_ID = 0x01F83100


def sensor(counter: int) -> bytes:
    """A 31/00 payload with the rolling counter set."""
    return bytes([0xF1, 0x04, 0x00, counter])


class TestLostFrames:
    """The sensor numbers its broadcasts, so loss is provable, not inferred."""

    def test_a_clean_run_reports_no_loss(self):
        quality = LinkQuality()
        for counter in range(10):
            quality.observe(SENSOR_ID, sensor(counter), counter * 0.0115)
        assert quality.lost == 0
        assert quality.loss_ratio == 0.0
        assert quality.healthy

    def test_a_skipped_counter_is_a_lost_frame(self):
        quality = LinkQuality()
        quality.observe(SENSOR_ID, sensor(0x0E), 1.0)
        quality.observe(SENSOR_ID, sensor(0x10), 1.023)
        assert quality.lost == 1
        assert quality.gaps[0].previous == 0x0E
        assert quality.gaps[0].current == 0x10
        assert quality.gaps[0].missing == 1

    def test_the_counter_wraps_at_ff_without_inventing_a_loss(self):
        quality = LinkQuality()
        quality.observe(SENSOR_ID, sensor(0xFF), 1.0)
        quality.observe(SENSOR_ID, sensor(0x00), 1.0115)
        assert quality.lost == 0

    def test_a_wrapped_gap_still_counts_what_it_lost(self):
        quality = LinkQuality()
        quality.observe(SENSOR_ID, sensor(0xFB), 1.0)
        quality.observe(SENSOR_ID, sensor(0x04), 1.117)
        assert quality.lost == 8

    def test_a_repeated_counter_is_a_duplicate_not_a_loss(self):
        quality = LinkQuality()
        quality.observe(SENSOR_ID, sensor(7), 1.0)
        quality.observe(SENSOR_ID, sensor(7), 1.0115)
        assert (quality.duplicates, quality.lost) == (1, 0)

    def test_loss_is_unknown_rather_than_zero_without_a_counter(self):
        """A capture with nothing sequenced in it cannot be checked this way.

        Reporting 0% would claim it had been checked and found clean.
        """
        quality = LinkQuality()
        quality.observe(0x02F83201, bytes(8), 1.0)
        assert quality.loss_ratio is None

    def test_gaps_are_listed_but_not_without_limit(self):
        quality = LinkQuality()
        for step in range(0, 200, 3):  # every observation skips two counters
            quality.observe(SENSOR_ID, sensor(step % 256), step * 0.03)
        assert quality.lost > quality.MAX_LISTED_GAPS
        assert len(quality.gaps) == quality.MAX_LISTED_GAPS


class TestCorruptIdentifiers:
    """29 bits is all CAN has; anything wider did not come off a bus."""

    def test_an_oversized_identifier_is_rejected(self):
        quality = LinkQuality()
        assert quality.observe(0x2F830100, b"\x04\xd4\x0f", 1.0) is False
        assert quality.invalid_ids == 1

    def test_a_valid_identifier_is_accepted(self):
        quality = LinkQuality()
        assert quality.observe(CAN_EFF_MASK, b"", 1.0) is True
        assert quality.invalid_ids == 0

    def test_corruption_inside_29_bits_is_flagged_not_dropped(self):
        """0x10004A41 is corrupt but decodes to a device that could exist.

        No range check can catch it, so it is reported and kept rather than
        silently discarded on a guess.
        """
        quality = LinkQuality()
        assert quality.observe(0x10004A41, b"", 1.0) is True
        assert quality.implausible_devices == 1
        assert quality.invalid_ids == 0


class TestImpossibleTiming:
    """A frame cannot arrive sooner than the shortest frame takes to clock out."""

    def test_the_floor_matches_the_bus(self):
        assert shortest_frame_time(250_000) == pytest.approx(268e-6, rel=1e-3)

    def test_frames_closer_than_the_floor_are_counted(self):
        quality = LinkQuality()
        quality.observe(0x02F83201, bytes(8), 1.000000)
        quality.observe(0x02F83201, bytes(8), 1.000056)  # 56 us
        assert quality.too_fast == 1

    def test_realistic_spacing_is_not_flagged(self):
        quality = LinkQuality()
        quality.observe(0x02F83201, bytes(8), 1.0)
        quality.observe(0x02F83201, bytes(8), 1.0115)
        assert quality.too_fast == 0


class TestAgainstTheRecordedBike:
    """The fixture is a real capture with known, measured damage."""

    def test_every_defect_is_found(self):
        quality = analyse_log(FIXTURE)
        assert quality.frames == 99
        assert quality.invalid_ids == 2
        assert quality.lost == 12
        assert quality.too_fast == 24
        assert quality.loss_ratio == pytest.approx(0.197, abs=0.001)
        assert not quality.healthy

    def test_the_problems_are_explained_in_words(self):
        warnings = analyse_log(FIXTURE).warnings()
        assert len(warnings) == 3
        assert any("never arrived" in w for w in warnings)
        assert any("29 bits" in w for w in warnings)
        assert any("shortest frame" in w for w in warnings)

    def test_the_raw_reader_keeps_the_corruption_python_can_masks(self):
        """python-can masks the identifier to 29 bits before we ever see it.

        That is the whole reason candump logs are parsed here instead.
        """
        oversized = [
            can_id for can_id, _, _ in iter_candump(FIXTURE) if can_id > CAN_EFF_MASK
        ]
        assert oversized == [0x2F830100, 0x28308000]

    def test_python_can_can_still_read_the_fixture(self):
        """The fixture must stay valid candump, comments and all."""
        can = pytest.importorskip("can")
        with can.LogReader(str(FIXTURE)) as reader:
            assert sum(1 for _ in reader) == 99


class TestAgainstAHealthyCapture:
    """A detector only tested on broken input is not known to accept good input."""

    HEALTHY = Path(__file__).parent / "data" / "bench-repaired-excerpt.log"

    def test_a_clean_capture_passes_every_check(self):
        quality = analyse_log(self.HEALTHY)

        assert quality.frames == 120
        assert quality.invalid_ids == 0
        assert quality.lost == 0
        assert quality.too_fast == 0
        assert quality.implausible_devices == 0
        assert quality.healthy

    def test_a_clean_capture_produces_no_warnings(self):
        assert analyse_log(self.HEALTHY).warnings() == []

    def test_loss_is_measured_not_assumed(self):
        """It is 0% because 62 sequenced frames were checked, not by default."""
        quality = analyse_log(self.HEALTHY)

        assert quality.counted == 62
        assert quality.loss_ratio == 0.0
