"""Payload codecs for the Bafang CAN protocol.

Every configuration block is decoded into a dataclass that keeps the original
raw bytes. Writes are always read-modify-write: the fields you changed are
patched into the block that was read from the device, the checksum byte is
recomputed, and everything else is sent back untouched. That matters because
several bytes in every block have no known meaning; blanking them is how a
motor ends up unrideable.

Layout sources
--------------
* Parameter1/Parameter2/SpeedParameters: both projects agree except for the
  under-voltage fields (see ``Parameter1``) and the assist speed-limit write
  offset, where ``OpenBafangTool`` writes at 48+i but reads at 49+i. The read
  offset (49+i) is the consistent one and is used here.
* Parameter0: only ``bafang_canable_pro`` decodes this block.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, asdict
from typing import Any, Sequence

from .constants import (
    LOW_VOLTAGE_LIMITS,
    Wheel,
    wheel_by_code,
)
from .frame import checksum, int_to_bytes_le, string_from_bytes

BLOCK_LEN = 64


class DecodeError(ValueError):
    pass


def _u16(data: Sequence[int], offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8)


def _s16(data: Sequence[int], offset: int) -> int:
    value = _u16(data, offset)
    return value - 0x10000 if value & 0x8000 else value


def _u24(data: Sequence[int], offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16)


def _put16(buf: list[int], offset: int, value: int) -> None:
    low, high = int_to_bytes_le(int(round(value)), 2)
    buf[offset] = low
    buf[offset + 1] = high


def _temp(byte: int) -> int | None:
    """Temperatures are offset by 40 degrees; 0xFF means "no sensor"."""
    return None if byte == 0xFF else byte - 40


@dataclass
class Block:
    """Base class for a 64-byte configuration block."""

    raw: bytes = field(repr=False, default=b"")

    @property
    def checksum_ok(self) -> bool:
        return len(self.raw) == BLOCK_LEN and self.raw[63] == checksum(self.raw[:63])

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["raw"] = self.raw.hex()
        return out

    def _buffer(self) -> list[int]:
        if len(self.raw) != BLOCK_LEN:
            raise DecodeError(
                f"{type(self).__name__} has no source block to patch "
                f"(got {len(self.raw)} bytes, need {BLOCK_LEN}). "
                "Read the block from the device before writing it."
            )
        return list(self.raw)

    @staticmethod
    def _finish(buf: list[int]) -> bytes:
        buf[63] = checksum(buf[:63])
        return bytes(buf)


# --------------------------------------------------------------------------
# Parameter0 -- acceleration and assist ratio (0x60/0x10)
# --------------------------------------------------------------------------


@dataclass
class Parameter0(Block):
    """Per-assist-level acceleration and assist ratio.

    ``acceleration_levels[i]`` and ``assist_ratio_levels[i]`` correspond to
    assist level i+1. The assist ratio is in percent of rider power.
    """

    acceleration_levels: list[int] = field(default_factory=list)
    assist_ratio_levels: list[int] = field(default_factory=list)
    assist_ratio_upper_limit: int = 0

    @classmethod
    def decode(cls, data: bytes) -> "Parameter0":
        if len(data) < BLOCK_LEN:
            raise DecodeError(f"Parameter0 needs {BLOCK_LEN} bytes, got {len(data)}")
        return cls(
            raw=bytes(data[:BLOCK_LEN]),
            acceleration_levels=[data[1 + i] for i in range(9)],
            assist_ratio_levels=[_u16(data, 10 + i * 2) for i in range(9)],
            assist_ratio_upper_limit=_u16(data, 28),
        )

    def encode(self) -> bytes:
        buf = self._buffer()
        if len(self.acceleration_levels) != 9 or len(self.assist_ratio_levels) != 9:
            raise DecodeError("Parameter0 needs exactly 9 acceleration and ratio levels")
        for i, value in enumerate(self.acceleration_levels):
            buf[1 + i] = int(value) & 0xFF
        for i, value in enumerate(self.assist_ratio_levels):
            _put16(buf, 10 + i * 2, value)
        _put16(buf, 28, self.assist_ratio_upper_limit)
        return self._finish(buf)


# --------------------------------------------------------------------------
# Parameter1 -- electrical/mechanical setup (0x60/0x11)
# --------------------------------------------------------------------------


@dataclass
class AssistLevel:
    current_limit: int  # percent of current_limit
    speed_limit: int  # percent of speed_limit


@dataclass
class Parameter1(Block):
    """Battery, motor and assist-level configuration.

    The under-voltage fields differ between the two upstream projects:
    ``OpenBafangTool`` reads bytes 3/4/5 as three single-byte volt values,
    ``bafang_canable_pro`` reads bytes 3..4 and 5..6 as two 16-bit values in
    units of 0.1 V. The 16-bit reading is what current CAN drive units send,
    so it is used here; ``undervoltage_bytes`` keeps the raw bytes so you can
    check the interpretation against your own unit before writing.
    """

    system_voltage: int = 0
    current_limit: int = 0  # A
    overvoltage: int = 0  # V
    undervoltage_under_load: int = 0  # 0.1 V
    undervoltage: int = 0  # 0.1 V
    battery_capacity: int = 0  # mAh
    max_current_on_low_charge: int = 0  # A
    limp_mode_soc_limit: int = 0  # %
    limp_mode_soc_limit_stage2: int = 0  # %
    full_capacity_range: int = 0  # km
    pedal_sensor_type: int = 0
    coaster_brake: bool = False
    pedal_sensor_signals_per_rotation: int = 0
    speed_sensor_channel_number: int = 0
    motor_type: int = 0
    motor_pole_pair_number: int = 0
    speedmeter_magnets_number: int = 0
    temperature_sensor_type: int = 0
    deceleration_ratio: float = 0.0
    motor_max_rotor_rpm: int = 0
    motor_d_axis_inductance: int = 0
    motor_q_axis_inductance: int = 0
    motor_phase_resistance: int = 0
    motor_reverse_potential_coefficient: int = 0
    throttle_start_voltage: float = 0.0  # V
    throttle_max_voltage: float = 0.0  # V
    start_current: int = 0  # %
    current_loading_time: float = 0.0  # s
    current_shedding_time: float = 0.0  # s
    assist_levels: list[AssistLevel] = field(default_factory=list)
    displayless_mode: bool = False
    lamps_always_on: bool = False
    walk_assist_speed: float = 0.0  # km/h
    undervoltage_bytes: list[int] = field(default_factory=list)

    @classmethod
    def decode(cls, data: bytes) -> "Parameter1":
        if len(data) < BLOCK_LEN:
            raise DecodeError(f"Parameter1 needs {BLOCK_LEN} bytes, got {len(data)}")
        return cls(
            raw=bytes(data[:BLOCK_LEN]),
            system_voltage=data[0],
            current_limit=data[1],
            overvoltage=data[2],
            undervoltage_under_load=_u16(data, 3),
            undervoltage=_u16(data, 5),
            battery_capacity=_u16(data, 7),
            max_current_on_low_charge=data[9],
            limp_mode_soc_limit=data[10],
            limp_mode_soc_limit_stage2=data[11],
            full_capacity_range=data[12],
            pedal_sensor_type=data[13],
            coaster_brake=data[14] == 1,
            pedal_sensor_signals_per_rotation=data[15],
            speed_sensor_channel_number=data[16],
            motor_type=data[18],
            motor_pole_pair_number=data[19],
            speedmeter_magnets_number=data[20],
            temperature_sensor_type=data[21],
            deceleration_ratio=_u16(data, 22) / 100,
            motor_max_rotor_rpm=_u16(data, 24),
            motor_d_axis_inductance=_u16(data, 26),
            motor_q_axis_inductance=_u16(data, 28),
            motor_phase_resistance=_u16(data, 30),
            motor_reverse_potential_coefficient=_u16(data, 32),
            throttle_start_voltage=data[34] / 10,
            throttle_max_voltage=data[35] / 10,
            start_current=data[37],
            current_loading_time=data[38] / 10,
            current_shedding_time=data[39] / 10,
            assist_levels=[
                AssistLevel(current_limit=data[40 + i], speed_limit=data[49 + i])
                for i in range(9)
            ],
            displayless_mode=data[58] == 1,
            lamps_always_on=data[59] == 1,
            walk_assist_speed=_u16(data, 60) / 100,
            undervoltage_bytes=list(data[3:7]),
        )

    def encode(self) -> bytes:
        buf = self._buffer()
        buf[1] = int(self.current_limit)
        buf[2] = int(self.overvoltage)
        _put16(buf, 3, self.undervoltage_under_load)
        _put16(buf, 5, self.undervoltage)
        _put16(buf, 7, self.battery_capacity)
        buf[9] = int(self.max_current_on_low_charge)
        buf[10] = int(self.limp_mode_soc_limit)
        buf[11] = int(self.limp_mode_soc_limit_stage2)
        buf[12] = int(self.full_capacity_range)
        buf[13] = int(self.pedal_sensor_type)
        buf[14] = 1 if self.coaster_brake else 0
        buf[15] = int(self.pedal_sensor_signals_per_rotation)
        buf[16] = int(self.speed_sensor_channel_number)
        buf[20] = int(self.speedmeter_magnets_number)
        buf[34] = int(round(self.throttle_start_voltage * 10))
        buf[35] = int(round(self.throttle_max_voltage * 10))
        buf[37] = int(self.start_current)
        buf[38] = int(round(self.current_loading_time * 10))
        buf[39] = int(round(self.current_shedding_time * 10))
        if len(self.assist_levels) != 9:
            raise DecodeError("Parameter1 needs exactly 9 assist levels")
        for i, level in enumerate(self.assist_levels):
            buf[40 + i] = int(level.current_limit)
            buf[49 + i] = int(level.speed_limit)
        buf[58] = 1 if self.displayless_mode else 0
        buf[59] = 1 if self.lamps_always_on else 0
        _put16(buf, 60, self.walk_assist_speed * 100)
        return self._finish(buf)

    def validate(self) -> list[str]:
        """Sanity checks that are cheap to run before a write."""
        problems: list[str] = []
        if not 0 < self.current_limit <= 60:
            problems.append(f"current_limit {self.current_limit} A is out of range 1..60")
        window = LOW_VOLTAGE_LIMITS.get(self.system_voltage)
        if window:
            low, high = window
            volts = self.undervoltage / 10
            if not low <= volts <= high:
                problems.append(
                    f"undervoltage {volts:.1f} V is outside the safe window "
                    f"{low}..{high} V for a {self.system_voltage} V system"
                )
        for i, level in enumerate(self.assist_levels, start=1):
            if level.current_limit > 100 or level.speed_limit > 100:
                problems.append(f"assist level {i} is above 100 %")
        return problems


# --------------------------------------------------------------------------
# Parameter2 -- torque sensor response (0x60/0x12)
# --------------------------------------------------------------------------


@dataclass
class TorqueProfile:
    start_torque_value: int
    max_torque_value: int
    return_torque_value: int
    min_current: int  # %
    max_current: int  # %
    torque_decay_time: int
    start_pulse: int
    current_decay_time: int  # ms (raw * 5)
    stop_delay: int  # ms (raw * 2)


@dataclass
class Parameter2(Block):
    """Six torque profiles plus the global acceleration level."""

    torque_profiles: list[TorqueProfile] = field(default_factory=list)
    acceleration_level: int = 0

    @classmethod
    def decode(cls, data: bytes) -> "Parameter2":
        if len(data) < BLOCK_LEN:
            raise DecodeError(f"Parameter2 needs {BLOCK_LEN} bytes, got {len(data)}")
        return cls(
            raw=bytes(data[:BLOCK_LEN]),
            torque_profiles=[
                TorqueProfile(
                    start_torque_value=data[0 + i],
                    max_torque_value=data[6 + i],
                    return_torque_value=data[12 + i],
                    max_current=data[18 + i],
                    min_current=data[24 + i],
                    torque_decay_time=data[30 + i],
                    start_pulse=data[36 + i],
                    current_decay_time=data[42 + i] * 5,
                    stop_delay=data[48 + i] * 2,
                )
                for i in range(6)
            ],
            acceleration_level=data[54],
        )

    def encode(self) -> bytes:
        buf = self._buffer()
        if len(self.torque_profiles) != 6:
            raise DecodeError("Parameter2 needs exactly 6 torque profiles")
        for i, profile in enumerate(self.torque_profiles):
            buf[0 + i] = int(profile.start_torque_value)
            buf[6 + i] = int(profile.max_torque_value)
            buf[12 + i] = int(profile.return_torque_value)
            buf[18 + i] = int(profile.max_current)
            buf[24 + i] = int(profile.min_current)
            buf[30 + i] = int(profile.torque_decay_time)
            buf[36 + i] = int(profile.start_pulse)
            buf[42 + i] = int(profile.current_decay_time) // 5
            buf[48 + i] = int(profile.stop_delay) // 2
        buf[54] = int(self.acceleration_level)
        return self._finish(buf)


# --------------------------------------------------------------------------
# Speed parameters (0x32/0x03) -- 6 bytes, no checksum
# --------------------------------------------------------------------------


@dataclass
class SpeedParameters:
    speed_limit: float  # km/h
    wheel_code: tuple[int, int]
    circumference: int  # mm
    raw: bytes = field(repr=False, default=b"")

    @property
    def wheel(self) -> Wheel | None:
        return wheel_by_code(*self.wheel_code)

    @classmethod
    def decode(cls, data: bytes) -> "SpeedParameters":
        if len(data) < 6:
            raise DecodeError(f"Speed parameters need 6 bytes, got {len(data)}")
        return cls(
            speed_limit=_u16(data, 0) / 100,
            wheel_code=(data[2], data[3]),
            circumference=_u16(data, 4),
            raw=bytes(data[:6]),
        )

    def encode(self) -> bytes:
        buf = [0] * 6
        _put16(buf, 0, self.speed_limit * 100)
        buf[2], buf[3] = self.wheel_code
        _put16(buf, 4, self.circumference)
        return bytes(buf)

    def validate(self) -> list[str]:
        problems: list[str] = []
        wheel = self.wheel
        if wheel is None:
            problems.append(
                f"wheel code {self.wheel_code[0]:#04x},{self.wheel_code[1]:#04x} "
                "is not in the known wheel table"
            )
        elif not wheel.min_circumference <= self.circumference <= wheel.max_circumference:
            problems.append(
                f"circumference {self.circumference} mm is outside "
                f"{wheel.min_circumference}..{wheel.max_circumference} mm "
                f"allowed for a {wheel.text}\" wheel"
            )
        if not 0 < self.speed_limit <= 60:
            problems.append(f"speed limit {self.speed_limit} km/h is out of range")
        return problems

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["raw"] = self.raw.hex()
        out["wheel"] = self.wheel.text if self.wheel else None
        return out


# --------------------------------------------------------------------------
# Live data
# --------------------------------------------------------------------------


@dataclass
class ControllerRealtime0:
    remaining_capacity: int  # %
    single_trip: float  # km
    cadence: int  # rpm
    torque: int  # mV from the torque sensor ADC
    remaining_distance: float | None  # km

    @classmethod
    def decode(cls, data: bytes) -> "ControllerRealtime0":
        if len(data) < 8:
            raise DecodeError("ControllerRealtime0 needs 8 bytes")
        remaining = _u16(data, 6)
        return cls(
            remaining_capacity=data[0],
            single_trip=_u16(data, 1) / 100,
            cadence=data[3],
            torque=_u16(data, 4),
            remaining_distance=None if remaining == 0xFFFF else remaining / 100,
        )


@dataclass
class ControllerRealtime1:
    speed: float  # km/h
    current: float  # A
    voltage: float  # V
    temperature: int | None  # controller, degC
    motor_temperature: int | None  # degC

    @classmethod
    def decode(cls, data: bytes) -> "ControllerRealtime1":
        if len(data) < 8:
            raise DecodeError("ControllerRealtime1 needs 8 bytes")
        return cls(
            speed=_u16(data, 0) / 100,
            current=_u16(data, 2) / 100,
            voltage=_u16(data, 4) / 100,
            temperature=_temp(data[6]),
            motor_temperature=_temp(data[7]),
        )


@dataclass
class SensorRealtime:
    torque: int  # raw ADC counts
    cadence: int  # rpm

    @classmethod
    def decode(cls, data: bytes) -> "SensorRealtime":
        if len(data) < 3:
            raise DecodeError("SensorRealtime needs 3 bytes")
        return cls(torque=_u16(data, 0), cadence=data[2])


@dataclass
class BatteryCapacity:
    full_capacity: int  # mAh
    capacity_left: int  # mAh
    rsoc: int  # %
    asoc: int  # %
    soh: int  # %

    @classmethod
    def decode(cls, data: bytes) -> "BatteryCapacity":
        if len(data) < 7:
            raise DecodeError("BatteryCapacity needs 7 bytes")
        return cls(
            full_capacity=_u16(data, 0),
            capacity_left=_u16(data, 2),
            rsoc=data[4],
            asoc=data[5],
            soh=data[6],
        )


@dataclass
class BatteryState:
    current: float  # A, negative while charging
    voltage: float  # V
    temperature: int  # degC

    @classmethod
    def decode(cls, data: bytes) -> "BatteryState":
        if len(data) < 5:
            raise DecodeError("BatteryState needs 5 bytes")
        return cls(
            current=_s16(data, 0) / 100,
            voltage=_u16(data, 2) / 100,
            temperature=data[4] - 40,
        )


@dataclass
class DisplayData1:
    total_mileage: int  # km
    single_mileage: float  # km
    max_speed: float  # km/h

    @classmethod
    def decode(cls, data: bytes) -> "DisplayData1":
        if len(data) < 8:
            raise DecodeError("DisplayData1 needs 8 bytes")
        return cls(
            total_mileage=_u24(data, 0),
            single_mileage=_u24(data, 3) / 10,
            max_speed=_u16(data, 6) / 10,
        )


@dataclass
class DisplayData2:
    average_speed: float  # km/h
    service_mileage: float  # km

    @classmethod
    def decode(cls, data: bytes) -> "DisplayData2":
        if len(data) < 5:
            raise DecodeError("DisplayData2 needs 5 bytes")
        return cls(average_speed=_u16(data, 0) / 10, service_mileage=_u24(data, 2) / 10)


def decode_error_codes(data: bytes) -> list[int]:
    """Error codes arrive as an ASCII digit string, two digits per code."""
    text = string_from_bytes(data)
    codes: list[int] = []
    for i in range(0, len(text) - 1, 2):
        chunk = text[i : i + 2]
        if chunk.isdigit():
            codes.append(int(chunk, 10))
    return codes


#: Blocks that are read-modify-write 64 byte blocks with a checksum.
CONFIG_BLOCKS = {
    "Parameter0": Parameter0,
    "Parameter1": Parameter1,
    "Parameter2": Parameter2,
}


def block_field_names(cls: type) -> list[str]:
    return [f.name for f in fields(cls) if f.name != "raw"]
