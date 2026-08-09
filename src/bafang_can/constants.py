"""Constants for the Bafang CAN protocol.

Merged from the two vendored reference implementations:

* ``vendor/OpenBafangTool``      (TypeScript, MIT)  -- command tables, wheel
  diameter table, parameter block layouts, voltage limits.
* ``vendor/bafang_canable_pro``  (JavaScript, GPLv3) -- extra command codes
  (Parameter0, ControllerState, SensorRealtime, calibration, firmware update),
  the multi-frame framing rules and the CANable/gs_usb transport.
"""

from __future__ import annotations

from enum import IntEnum
from typing import NamedTuple

#: Bafang CAN systems run classic CAN 2.0B, 29-bit identifiers, 250 kbit/s.
BITRATE = 250_000

#: Bafang uses no bit-rate switching and no CAN-FD frames.
IS_FD = False


class DeviceId(IntEnum):
    """Node addresses on the Bafang CAN network (5 bit)."""

    TORQUE_SENSOR = 0x01
    DRIVE_UNIT = 0x02
    DISPLAY = 0x03
    BATTERY = 0x04
    #: The service tool. The stock tool is the BESST box; we impersonate it.
    TOOL = 0x05
    BROADCAST = 0x1F


class CanOperation(IntEnum):
    """Operation code carried in bits 16..18 of the CAN identifier."""

    WRITE_CMD = 0x00
    READ_CMD = 0x01
    NORMAL_ACK = 0x02
    ERROR_ACK = 0x03
    MULTIFRAME_START = 0x04
    MULTIFRAME = 0x05
    MULTIFRAME_END = 0x06
    MULTIFRAME_WARNING = 0x07


class PedalSensorType(IntEnum):
    TORQUE_SENSOR = 0
    CADENCE_SENSOR = 1
    THROTTLE_LEVER = 2


class MotorType(IntEnum):
    HUB_MOTOR = 0
    MID_DRIVE = 1
    DIRECT_DRIVE = 2


class TemperatureSensorType(IntEnum):
    NO_SENSOR = 0
    K10 = 1
    PT1000 = 2


class SpeedSensorChannel(IntEnum):
    """Which speed sensor input the controller listens on."""

    BY_DISPLAY = 0
    INTERNAL = 1
    EXTERNAL = 2


#: Nominal system voltages the controller reports in Parameter1 byte 0.
SYSTEM_VOLTAGES = (24, 36, 43, 48)

#: Safe low-voltage cutoff windows per nominal voltage (OpenBafangTool).
LOW_VOLTAGE_LIMITS = {
    24: (16, 21),
    36: (27, 32),
    43: (33, 39),
    48: (38, 43),
}

#: Recommended cutoff per nominal voltage and chemistry; -1 = unsupported.
LOW_VOLTAGE_BY_CHEMISTRY = {
    24: {"liion": 20, "lipo": -1, "lifepo4": 20},
    36: {"liion": 31, "lipo": -1, "lifepo4": 30},
    43: {"liion": 38, "lipo": -1, "lifepo4": -1},
    48: {"liion": 41, "lipo": -1, "lifepo4": 40},
}


class Wheel(NamedTuple):
    text: str
    code: tuple[int, int]
    min_circumference: int
    max_circumference: int


#: Wheel diameter codes accepted by the drive unit (0x32/0x03 bytes 2..3).
WHEEL_TABLE: tuple[Wheel, ...] = (
    Wheel("6", (0x60, 0x00), 400, 880),
    Wheel("7", (0x70, 0x00), 520, 880),
    Wheel("8", (0x80, 0x00), 520, 880),
    Wheel("10", (0xA0, 0x00), 520, 880),
    Wheel("12", (0xC0, 0x00), 910, 1300),
    Wheel("14", (0xE0, 0x00), 910, 1300),
    Wheel("16", (0x00, 0x01), 1208, 1600),
    Wheel("17", (0x10, 0x01), 1208, 1600),
    Wheel("18", (0x10, 0x01), 1208, 1600),
    Wheel("20", (0x40, 0x01), 1290, 1880),
    Wheel("22", (0x60, 0x01), 1290, 1880),
    Wheel("23", (0x70, 0x01), 1290, 1880),
    Wheel("24", (0x80, 0x01), 1290, 2200),
    Wheel("25", (0x90, 0x01), 1880, 2200),
    Wheel("26", (0xA0, 0x01), 1880, 2510),
    Wheel("27", (0xB0, 0x01), 1880, 2510),
    Wheel("27.5", (0xB5, 0x01), 1880, 2510),
    Wheel("28", (0xC0, 0x01), 1880, 2510),
    Wheel("29", (0xD0, 0x01), 1880, 2510),
    Wheel("32", (0x00, 0x02), 2200, 2652),
    Wheel("400mm", (0x00, 0x19), 1208, 1600),
    Wheel("450mm", (0x10, 0x2C), 1208, 1600),
    Wheel("600mm", (0x80, 0x25), 1600, 2200),
    Wheel("650mm", (0xA0, 0x28), 1600, 2200),
    Wheel("700mm", (0xC0, 0x2B), 1880, 2510),
)


def wheel_by_text(text: str) -> Wheel | None:
    key = text.strip().replace("″", "").replace('"', "").lower()
    for wheel in WHEEL_TABLE:
        if wheel.text.lower() == key:
            return wheel
    return None


def wheel_by_code(code0: int, code1: int) -> Wheel | None:
    for wheel in WHEEL_TABLE:
        if wheel.code == (code0, code1):
            return wheel
    return None


#: Error codes reported by drive unit / display (command 0x60/0x07).
#:
#: Only three codes have a documented meaning in either upstream project; the
#: remaining entries are codes that are known to exist but whose meaning is not
#: documented. Do not invent descriptions for them -- an undocumented code is
#: reported as such so a wrong guess never drives a repair decision.
ERROR_DESCRIPTIONS: dict[int, tuple[str, str]] = {
    8: (
        "Inner motor hall sensor error (not the speed hall sensor)",
        "Check cable connection, replace motor, or repair the motor if you are "
        "an electronics specialist.",
    ),
    14: (
        "Motor communication error",
        "Check the connection to the motor. Let it cool down if overheated. "
        "Check supply voltage and connector contacts for dirt and damage.",
    ),
    21: (
        "Hall sensor error",
        "Check the wheel magnet and its gap, check the hall sensor wiring, "
        "measure the sensor, try a spare sensor.",
    ),
}

#: Codes seen in the wild without a documented description.
KNOWN_UNDOCUMENTED_ERRORS = frozenset(
    {4, 5, 7, 9, 10, 11, 12, 15, 25, 26, 27, 30, 33, 35, 36, 37,
     41, 42, 43, 45, 46, 47, 48, 71, 81}
)


def error_text(code: int) -> tuple[str, str]:
    """Return ``(description, recommendation)`` for an error code."""
    if code in ERROR_DESCRIPTIONS:
        return ERROR_DESCRIPTIONS[code]
    if code in KNOWN_UNDOCUMENTED_ERRORS:
        return (f"Code {code}: known code, no description available", "-")
    return (f"Code {code}: unknown code", "-")
