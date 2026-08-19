import random

import pytest

from bafang_can import codecs
from bafang_can.constants import wheel_by_text
from bafang_can.frame import checksum


def block_with(values: dict[int, int]) -> bytes:
    body = bytearray(64)
    for offset, value in values.items():
        body[offset] = value
    body[63] = checksum(body[:63])
    return bytes(body)


def test_parameter1_roundtrip_preserves_unknown_bytes():
    raw = bytearray(range(64))
    raw[63] = checksum(raw[:63])
    block = codecs.Parameter1.decode(bytes(raw))
    assert block.checksum_ok
    encoded = block.encode()
    # Bytes the codec does not claim to understand must survive untouched.
    for offset in (17, 22, 23, 30, 31, 55, 56, 57, 62):
        assert encoded[offset] == raw[offset]
    assert encoded[63] == checksum(encoded[:63])


def test_parameter1_fields():
    raw = block_with(
        {
            0: 36,
            1: 15,
            2: 43,
            3: 0x2C, 4: 0x01,  # 300 -> 30.0 V under load
            5: 0x40, 6: 0x01,  # 320 -> 32.0 V
            7: 0x10, 8: 0x27,  # 10000 mAh
            19: 8,
            40: 20, 49: 30,
        }
    )
    block = codecs.Parameter1.decode(raw)
    assert block.system_voltage == 36
    assert block.current_limit == 15
    assert block.undervoltage_under_load == 300
    assert block.undervoltage == 320
    assert block.battery_capacity == 10000
    assert block.motor_pole_pair_number == 8
    assert block.assist_levels[0].current_limit == 20
    assert block.assist_levels[0].speed_limit == 30
    assert len(block.assist_levels) == 9


def test_parameter1_edit_changes_only_that_byte():
    raw = block_with({1: 15, 40: 20, 49: 30})
    block = codecs.Parameter1.decode(raw)
    block.current_limit = 12
    encoded = block.encode()
    assert encoded[1] == 12
    assert encoded[:1] == raw[:1]
    assert encoded[2:63] == raw[2:63]
    assert encoded[63] == checksum(encoded[:63])


def test_parameter2_scaled_fields_roundtrip():
    raw = block_with({42: 4, 48: 3, 54: 5})
    block = codecs.Parameter2.decode(raw)
    assert block.torque_profiles[0].current_decay_time == 20
    assert block.torque_profiles[0].stop_delay == 6
    assert block.acceleration_level == 5
    assert block.encode()[42] == 4
    assert block.encode()[48] == 3


def test_parameter0_ratio_levels():
    raw = bytearray(64)
    raw[1:10] = bytes(range(1, 10))
    raw[10] = 0x2C
    raw[11] = 0x01  # 300 %
    raw[28] = 0xF4
    raw[29] = 0x01  # 500 %
    raw[63] = checksum(raw[:63])
    block = codecs.Parameter0.decode(bytes(raw))
    assert block.acceleration_levels == list(range(1, 10))
    assert block.assist_ratio_levels[0] == 300
    assert block.assist_ratio_upper_limit == 500
    assert block.encode() == bytes(raw)


def test_block_without_source_refuses_to_encode():
    block = codecs.Parameter1(assist_levels=[codecs.AssistLevel(0, 0)] * 9)
    with pytest.raises(codecs.DecodeError):
        block.encode()


def test_speed_parameters_roundtrip_and_validation():
    wheel = wheel_by_text("27.5")
    params = codecs.SpeedParameters(
        speed_limit=25.0, wheel_code=wheel.code, circumference=2215
    )
    encoded = params.encode()
    again = codecs.SpeedParameters.decode(encoded)
    assert again.speed_limit == 25.0
    assert again.wheel.text == "27.5"
    assert again.circumference == 2215
    assert params.validate() == []

    params.circumference = 900
    problems = params.validate()
    assert problems and "circumference" in problems[0]


def test_realtime_decoders():
    rt1 = codecs.ControllerRealtime1.decode(
        bytes([0xE8, 0x03, 0x64, 0x00, 0x10, 0x0E, 0x3C, 0xFF])
    )
    assert rt1.speed == 10.0
    assert rt1.current == 1.0
    assert rt1.voltage == 36.0
    assert rt1.temperature == 20
    assert rt1.motor_temperature is None

    rt0 = codecs.ControllerRealtime0.decode(
        bytes([80, 0x10, 0x00, 60, 0x20, 0x00, 0xFF, 0xFF])
    )
    assert rt0.remaining_capacity == 80
    assert rt0.cadence == 60
    assert rt0.remaining_distance is None


def test_battery_state_handles_negative_current():
    state = codecs.BatteryState.decode(bytes([0x9C, 0xFF, 0x10, 0x0E, 60]))
    assert state.current == -1.0
    assert state.voltage == 36.0
    assert state.temperature == 20


def test_error_codes_are_ascii_pairs():
    assert codecs.decode_error_codes(b"0821\x00") == [8, 21]
    assert codecs.decode_error_codes(b"\x00") == []


@pytest.mark.parametrize("cls", [codecs.Parameter0, codecs.Parameter1, codecs.Parameter2])
def test_decode_encode_is_the_identity_on_random_blocks(cls):
    """A block read from a device and written straight back must not change.

    This is the invariant that protects the undecoded bytes: if any field
    scales badly on the way out (or an offset is wrong in one direction only),
    a random block will not survive the round trip.
    """
    rng = random.Random(4242)
    for _ in range(200):
        body = bytes(rng.randrange(256) for _ in range(63))
        raw = body + bytes([checksum(body)])
        block = cls.decode(raw)
        assert block.checksum_ok
        assert block.encode() == raw, cls.__name__


def test_speed_parameters_round_trip_on_random_payloads():
    rng = random.Random(99)
    for _ in range(200):
        raw = bytes(rng.randrange(256) for _ in range(6))
        assert codecs.SpeedParameters.decode(raw).encode() == raw


# ---------------------------------------------------------------------------
# error codes, merged from two upstream projects that do not fully agree
# ---------------------------------------------------------------------------


def test_the_vendored_descriptions_are_all_exposed():
    """24 codes used to be reported as having no description available.

    Every one of them had a description vendored in this repository.
    """
    from bafang_can.constants import ERROR_DESCRIPTIONS

    assert len(ERROR_DESCRIPTIONS) == 33


def test_code_7_is_named():
    """The fault the bike in docs/m200.md displayed for the whole case."""
    from bafang_can.constants import error_text

    description, recommendation = error_text(7)

    assert description == "Over voltage protection"
    assert recommendation != "-"


def test_openbafang_detail_wins_where_the_two_agree():
    """Both call 8 a hall sensor fault; OpenBafangTool says which one."""
    from bafang_can.constants import error_text

    assert error_text(8)[0] == "Inner motor hall sensor error (not the speed hall sensor)"


def test_the_vehicle_manual_settles_the_disputed_code():
    """Code 14 meant opposite things in the two upstream projects.

    OpenBafangTool called it a motor communication error, bafang_canable_pro
    controller overtemperature -- opposite repairs, so both were reported and
    neither chosen. The bike's own manual names it as the controller box
    reaching its temperature limit, which is a source neither upstream project
    had and which is definitive for this vehicle.
    """
    from bafang_can.constants import CONFLICTING_ERRORS, error_text

    assert 14 not in CONFLICTING_ERRORS
    description, recommendation = error_text(14)

    assert "Controller box temperature" in description
    assert "disagree" not in description
    assert "cool" in recommendation


def test_a_disputed_code_would_still_report_both_readings():
    """Nothing is disputed today; the machinery for the next one must survive.

    Removing it along with the last conflict would mean the next disagreement
    silently picks a winner.
    """
    import bafang_can.constants as constants

    original = constants.CONFLICTING_ERRORS
    constants.CONFLICTING_ERRORS = frozenset({14})
    try:
        description, recommendation = constants.error_text(14)
    finally:
        constants.CONFLICTING_ERRORS = original

    assert "disagree" in description
    assert "If that is not it" in recommendation


def test_the_manual_beats_the_upstream_tables_where_it_is_more_specific():
    """Both upstream projects call code 21 a generic hall sensor error.

    The manual names the speed sensor and gives the magnet gap, which is the
    difference between a description and something you can act on.
    """
    from bafang_can.constants import error_text

    description, recommendation = error_text(21)

    assert "Speed sensor" in description
    assert "10-20 mm" in recommendation


def test_an_unknown_code_is_not_given_an_invented_meaning():
    from bafang_can.constants import error_text

    assert error_text(200) == ("Code 200: unknown code", "-")


def test_the_backlight_level_is_read_from_63_04():
    """bafang_canable_pro's package3; OpenBafangTool does not know 63/04.

    A DP C340.C exposes this one in its own menu as `bL`, 1 to 5, which makes
    it the only field on this bus a user can drive to a known value on demand.
    """
    value = codecs.DisplayLightLevels.decode(bytes([5, 3, 5, 4]))

    assert value.backlight_levels == 5
    assert value.backlight_level == 4
    assert value.light_sensor_level == 3


def test_auto_shutdown_reports_never_rather_than_255():
    assert codecs.DisplayAutoShutdown.decode(b"\x05").to_dict() == {"minutes": 5}
    assert codecs.DisplayAutoShutdown.decode(b"\xff").to_dict() == {"minutes": "never"}


def test_the_two_places_a_riding_style_could_live_are_both_carried():
    """The manual documents a dealer-only three-state körstil: D, S, C.

    Both vendored parsers read byte 0 bit 4 as a *two*-state ride mode, and
    neither reads byte 3 at all. A three-state setting at its middle value
    reads 1 in either place, which is what a real DP C340.CAN shows -- so both
    must survive decoding until a capture across a change separates them.
    """
    value = codecs.DisplayRealtime.decode(bytes.fromhex("55030001"))

    assert value.ride_mode == 1
    assert value.mode_byte == 1
    assert value.assist_levels == 5
    assert value.assist_level == "5"


def test_a_third_riding_style_state_would_not_be_flattened_to_a_bool():
    """Reported as an integer so a value neither vendor expects stays visible."""
    assert codecs.DisplayRealtime.decode(bytes.fromhex("45030002")).ride_mode == 0
    assert codecs.DisplayRealtime.decode(bytes.fromhex("55030002")).mode_byte == 2


def test_a_three_byte_display_frame_still_decodes():
    """mode_byte is absent rather than invented when the frame is short."""
    value = codecs.DisplayRealtime.decode(bytes.fromhex("550300"))

    assert value.mode_byte is None
    assert value.ride_mode == 1


def test_byte_0_is_carried_raw_because_its_flags_do_not_explain_it():
    """ride_mode and boost are two bits sliced out of a nibble worth 5.

    A published M500 capture shows a DPC080 sending byte 0 = 0x05, where this
    DP C340.C sends 0x55 in every frame after boot. Both parsers agree on the
    low nibble (five assist levels) and then read bits 4 and 5 of the high one.
    But 0x5 in the high nibble also sets bit 6, which no source reads, and it
    makes the high nibble equal the low one -- so "ride_mode = 1, boost off"
    may be an artifact of slicing a nibble that is not a set of flags. Keeping
    byte 0 raw is what lets that be seen rather than asserted away.
    """
    value = codecs.DisplayRealtime.decode(bytes.fromhex("55030001"))

    assert value.byte0 == 0x55
    accounted = 0b1111 | 0b110000  # levels, ride_mode, boost
    assert value.byte0 & ~accounted, "bit 6 is set and nothing reads it"


def test_the_boot_frame_is_the_one_that_breaks_the_byte_3_pattern():
    """3567 of 3568 captured frames hold 0x01 in byte 3. This is the other one.

    It is the display's first frame after power-on, and byte 0 is
    uninitialised in it too -- 0x05 rather than the 0x55 of every later frame.
    That matters for the riding-style question: the single exception to a
    constant byte is a boot artifact, not the setting moving.
    """
    boot = codecs.DisplayRealtime.decode(bytes.fromhex("050B0000"))
    steady = codecs.DisplayRealtime.decode(bytes.fromhex("550B0001"))

    assert (boot.byte0, boot.mode_byte) == (0x05, 0)
    assert (steady.byte0, steady.mode_byte) == (0x55, 1)
    assert boot.assist_levels == steady.assist_levels == 5
