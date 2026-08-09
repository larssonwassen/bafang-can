"""Differential test against the vendored JavaScript implementation.

The Python codecs were derived by reading ``vendor/bafang_canable_pro`` and
``vendor/OpenBafangTool`` rather than by importing them, so this test feeds
random payloads through both the vendored JS parsers and the Python ones and
compares every field they claim to share.

Where the two upstream projects disagree with each other, the disagreement is
recorded in ``KNOWN_DIFFERENCES`` with the reason, instead of being papered
over.

Skipped when node or the submodules are missing.
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from bafang_can import codecs
from bafang_can.frame import checksum

VENDOR = Path(__file__).resolve().parents[1] / "vendor" / "bafang_canable_pro"
SCRIPT = Path(__file__).parent / "differential" / "parse_with_vendor.js"

pytestmark = [
    pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed"),
    pytest.mark.skipif(
        not (VENDOR / "bafang-parser.js").exists(),
        reason="git submodules are not checked out",
    ),
]

#: Fields where this tool deliberately differs from the vendored JS.
KNOWN_DIFFERENCES = {
    # OpenBafangTool reads byte 5 as battery_recovery_voltage while
    # bafang_canable_pro reads bytes 5..6 as a 16 bit undervoltage. We follow
    # the 16 bit reading, which makes the single-byte field meaningless.
    "battery_recovery_voltage",
    "par1_value_offset_6",
    "par1_value_offset_17",
    "par1_value_offset_62",
    "par0_value_offset_0",
    # Bookkeeping the JS parser exposes and we keep as raw bytes instead.
    "unknown_bytes",
    "unknown_bytes_1",
    "unknown_bytes_2",
    "checksum_missmatch",
    "parseError",
    "wheel_diameter_code",
}


def run_vendor(cases: list[dict]) -> list[dict]:
    result = subprocess.run(
        ["node", str(SCRIPT)],
        input=json.dumps({"cases": cases}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"vendor parser failed: {result.stderr}")
    return json.loads(result.stdout)


def random_block(rng: random.Random) -> list[int]:
    body = [rng.randrange(256) for _ in range(63)]
    return [*body, checksum(body)]


def random_payload(rng: random.Random, length: int) -> list[int]:
    return [rng.randrange(256) for _ in range(length)]


def compare(python_value, js_value, field: str) -> None:
    if isinstance(python_value, float) or isinstance(js_value, float):
        assert python_value == pytest.approx(js_value), field
    else:
        assert python_value == js_value, field


@pytest.fixture(scope="module")
def rng() -> random.Random:
    return random.Random(20260809)


def test_parameter1_matches_vendor(rng):
    blocks = [random_block(rng) for _ in range(50)]
    vendor = run_vendor([{"kind": "parameter1", "data": b} for b in blocks])

    for raw, js in zip(blocks, vendor):
        block = codecs.Parameter1.decode(bytes(raw))
        for field, js_value in js.items():
            if field in KNOWN_DIFFERENCES:
                continue
            if field == "assist_levels":
                for index, level in enumerate(js_value):
                    compare(
                        block.assist_levels[index].current_limit,
                        level["current_limit"],
                        f"assist_levels[{index}].current_limit",
                    )
                    compare(
                        block.assist_levels[index].speed_limit,
                        level["speed_limit"],
                        f"assist_levels[{index}].speed_limit",
                    )
                continue
            compare(getattr(block, field), js_value, field)


def test_parameter0_matches_vendor(rng):
    blocks = [random_block(rng) for _ in range(50)]
    vendor = run_vendor([{"kind": "parameter0", "data": b} for b in blocks])

    for raw, js in zip(blocks, vendor):
        block = codecs.Parameter0.decode(bytes(raw))
        assert block.acceleration_levels == [
            level["acceleration_level"] for level in js["acceleration_levels"]
        ]
        assert block.assist_ratio_levels == [
            level["assist_ratio_level"] for level in js["assist_ratio_levels"]
        ]
        assert block.assist_ratio_upper_limit == js["assist_ratio_upper_limit"]


def test_parameter2_matches_vendor(rng):
    blocks = [random_block(rng) for _ in range(50)]
    vendor = run_vendor([{"kind": "parameter2", "data": b} for b in blocks])

    for raw, js in zip(blocks, vendor):
        block = codecs.Parameter2.decode(bytes(raw))
        assert block.acceleration_level == js["acceleration_level"]
        for index, profile in enumerate(js["torque_profiles"]):
            ours = block.torque_profiles[index]
            for field in (
                "start_torque_value",
                "max_torque_value",
                "return_torque_value",
                "min_current",
                "max_current",
                "torque_decay_time",
                "start_pulse",
            ):
                compare(getattr(ours, field), profile[field], f"{index}.{field}")
            # The JS parser reports these raw; OpenBafangTool scales them and
            # so do we (x5 ms and x2 ms respectively).
            compare(
                ours.current_decay_time,
                profile["current_decay_time"] * 5,
                f"{index}.current_decay_time",
            )
            compare(ours.stop_delay, profile["stop_delay"] * 2, f"{index}.stop_delay")


def test_speed_parameters_match_vendor(rng):
    payloads = [random_payload(rng, 6) for _ in range(50)]
    vendor = run_vendor([{"kind": "speed", "data": p} for p in payloads])

    for raw, js in zip(payloads, vendor):
        params = codecs.SpeedParameters.decode(bytes(raw))
        assert params.speed_limit == pytest.approx(js["speed_limit"])
        assert list(params.wheel_code) == js["wheel_diameter_code"]
        assert params.circumference == js["circumference"]


def test_realtime_matches_vendor(rng):
    payloads = [random_payload(rng, 8) for _ in range(50)]

    vendor0 = run_vendor([{"kind": "realtime0", "data": p} for p in payloads])
    for raw, js in zip(payloads, vendor0):
        value = codecs.ControllerRealtime0.decode(bytes(raw))
        compare(value.remaining_capacity, js["remaining_capacity"], "remaining_capacity")
        compare(value.single_trip, js["single_trip"], "single_trip")
        compare(value.cadence, js["cadence"], "cadence")
        compare(value.torque, js["torque"], "torque")
        compare(value.remaining_distance, js["remaining_distance"], "remaining_distance")

    vendor1 = run_vendor([{"kind": "realtime1", "data": p} for p in payloads])
    for raw, js in zip(payloads, vendor1):
        value = codecs.ControllerRealtime1.decode(bytes(raw))
        compare(value.speed, js["speed"], "speed")
        compare(value.current, js["current"], "current")
        compare(value.voltage, js["voltage"], "voltage")
        compare(value.temperature, js["temperature"], "temperature")
        compare(value.motor_temperature, js["motor_temperature"], "motor_temperature")


def test_sensor_and_battery_match_vendor(rng):
    payloads = [random_payload(rng, 8) for _ in range(50)]

    for raw, js in zip(payloads, run_vendor([{"kind": "sensor", "data": p} for p in payloads])):
        value = codecs.SensorRealtime.decode(bytes(raw))
        compare(value.torque, js["torque"], "torque")
        compare(value.cadence, js["cadence"], "cadence")

    for raw, js in zip(
        payloads, run_vendor([{"kind": "battery_state", "data": p} for p in payloads])
    ):
        value = codecs.BatteryState.decode(bytes(raw))
        compare(value.current, js["current"], "current")
        compare(value.voltage, js["voltage"], "voltage")
        compare(value.temperature, js["temperature"], "temperature")

    for raw, js in zip(
        payloads, run_vendor([{"kind": "battery_capacity", "data": p} for p in payloads])
    ):
        value = codecs.BatteryCapacity.decode(bytes(raw))
        for field in ("full_capacity", "capacity_left", "rsoc", "asoc", "soh"):
            compare(getattr(value, field), js[field], field)


def test_display_blocks_match_vendor(rng):
    payloads = [random_payload(rng, 8) for _ in range(50)]

    for raw, js in zip(payloads, run_vendor([{"kind": "display1", "data": p} for p in payloads])):
        value = codecs.DisplayData1.decode(bytes(raw))
        compare(value.total_mileage, js["total_mileage"], "total_mileage")
        compare(value.single_mileage, js["single_mileage"], "single_mileage")
        compare(value.max_speed, js["max_speed"], "max_speed")

    for raw, js in zip(payloads, run_vendor([{"kind": "display2", "data": p} for p in payloads])):
        value = codecs.DisplayData2.decode(bytes(raw))
        compare(value.average_speed, js["average_speed"], "average_speed")
        compare(value.service_mileage, js["service_mileage"], "service_mileage")


def test_error_codes_match_vendor():
    payloads = [
        list(b"0821\x00"),
        list(b"14\x00"),
        list(b"\x00"),
        list(b"304142\x00"),
    ]
    vendor = run_vendor([{"kind": "error_codes", "data": p} for p in payloads])
    for raw, js in zip(payloads, vendor):
        assert codecs.decode_error_codes(bytes(raw)) == js
