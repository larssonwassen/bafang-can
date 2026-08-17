"""Falling back when 87.5% is arithmetically out of reach.

python-can's gs_usb backend asks for an 87.5% sample point and gives up if no
bit timing hits it. Whether one exists depends on the adapter's CAN clock, so
an ordinary bitrate can be unreachable. Measured on a candleLight 2.5 board
with a 160 MHz clock: 250 kbit/s needs 640/brp time quanta, and python-can's
classic-CAN rules (prescaler <= 32, bit time <= 25 quanta, tseg1 <= 16) leave
only 20 quanta, where 87.5% needs a tseg1 of 17. So `--interface gs_usb
--bitrate 250000` could not open at all on that hardware.
"""

from __future__ import annotations

import can
import pytest

from bafang_can.transport import SAMPLE_POINTS, _relaxed_sample_point

# The real numbers from the adapter this was found on.
F_CLOCK = 160_000_000
BITRATE = 250_000


def test_the_real_case_has_no_solution_at_87_5_percent():
    """Without the fallback this is the failure users hit."""
    with pytest.raises(ValueError, match="No suitable bit timings"):
        can.BitTiming.from_sample_point(
            f_clock=F_CLOCK, bitrate=BITRATE, sample_point=87.5
        )


def test_the_fallback_finds_a_timing_that_hits_the_bitrate_exactly():
    with _relaxed_sample_point("gs_usb"):
        timing = can.BitTiming.from_sample_point(
            f_clock=F_CLOCK, bitrate=BITRATE, sample_point=87.5
        )

    assert timing.bitrate == BITRATE  # exactly, not approximately
    assert timing.sample_point == 85.0
    assert (timing.brp, timing.tseg1, timing.tseg2) == (32, 16, 3)


def test_87_5_still_wins_when_it_is_achievable():
    """Widen the search, do not change the answer."""
    plain = can.BitTiming.from_sample_point(
        f_clock=80_000_000, bitrate=500_000, sample_point=87.5
    )
    with _relaxed_sample_point("gs_usb"):
        relaxed = can.BitTiming.from_sample_point(
            f_clock=80_000_000, bitrate=500_000, sample_point=87.5
        )

    assert relaxed.sample_point == plain.sample_point == 87.5
    assert (relaxed.brp, relaxed.tseg1, relaxed.tseg2) == (
        plain.brp,
        plain.tseg1,
        plain.tseg2,
    )


def test_the_patch_is_removed_afterwards():
    original = can.BitTiming.from_sample_point
    with _relaxed_sample_point("gs_usb"):
        assert can.BitTiming.from_sample_point is not original
    assert can.BitTiming.from_sample_point is original


def test_the_patch_is_removed_even_when_opening_raises():
    original = can.BitTiming.from_sample_point
    with pytest.raises(RuntimeError), _relaxed_sample_point("gs_usb"):
        raise RuntimeError("bus open failed")
    assert can.BitTiming.from_sample_point is original


def test_other_interfaces_are_left_alone():
    """slcan and socketcan do not use this code path; do not touch them."""
    original = can.BitTiming.from_sample_point
    with _relaxed_sample_point("slcan"):
        assert can.BitTiming.from_sample_point is original


def test_an_unreachable_bitrate_says_so_instead_of_no_suitable_bit_timings():
    with _relaxed_sample_point("gs_usb"):
        with pytest.raises(ValueError, match="at any of the sample points"):
            # 8 bit/s is not reachable on any clock this code will meet.
            can.BitTiming.from_sample_point(
                f_clock=F_CLOCK, bitrate=8, sample_point=87.5
            )


def test_87_5_is_tried_first():
    assert SAMPLE_POINTS[0] == 87.5
