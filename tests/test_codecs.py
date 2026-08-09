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
