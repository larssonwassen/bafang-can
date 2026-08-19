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

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from .constants import (
    LOW_VOLTAGE_LIMITS,
    Wheel,
    error_text,
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


def _put16(buf: list[int], offset: int, value: float) -> None:
    low, high = int_to_bytes_le(round(value), 2)
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

    def _put_flag(self, buf: list[int], offset: int, value: bool) -> None:
        """Write a boolean byte, but only when it actually changed.

        A byte we read as "true" may hold something other than 1 -- these
        fields are single-bit in our model but not necessarily in the
        firmware's. Rewriting 0x3D as 0x01 because we decoded it as True would
        silently discard whatever else that byte encodes, so an unchanged flag
        leaves the original byte alone.
        """
        if bool(self.raw[offset] == 1) != value:
            buf[offset] = 1 if value else 0


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
    def decode(cls, data: bytes) -> Parameter0:
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
    speed_limit_enabled: int = 0
    start_current: int = 0  # %
    current_loading_time: float = 0.0  # s
    current_shedding_time: float = 0.0  # s
    assist_levels: list[AssistLevel] = field(default_factory=list)
    displayless_mode: bool = False
    lamps_always_on: bool = False
    walk_assist_speed: float = 0.0  # km/h
    undervoltage_bytes: list[int] = field(default_factory=list)

    @classmethod
    def decode(cls, data: bytes) -> Parameter1:
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
            speed_limit_enabled=data[36],
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
        self._put_flag(buf, 14, self.coaster_brake)
        buf[15] = int(self.pedal_sensor_signals_per_rotation)
        buf[16] = int(self.speed_sensor_channel_number)
        buf[20] = int(self.speedmeter_magnets_number)
        buf[34] = round(self.throttle_start_voltage * 10)
        buf[35] = round(self.throttle_max_voltage * 10)
        buf[36] = int(self.speed_limit_enabled)
        buf[37] = int(self.start_current)
        buf[38] = round(self.current_loading_time * 10)
        buf[39] = round(self.current_shedding_time * 10)
        if len(self.assist_levels) != 9:
            raise DecodeError("Parameter1 needs exactly 9 assist levels")
        for i, level in enumerate(self.assist_levels):
            buf[40 + i] = int(level.current_limit)
            buf[49 + i] = int(level.speed_limit)
        self._put_flag(buf, 58, self.displayless_mode)
        self._put_flag(buf, 59, self.lamps_always_on)
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
    def decode(cls, data: bytes) -> Parameter2:
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
    """Drive unit 0x32/0x03 -- speed limit, wheel size and circumference.

    Confirmed against the vehicle's own manual, which is about as independent
    as a check gets: the manual states this bike's wheel setting is locked to
    28" and its speed to 25 km/h, and decoding the broadcast
    ``C4 09 C0 01 9D 08`` gives 25.0 km/h and a 28" wheel code. Two documents
    that share no code path agreeing on both fields.

    Note what "locked" means there. It describes the *display menu*, which
    shows those two settings and refuses to change them. It is not a property
    of this message: the drive unit broadcasts `32/03` every two seconds and
    the write command for it is the same `32/03`, which is why fitting a
    generic Bafang display is reported to make them adjustable again.
    """

    speed_limit: float  # km/h
    wheel_code: tuple[int, int]
    circumference: int  # mm
    raw: bytes = field(repr=False, default=b"")

    @property
    def wheel(self) -> Wheel | None:
        return wheel_by_code(*self.wheel_code)

    @classmethod
    def decode(cls, data: bytes) -> SpeedParameters:
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
    def decode(cls, data: bytes) -> ControllerRealtime0:
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
    def decode(cls, data: bytes) -> ControllerRealtime1:
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
    #: Rolling message counter, present when the sensor broadcasts 4 bytes.
    #:
    #: Neither vendored project reads this byte, but a real G210 sensor
    #: increments it once per broadcast and wraps at 0xFF. That makes it the
    #: only thing on a Bafang bus that reveals a dropped frame, which is what
    #: :mod:`bafang_can.quality` uses it for. ``None`` when the payload is the
    #: 3 bytes the vendor parsers expect, so nothing is invented.
    #:
    #: Independently corroborated on an M500: the ``DP_C240_241`` shutdown log
    #: published by ``OpenSourceEBike/Bafang_M500_M600`` runs 333 frames of
    #: ``31/00`` in which bytes 0-2 never move and this byte advances by one
    #: per frame through a full wrap. That project's table calls byte 3
    #: "Progressive", which is the same observation under a vaguer name.
    counter: int | None = None

    @classmethod
    def decode(cls, data: bytes) -> SensorRealtime:
        if len(data) < 3:
            raise DecodeError("SensorRealtime needs 3 bytes")
        return cls(
            torque=_u16(data, 0),
            cadence=data[2],
            counter=data[3] if len(data) > 3 else None,
        )


@dataclass
class BatteryCapacity:
    full_capacity: int  # mAh
    capacity_left: int  # mAh
    rsoc: int  # %
    asoc: int  # %
    soh: int  # %

    @classmethod
    def decode(cls, data: bytes) -> BatteryCapacity:
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
    def decode(cls, data: bytes) -> BatteryState:
        if len(data) < 5:
            raise DecodeError("BatteryState needs 5 bytes")
        return cls(
            current=_s16(data, 0) / 100,
            voltage=_u16(data, 2) / 100,
            temperature=data[4] - 40,
        )


#: Accumulator counts per turn of the output shaft. See
#: :class:`OutputShaftCounter` for how this was measured, and why it is 64
#: rather than the 60 an early reading against a drill suggested.
SHAFT_COUNTS_PER_REVOLUTION = 64


#: Seconds per tick of the drive unit's 0x30/0x00 uptime counter.
#:
#: Measured as 10.023 to 10.028 s across eight captures on two days. The
#: nominal value is plainly 10 s and the excess is the drive unit's clock
#: running slow by about 0.26%, so this is the observed figure rather than the
#: intended one -- using 10.0 would drift a captured boot instant by a second
#: every six and a half minutes of uptime.
UPTIME_TICK_SECONDS = 10.026


@dataclass
class SystemUptime:
    """Drive unit 0x30/0x00 -- how long this drive unit has been powered.

    Four bytes, little-endian, counting ticks of about ten seconds since the
    drive unit came up. It resets to zero on every power cycle, so it says
    nothing about the life of the motor; what it gives you is an **absolute
    reference for when the bus you are looking at was switched on**, which no
    other message on this bus carries.

    That reference is what makes it worth decoding. Subtracting
    ``seconds`` from a frame's timestamp reconstructs the power-on instant, and
    two captures taken from the same power cycle must agree on it. Four
    captures recorded between 12:42 and 12:51 one afternoon reconstructed the
    same boot to within 5.5 s across that nine-minute span, and two others
    taken four minutes apart agreed to 2 s. A capture whose host timestamps are
    damaged fails that check loudly: the one recording in the set with a
    mangled time base reconstructs a boot 20000 s away from when it was
    actually taken.

    Those 5.5 s are the counter's own resolution, not error. It is broadcast
    once per tick, so where inside a tick a capture began is not observable,
    and one tick is the tightest bound worth asserting.

    Neither vendored project decodes this message at all. The BESST command
    table names ``30/00`` ``pulse``, which fits a counter that free-runs from
    power-on, but publishes no layout for it. The one four-byte sample of it in
    the ``OpenSourceEBike`` logs, ``00 00 00 44``, does not decode as this
    counter little-endian; it was captured on a different node address
    (``02FF3000`` rather than the ``02F83000`` this bike broadcasts), so it may
    not be the same message, and one sample is not enough to argue from either
    way. The little-endian reading is what this bike's own frames show: they
    count ``04 00 00 00``, ``05 00 00 00`` and upward in byte 0.
    """

    ticks: int

    @property
    def seconds(self) -> float:
        return self.ticks * UPTIME_TICK_SECONDS

    @classmethod
    def decode(cls, data: bytes) -> SystemUptime:
        if len(data) < 4:
            raise DecodeError(f"SystemUptime needs 4 bytes, got {len(data)}")
        return cls(ticks=int.from_bytes(data[:4], "little"))

    def booted_at(self, timestamp: float) -> float:
        """The wall-clock instant this drive unit powered on.

        ``timestamp`` is when this broadcast was received. Only as good as the
        capture's time base -- which is the point: disagreement between two
        captures of one power cycle means the time base, not the counter.
        """
        return timestamp - self.seconds

    def to_dict(self) -> dict[str, Any]:
        return {"ticks": self.ticks, "seconds": round(self.seconds, 1)}


@dataclass
class OutputShaftCounter:
    """Drive unit 0x32/0x07 bytes 5--6 -- how far the output shaft has turned.

    A u16 little-endian accumulator that only ever increases, and only while
    the output shaft is moving. Bytes 0--4 have always been zero and are not
    interpreted here.

    The scale is **64 counts per revolution**, measured over 23 seconds of
    steady rotation at a cadence the sensor held at 88--89 rpm: the counter
    advanced at 94.46 counts/s, which is 63.68 counts per revolution, and the
    same figure comes back as 63.65 and 63.69 over shorter windows inside it.

    A first attempt put this at 60, from a drill labelled 550 rpm whose
    implied rate came out at 552. The drill was the weaker reference: those
    labels are no-load figures, and at 64 counts per revolution that run works
    out to 518 rpm, 94% of the label, which is ordinary for a drill under load.
    The cadence byte is the better one -- the firmware uses it for its own
    assist decisions, it was steady to +-0.5 rpm for the whole window, and it
    agrees with ``ControllerRealtime0``'s cadence byte exactly. Taking it as
    true rpm puts the count between 63.7 and 64.4, and 64 is the sort of number
    an encoder actually has.

    It is a trip counter, not an odometer: it was observed restarting from
    10833 and from 1689 during one afternoon on the bench, and it read exactly
    0 across both captures of a bike whose motor had no shaft fitted, so
    nothing had ever turned it. Do not read a distance out of it.

    This is the only trustworthy speed source on the bus above about 450 rpm,
    where the torque sensor's cadence byte goes non-monotonic;
    ``ControllerRealtime1``'s speed field stays zero without a wheel-speed
    signal. Two consecutive broadcasts give a shaft rpm at any speed, which is
    what makes it worth decoding rather than merely recording.

    Neither vendored project decodes this message at all. The BESST command
    table names ``30/00`` ``pulse``, which fits a counter that free-runs from
    power-on, but publishes no layout for it. The one four-byte sample of it in
    the ``OpenSourceEBike`` logs, ``00 00 00 44``, does not decode as this
    counter little-endian; it was captured on a different node address
    (``02FF3000`` rather than the ``02F83000`` this bike broadcasts), so it may
    not be the same message, and one sample is not enough to argue from either
    way. The little-endian reading is what this bike's own frames show: they
    count ``04 00 00 00``, ``05 00 00 00`` and upward in byte 0.
    """

    counts: int

    @property
    def revolutions(self) -> float:
        return self.counts / SHAFT_COUNTS_PER_REVOLUTION

    @classmethod
    def decode(cls, data: bytes) -> OutputShaftCounter:
        if len(data) < 7:
            raise DecodeError(
                f"OutputShaftCounter needs 7 bytes, got {len(data)}"
            )
        return cls(counts=_u16(data, 5))

    def rpm_since(self, previous: OutputShaftCounter, seconds: float) -> float | None:
        """Shaft speed between two broadcasts, or ``None`` if it cannot be told.

        A count that goes backwards is either a wrap or a restart, and the two
        need telling apart. The counter is 16 bits, so a wrap can only ever
        happen from the very top of the range; a restart happens from wherever
        the counter had got to. Both were observed on the same bench in one
        afternoon -- 10833 to 158, and 1689 to 0 -- and treating either as a
        wrap turns it into 857 revolutions inside one 96 ms broadcast.

        A restart yields ``None``: no speed can be derived across it, and
        saying so is better than reporting a number that is off by three orders
        of magnitude.
        """
        if seconds <= 0:
            return None
        if self.counts >= previous.counts:
            advance = self.counts - previous.counts
        elif previous.counts > 0xC000 and self.counts < 0x4000:
            advance = self.counts + 0x10000 - previous.counts
        else:
            return None
        return advance / SHAFT_COUNTS_PER_REVOLUTION / seconds * 60

    def to_dict(self) -> dict[str, Any]:
        return {"counts": self.counts, "revolutions": round(self.revolutions, 2)}


#: Assist-level codes, keyed by how many levels the display is configured for.
#:
#: The codes are a lookup table, not an arithmetic scale -- level 5 on a
#: five-level display is 3, which is lower than level 1's 11. Taken from
#: ``bafang_canable_pro``'s ``decodeCurrentAssistLevel`` and confirmed on a
#: ``DP C340.CAN`` by stepping from 5 down to walk assist one press at a time:
#: the bus produced 3, 23, 21, 13, 11, 0, 6, which is exactly this table read
#: backwards.
ASSIST_LEVEL_CODES: dict[int, dict[int, str]] = {
    3: {0: "0", 12: "1", 2: "2", 3: "3", 6: "walk"},
    5: {0: "0", 11: "1", 13: "2", 21: "3", 23: "4", 3: "5", 6: "walk"},
    9: {
        0: "0", 1: "1", 11: "2", 12: "3", 13: "4",
        2: "5", 21: "6", 22: "7", 23: "8", 3: "9", 6: "walk",
    },
}


@dataclass
class DisplayRealtime:
    """Display 0x63/0x00 -- what the rider is asking for, broadcast to the drive unit.

    The display pushes this at about 10 Hz whether or not anyone asks, so it is
    readable in listen-only mode. Layout from ``bafang_canable_pro``'s
    ``BafangCanDisplayParser.package0``, confirmed byte by byte on a
    ``DP C340.CAN``: byte 0's low nibble read 5 on a five-level display, byte 1
    walked the assist table above, and byte 2's two low bits moved exactly as
    the lamp and the ``+`` button were operated.

    ``ride_mode`` and ``mode_byte`` are the two places the bike's *riding
    style* could live, and this data cannot yet separate them. The vehicle
    manual documents a dealer-only "körstilsväljare" with **three** settings --
    dynamisk, standard, kontroll -- shown on the display as D, S and C. Both
    vendored projects read byte 0 bit 4 as a **two**-state ride mode, and
    ``bafang_canable_pro`` comments its own version as "Simplified", so neither
    accounts for a third state. Byte 3 held ``0x01`` in 3567 of 3568 captured
    frames; the single exception is the display's very first frame after
    power-on, which also carries an uninitialised byte 0.

    A three-state setting sitting at its middle value would read 1 in either
    place, which is exactly what both show. The bit is reported as an integer
    rather than a bool so that a third state, if it appears there, is visible
    instead of being flattened; and byte 3 is carried through raw.

    Elimination favours byte 3. The display shows the riding style and never
    reads a parameter block from the drive unit -- across every capture it has
    only ever asked it for a serial number and a software version -- so the
    display holds the setting. The manual says the setting changes how the
    motor behaves, so the controller must be told, and the only messages the
    display sends it are this one and ``63/03``, whose single byte is the
    documented five-minute auto-shutdown. That leaves ``63/00``, and within it
    the byte nobody has claimed.

    A third source complicates that last step and is recorded here rather than
    argued away. ``OpenSourceEBike/Bafang_M500_M600`` labels byte 3 **boost
    mode**, off = ``01`` and on = ``00``. That is not idle speculation -- but
    it is not an observation either: the same page says the byte "is always 1
    with my DPC080 display", so its author never saw it change any more than we
    have. Byte 3 is therefore disputed rather than unclaimed, and the
    elimination argument is weaker than it was, not dead.

    The same comparison turns up something neither reading covers. On that
    DPC080 byte 0 reads ``0x05``; on this ``DP C340.C`` it reads ``0x55`` in
    every frame after boot. Both parsers take the low nibble as the level count
    -- 5, agreeing -- and then read bits 4 and 5 of the high nibble as
    ``ride_mode`` and ``boost``. But ``0x5`` in the high nibble also sets **bit
    6, which no source reads at all**, and it makes the high nibble equal the
    low one. So ``ride_mode = 1`` and ``boost = False`` on this bike may be
    nothing but two bits sliced out of a nibble that is not a set of
    independent flags. ``byte0`` is carried raw so that stays visible.

    It remains an elimination rather than an observation: the setting has never
    been seen to change. Capturing across a change is what would settle it.
    """

    assist_levels: int
    assist_level: str
    ride_mode: int
    boost: bool
    light: bool
    button_up: bool
    button_down: bool
    #: Byte 3. Neither vendored parser reads it, and the third source calls it
    #: boost mode without having watched it change; see the class docstring.
    mode_byte: int | None = None
    #: Byte 0 unmasked. ``assist_levels``, ``ride_mode`` and ``boost`` are all
    #: slices of it and they do not account for all of its bits.
    byte0: int = 0

    @classmethod
    def decode(cls, data: bytes) -> DisplayRealtime:
        if len(data) < 3:
            raise DecodeError(f"DisplayRealtime needs 3 bytes, got {len(data)}")
        levels = data[0] & 0b1111
        table = ASSIST_LEVEL_CODES.get(levels, {})
        return cls(
            assist_levels=levels,
            # An unknown code is reported as such rather than guessed at: the
            # table is per-display and only three configurations are known.
            assist_level=table.get(data[1], f"unknown ({data[1]})"),
            ride_mode=(data[0] >> 4) & 0b1,
            boost=bool(data[0] & 0b100000),
            light=bool(data[2] & 0b1),
            button_up=bool(data[2] & 0b10),
            button_down=bool(data[2] & 0b100000),
            mode_byte=data[3] if len(data) > 3 else None,
            byte0=data[0],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ControllerState:
    """Drive unit 0x12/0x00 -- the fault the controller is showing, live.

    One byte, zero when nothing is wrong. Established by capturing the same
    bike twice: while it displayed error 07 with assist disabled, the drive
    unit broadcast ``07`` here about ten times a second and the display echoed
    it on ``13/00``; after the voltage-sense fault was repaired, both read
    ``00``.

    That matters because ``docs/m200.md`` had recorded that this firmware
    exposes "no stored fault to clear over CAN" -- true of the *stored* code,
    which it will not answer a read for, but the live one is on the wire
    continuously and needs no request at all.

    **A third source reads this byte differently and the disagreement is not
    settled.** ``OpenSourceEBike/Bafang_M500_M600`` documents ``12/00`` as a
    bitfield: bit 0 brake applied, bit 1 motor stopped, bit 2 battery
    undervoltage. Both readings agree that zero means healthy, and they are not
    reconcilable above that -- under theirs, the ``07`` seen here would mean
    brake *and* stopped *and* undervoltage on a bike whose fault was a
    voltage-sense gain error reading high.

    Neither source is obviously stronger. Ours is a correlation with the number
    the screen displayed, on one bike, before and after a repair. Theirs is a
    label without a stated experiment, and their own sample value ``0x11`` sets
    a bit 4 that their three-bit layout does not explain.

    The discriminator is cheap and has not been run: **squeeze the brake lever
    while capturing.** Under the bitfield reading this byte goes to ``0x01``
    with the bike otherwise healthy; under the error-code reading it stays
    ``00``, because 1 is not a fault code. One capture decides it. Until then
    ``error_code`` is what the evidence at hand supports and this note is what
    keeps the alternative from being lost.
    """

    error_code: int

    @property
    def ok(self) -> bool:
        return self.error_code == 0

    @property
    def description(self) -> str:
        return "no fault" if self.ok else error_text(self.error_code)[0]

    @property
    def recommendation(self) -> str:
        return "-" if self.ok else error_text(self.error_code)[1]

    @classmethod
    def decode(cls, data: bytes) -> ControllerState:
        if len(data) < 1:
            raise DecodeError("ControllerState needs 1 byte")
        return cls(error_code=data[0])

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "description": self.description,
            "recommendation": self.recommendation,
        }


@dataclass
class BatteryChargingInfo:
    """Battery 0x64/0x01 -- how the pack has been treated over its life.

    Layout from ``bafang_canable_pro``'s ``chargingInfo``: three little-endian
    u16 in six bytes. The uncharged times are counts of hours; they are kept
    here as hours and formatted only for display, so nothing depends on the
    vendor's "Nd Nh" string.
    """

    charge_cycles: int
    max_uncharged_hours: int
    last_uncharged_hours: int

    @classmethod
    def decode(cls, data: bytes) -> BatteryChargingInfo:
        if len(data) < 6:
            raise DecodeError(
                f"BatteryChargingInfo needs 6 bytes, got {len(data)}"
            )
        return cls(
            charge_cycles=_u16(data, 0),
            max_uncharged_hours=_u16(data, 2),
            last_uncharged_hours=_u16(data, 4),
        )

    @staticmethod
    def _days_and_hours(hours: int) -> str:
        return f"{hours // 24}d {hours % 24}h"

    def to_dict(self) -> dict[str, Any]:
        return {
            "charge_cycles": self.charge_cycles,
            "max_uncharged": self._days_and_hours(self.max_uncharged_hours),
            "last_uncharged": self._days_and_hours(self.last_uncharged_hours),
        }


@dataclass
class DisplayData1:
    """Display 0x63/0x01 -- odometer, trip, and recorded top speed.

    Confirmed on a `DP C340.C`, which returned ``76 01 00 A3 0E 00 56 03``:
    374 km total and 374.7 km trip. A trip that has never been reset sitting a
    fraction above a whole-kilometre total is what these two scalings predict,
    and no other reading of those bytes puts the pair that close.

    This is the only configuration-shaped block anything on this bus will
    answer -- the drive unit is silent on all three of its parameter blocks.

    ``max_speed`` deserves suspicion rather than trust. The same read gave
    85.4 km/h, which no 25 km/h EPAC reaches by pedalling. It is the highest
    value the display has ever latched, so one spurious speed-sensor reading
    sets it permanently, and nothing distinguishes that from a real descent.
    Read it as a high-water mark of the *sensor*.
    """

    total_mileage: int  # km
    single_mileage: float  # km
    max_speed: float  # km/h

    @classmethod
    def decode(cls, data: bytes) -> DisplayData1:
        if len(data) < 8:
            raise DecodeError("DisplayData1 needs 8 bytes")
        return cls(
            total_mileage=_u24(data, 0),
            single_mileage=_u24(data, 3) / 10,
            max_speed=_u16(data, 6) / 10,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_mileage": self.total_mileage,
            "single_mileage": round(self.single_mileage, 1),
            "max_speed": round(self.max_speed, 1),
        }


@dataclass
class DisplayData2:
    """Display 0x63/0x02 -- average speed and distance since last service.

    Unlike :class:`DisplayData1` this has **not** been seen answering on real
    hardware: the `DP C340.C` here is silent on ``63/02``. The layout comes
    from both vendored parsers agreeing, which is worth something but is not
    the same as having read it off a bike.
    """

    average_speed: float  # km/h
    service_mileage: float  # km

    @classmethod
    def decode(cls, data: bytes) -> DisplayData2:
        if len(data) < 5:
            raise DecodeError("DisplayData2 needs 5 bytes")
        return cls(average_speed=_u16(data, 0) / 10, service_mileage=_u24(data, 2) / 10)

    def to_dict(self) -> dict[str, Any]:
        return {
            "average_speed": round(self.average_speed, 1),
            "service_mileage": round(self.service_mileage, 1),
        }


@dataclass
class DisplayAutoShutdown:
    """Display 0x63/0x03 -- idle minutes before the system powers itself off.

    One byte of minutes, with 255 meaning never in ``bafang_canable_pro``'s
    reading. Recorded here as ``05``, and a bench session confirmed the
    behaviour the hard way: the whole bus went silent partway through a run of
    reads, with no device answering afterwards.

    The vehicle manual documents this display's setting as five minutes by
    default, adjustable from 0 to 9 -- so the ``05`` on the wire is this
    display sitting at its factory value, and the field is minutes as the
    upstream parser says. Note the manual's range stops at 9, which leaves no
    room for the 255 "never" case; treat that as a property of other displays
    in the family rather than of this one.

    A published M500 power-on log settles what this message *is*, which was
    still open here: it is not a value the display holds and answers reads for,
    it is a value the display **writes to the drive unit**. In the
    ``DP_C240_241`` start sequence the display's tenth frame is
    ``03106303`` -- a write, one byte, ``05`` -- and it repeats it every second
    or so thereafter, exactly as this bike's display does.
    """

    minutes: int

    @property
    def never(self) -> bool:
        return self.minutes == 0xFF

    @classmethod
    def decode(cls, data: bytes) -> DisplayAutoShutdown:
        if len(data) < 1:
            raise DecodeError("DisplayAutoShutdown needs 1 byte, got 0")
        return cls(minutes=data[0])

    def to_dict(self) -> dict[str, Any]:
        return {"minutes": "never" if self.never else self.minutes}


@dataclass
class DisplayLightLevels:
    """Display 0x63/0x04 -- light sensor and backlight levels.

    Four bytes: how many light-sensor steps exist, which one is active, how
    many backlight steps exist, and which one is active. The layout is
    ``bafang_canable_pro``'s ``BafangCanDisplayParser.package3``;
    ``OpenBafangTool`` does not know this message at all.

    This one is directly checkable on a `DP C340.C`, which is unusual for this
    bus: its menu exposes both halves. The vehicle manual documents the light
    sensor sensitivity and the backlight brightness as separate settings, each
    running 0 to 5 and each defaulting to 3 -- which is exactly the shape this
    layout claims, two independent level counts of 5 with their own current
    value. Set either from the menu and re-read, and only its own byte should
    move.

    That makes it the only field on this bus a user can drive to a chosen value
    on demand rather than wait for the bike to produce.
    """

    light_sensor_levels: int
    light_sensor_level: int
    backlight_levels: int
    backlight_level: int

    @classmethod
    def decode(cls, data: bytes) -> DisplayLightLevels:
        if len(data) < 4:
            raise DecodeError(f"DisplayLightLevels needs 4 bytes, got {len(data)}")
        return cls(
            light_sensor_levels=data[0],
            light_sensor_level=data[1],
            backlight_levels=data[2],
            backlight_level=data[3],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "light_sensor_levels": self.light_sensor_levels,
            "light_sensor_level": self.light_sensor_level,
            "backlight_levels": self.backlight_levels,
            "backlight_level": self.backlight_level,
        }


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
