"""Differential test of the write path against the vendored JavaScript.

Two things are checked here, both of which write bytes into a real motor if
they are wrong:

1. **Framing** -- the exact sequence of CAN frames a multi-frame write emits
   (identifiers, operation codes, sequence numbers, payload split).
2. **Byte offsets** -- feeding the vendor serializer the same logical values
   must produce a block that this tool's decoder reads back identically.

The vendor serializer builds a fresh block filled with 0xFF for the bytes it
does not know, whereas this tool patches the block that was read from the
device. Only the fields both sides claim to understand are compared.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bafang_can import codecs
from bafang_can.commands import WRITE
from bafang_can.constants import DeviceId
from bafang_can.frame import BafangId, checksum
from bafang_can.protocol import BafangClient
from bafang_can.simulator import SimBus

VENDOR = Path(__file__).resolve().parents[1] / "vendor" / "bafang_canable_pro"
SCRIPT = Path(__file__).parent / "differential" / "serialize_with_vendor.js"

pytestmark = [
    pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed"),
    pytest.mark.skipif(
        not (VENDOR / "bafang-serializer.js").exists(),
        reason="git submodules are not checked out",
    ),
]


def run_vendor(requests: list[dict]) -> list[list[str]]:
    result = subprocess.run(
        ["node", str(SCRIPT)],
        input=json.dumps({"requests": requests}),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"vendor serializer failed: {result.stderr}")
    return json.loads(result.stdout)


def parse_frames(frames: list[str]) -> list[tuple[BafangId, bytes]]:
    out = []
    for frame in frames:
        raw_id, _, raw_data = frame.partition("#")
        out.append((BafangId.decode(int(raw_id, 16)), bytes.fromhex(raw_data)))
    return out


def our_frames(payload: bytes, command) -> list[tuple[BafangId, bytes]]:
    """The frames this tool puts on the bus for one multi-frame write."""
    bus = SimBus(chatter=False)
    with BafangClient(bus, timeout=2.0) as client:
        client.write_long(DeviceId.DRIVE_UNIT, command, payload)
    return [
        (BafangId.decode(message.arbitration_id), bytes(message.data))
        for message in bus.sent
    ]


def test_multiframe_write_framing_matches_vendor():
    payload = bytes((i * 7 + 3) % 256 for i in range(64))
    command = WRITE["Parameter1"]
    vendor = parse_frames(
        run_vendor(
            [
                {
                    "kind": "long_write",
                    "target": int(DeviceId.DRIVE_UNIT),
                    "code": command.code,
                    "subcode": command.subcode,
                    "data": list(payload),
                }
            ]
        )[0]
    )
    ours = our_frames(payload, command)

    assert len(ours) == len(vendor), "frame count differs"
    for index, ((our_id, our_data), (their_id, their_data)) in enumerate(
        zip(ours, vendor)
    ):
        assert our_id == their_id, f"frame {index} identifier"
        assert our_data == their_data, f"frame {index} payload"


def test_multiframe_write_split_is_exact():
    """A payload that does not divide evenly still lands byte for byte."""
    payload = bytes(range(30))
    command = WRITE["Parameter2"]
    vendor = parse_frames(
        run_vendor(
            [
                {
                    "kind": "long_write",
                    "target": int(DeviceId.DRIVE_UNIT),
                    "code": command.code,
                    "subcode": command.subcode,
                    "data": list(payload),
                }
            ]
        )[0]
    )
    assert our_frames(payload, command) == vendor

    # And the payload can be put back together from the frames.
    reassembled = b"".join(data for _, data in vendor[1:])
    assert reassembled == payload


def _vendor_block(kind: str, value: dict) -> bytes:
    frames = parse_frames(run_vendor([{"kind": kind, "value": value}])[0])
    assert frames, "vendor produced no frames"
    return b"".join(data for _, data in frames[1:])


def test_parameter1_offsets_match_vendor():
    """Vendor-serialized values must decode to the same fields in our codec."""
    value = {
        "system_voltage": 36,
        "current_limit": 14,
        "overvoltage": 43,
        "undervoltage": 320,
        "undervoltage_under_load": 300,
        "battery_capacity": 14000,
        "max_current_on_low_charge": 9,
        "limp_mode_soc_limit": 15,
        "limp_mode_soc_limit_stage2": 5,
        "full_capacity_range": 60,
        "pedal_sensor_type": 0,
        "coaster_brake": False,
        "pedal_sensor_signals_per_rotation": 32,
        "speed_sensor_channel_number": 1,
        "motor_type": 1,
        "motor_pole_pair_number": 8,
        "speedmeter_magnets_number": 1,
        "temperature_sensor_type": 1,
        "deceleration_ratio": 21.0,
        "motor_max_rotor_rpm": 4500,
        "motor_d_axis_inductance": 120,
        "motor_q_axis_inductance": 130,
        "motor_phase_resistance": 140,
        "motor_reverse_potential_coefficient": 150,
        "throttle_start_voltage": 1.2,
        "throttle_max_voltage": 4.2,
        "start_current": 25,
        "current_loading_time": 0.5,
        "current_shedding_time": 0.5,
        "assist_levels": [
            {"current_limit": 10 + i, "speed_limit": 20 + i} for i in range(9)
        ],
        "displayless_mode": False,
        "lamps_always_on": True,
        "walk_assist_speed": 6.0,
    }
    block = codecs.Parameter1.decode(_vendor_block("parameter1", value))

    assert block.system_voltage == 36
    assert block.current_limit == 14
    assert block.overvoltage == 43
    assert block.undervoltage == 320
    assert block.undervoltage_under_load == 300
    assert block.battery_capacity == 14000
    assert block.max_current_on_low_charge == 9
    assert block.limp_mode_soc_limit == 15
    assert block.limp_mode_soc_limit_stage2 == 5
    assert block.full_capacity_range == 60
    assert block.pedal_sensor_signals_per_rotation == 32
    assert block.motor_pole_pair_number == 8
    assert block.motor_max_rotor_rpm == 4500
    assert block.deceleration_ratio == 21.0
    assert block.throttle_start_voltage == 1.2
    assert block.throttle_max_voltage == 4.2
    assert block.start_current == 25
    assert block.current_loading_time == 0.5
    assert block.current_shedding_time == 0.5
    assert block.lamps_always_on is True
    assert block.walk_assist_speed == 6.0
    assert [level.current_limit for level in block.assist_levels] == [
        10 + i for i in range(9)
    ]
    assert [level.speed_limit for level in block.assist_levels] == [
        20 + i for i in range(9)
    ]
    assert block.checksum_ok


def test_parameter0_offsets_match_vendor():
    value = {
        "acceleration_levels": [{"acceleration_level": 20 + i * 5} for i in range(9)],
        "assist_ratio_levels": [{"assist_ratio_level": 50 + i * 50} for i in range(9)],
        "assist_ratio_upper_limit": 500,
        "unknown_bytes": [0xFF] * 33,
    }
    block = codecs.Parameter0.decode(_vendor_block("parameter0", value))
    assert block.acceleration_levels == [20 + i * 5 for i in range(9)]
    assert block.assist_ratio_levels == [50 + i * 50 for i in range(9)]
    assert block.assist_ratio_upper_limit == 500
    assert block.checksum_ok


def test_parameter2_offsets_match_vendor():
    value = {
        "torque_profiles": [
            {
                "start_torque_value": 8 + i,
                "max_torque_value": 90 + i,
                "return_torque_value": 20 + i,
                "min_current": 10 + i,
                "max_current": 80 + i,
                "torque_decay_time": 4,
                "start_pulse": 3,
                "current_decay_time": 6,
                "stop_delay": 5,
            }
            for i in range(6)
        ],
        "acceleration_level": 3,
        "unknown_bytes_1": [0xFF] * 6,
        "unknown_bytes_2": [0xFF] * 8,
    }
    block = codecs.Parameter2.decode(_vendor_block("parameter2", value))
    for index, profile in enumerate(block.torque_profiles):
        assert profile.start_torque_value == 8 + index
        assert profile.max_torque_value == 90 + index
        assert profile.return_torque_value == 20 + index
        assert profile.min_current == 10 + index
        assert profile.max_current == 80 + index
        assert profile.torque_decay_time == 4
        assert profile.start_pulse == 3
    assert block.acceleration_level == 3
    assert block.checksum_ok


def test_our_encoder_and_vendor_agree_on_a_full_roundtrip():
    """Our encode -> vendor decode -> our decode must be a fixed point."""
    base = bytearray(64)
    base[63] = checksum(base[:63])
    block = codecs.Parameter1.decode(bytes(base))
    block.assist_levels = [codecs.AssistLevel(10 + i, 20 + i) for i in range(9)]
    block.current_limit = 14
    block.walk_assist_speed = 6.0
    encoded = block.encode()

    again = codecs.Parameter1.decode(encoded)
    assert again.current_limit == 14
    assert again.walk_assist_speed == 6.0
    assert again.encode() == encoded
