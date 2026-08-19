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


#: Error codes, merged from both vendored projects.
#:
#: ``OpenBafangTool`` carries three (8, 14, 21) in ``locales/en.json``, with
#: long repair notes. ``bafang_canable_pro`` carries thirty-three in
#: ``ui/js/shared.js``, each with a one-line description and a recommendation.
#: This tool used to expose only OpenBafangTool's three and report the other
#: twenty-four as "no description available" -- including code 7, which is the
#: fault the bike in ``docs/m200.md`` was displaying throughout that entire
#: investigation. The description was vendored in this repository the whole
#: time.
#:
#: Where the two sources overlap they are reconciled explicitly below rather
#: than by picking one, because on code 14 they genuinely disagree.

_CANABLE_ERRORS: dict[int, tuple[str, str]] = {
    1: (
        "Throttle fault",
        "Inspect the wire from the throttle to the controller for cuts or kinks",
    ),
    2: (
        "Brake sensor malfunction",
        "Look for pinched, cut, or damaged cables leading from the brake levers to the main wiring harness",
    ),
    3: (
        "Brake Fault",
        "Brake sensor is active or stuck; check brake levers",
    ),
    4: (
        "Throttle not in correct position",
        "Check and adjust throttle position, inspect wiring, replace throttle if needed",
    ),
    7: (
        "Over voltage protection",
        "Check battery and charger compatibility, inspect battery, discharge if overcharged",
    ),
    8: (
        "Hall sensor error",
        "Check hall sensor connections, inspect for damage, replace if necessary",
    ),
    9: (
        "Motor phase winding fault",
        "Check motor connections, inspect for damage, test with a different controller",
    ),
    10: (
        "Motor overtemperature",
        "Allow motor to cool down, reduce load, ensure proper ventilation",
    ),
    11: (
        "Motor temperature sensor fault",
        "Check sensor connection, inspect for damage, replace if necessary",
    ),
    12: (
        "Motor overcurrent",
        "Reduce load, check wiring, inspect motor and controller",
    ),
    13: (
        "Battery temperature sensor fault",
        "Check sensor connection, inspect for damage, replace if necessary",
    ),
    14: (
        "Controller overtemperature",
        "Allow controller to cool down, reduce load, ensure proper ventilation",
    ),
    15: (
        "Controller temperature sensor fault",
        "Check sensor connection, inspect for damage, replace if necessary",
    ),
    21: (
        "Speed sensor error",
        "Check sensor connection, inspect for damage, realign magnets",
    ),
    25: (
        "Torque signal fault",
        "Check sensor connection, inspect for damage, replace if necessary",
    ),
    26: (
        "Torque sensor speed signal fault",
        "Check sensor connection, inspect for damage, replace if necessary",
    ),
    27: (
        "Controller overcurrent",
        "Reduce load, check wiring, inspect motor and controller",
    ),
    30: (
        "Communication failed",
        "Check connections, update firmware, replace faulty components",
    ),
    33: (
        "Brake detection circuit fault",
        "Check sensor connection, inspect wiring, replace sensor if needed",
    ),
    35: (
        "15V detection circuit error",
        "Check power supply and connections, replace damaged components",
    ),
    36: (
        "Keypad detection circuit error",
        "Check keypad connection, inspect wiring, replace keypad if needed",
    ),
    37: (
        "WDT circuit fault Controller",
        "Consult a professional for diagnosis and repair",
    ),
    41: (
        "Total voltage from the battery is too high.",
        "Check battery",
    ),
    42: (
        "Total voltage from the battery is too low.",
        "Check battery",
    ),
    43: (
        "Total power from the battery cells is too high.",
        "Check battery",
    ),
    45: (
        "Temperature from the battery is too high.",
        "Check battery",
    ),
    46: (
        "The temperature of the battery is too low.",
        "Check battery",
    ),
    47: (
        "SOC of the battery is too high.",
        "Check battery",
    ),
    48: (
        "SOC of the battery is too low.",
        "Check battery",
    ),
    61: (
        "Switching detection defect.",
        "-",
    ),
    62: (
        "Electronic derailleur cannot release.",
        "-",
    ),
    71: (
        "Electronic lock is jammed.",
        "-",
    ),
    81: (
        "Bluetooth module has an error.",
        "-",
    ),
}


#: OpenBafangTool's three, which carry more usable repair detail.
_OPENBAFANG_ERRORS: dict[int, tuple[str, str]] = {
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

#: Codes where the two upstream projects mean different things.
#:
#: Codes documented in the vehicle's own manual (eGoing/Crescent Center,
#: 2022-11-17, the manual for the exact bike this project is built around).
#:
#: A third source, and the only one that is authoritative for *this* vehicle
#: rather than for the Bafang family in general. Where it is more specific than
#: the upstream tables it wins -- the upstream projects describe code 21 as a
#: generic "hall sensor error", while the manual names the speed sensor and
#: gives the magnet gap to check.
#:
#: Descriptions are translated from the Swedish and condensed; they are short
#: factual repair steps, not the manual's text reproduced.
_MANUAL_ERRORS: dict[int, tuple[str, str]] = {
    7: (
        "Over voltage protection",
        "Remove the battery and refit it. If it persists, see a dealer.",
    ),
    10: (
        "Motor temperature has reached its maximum",
        "Switch the system off and let the motor cool.",
    ),
    14: (
        "Controller box temperature has reached its maximum",
        "Switch the system off and let the controller cool.",
    ),
    21: (
        "Speed sensor fault",
        "Restart the system. Check the spoke magnet is 10-20 mm from the "
        "sensor and correctly aligned, and that the sensor cable is seated.",
    ),
    25: (
        "Torque sensor fault",
        "Check every connector is properly seated.",
    ),
    26: (
        "Torque sensor is not reaching the speed sensor",
        "Check the speed sensor cable is seated and the sensor undamaged.",
    ),
    30: (
        "Communication fault",
        "Check that all cables are properly connected.",
    ),
    33: (
        "Brake sensor fault",
        "Check that all cables are properly connected.",
    ),
}

#: Codes the two upstream projects describe incompatibly, and the manual does
#: not settle.
#:
#: Code 14 used to be here: OpenBafangTool calls it a motor communication
#: error, bafang_canable_pro calls it controller overtemperature, and those
#: lead to opposite repairs. The vehicle manual names it as the controller box
#: reaching its temperature limit, which agrees with bafang_canable_pro, so on
#: this bike the question is answered and both readings are no longer reported.
#: Nothing currently remains unresolved -- kept because the next code that
#: disagrees will need it.
CONFLICTING_ERRORS: frozenset[int] = frozenset()


def _merge_error_tables() -> dict[int, tuple[str, str]]:
    """Merge the three sources, least to most authoritative.

    ``bafang_canable_pro`` has the broadest table, ``OpenBafangTool`` the more
    detailed entries where it has any, and the vehicle's own manual is
    definitive for the bike this project targets.
    """
    merged = dict(_CANABLE_ERRORS)
    for code, entry in _OPENBAFANG_ERRORS.items():
        if code in CONFLICTING_ERRORS:
            continue  # handled by error_text, which reports both readings
        merged[code] = entry  # the more detailed of the two
    merged.update(_MANUAL_ERRORS)
    return merged


ERROR_DESCRIPTIONS: dict[int, tuple[str, str]] = _merge_error_tables()

#: Codes seen in the wild that still have no description in either project.
KNOWN_UNDOCUMENTED_ERRORS = frozenset({5})


def error_text(code: int) -> tuple[str, str]:
    """Return ``(description, recommendation)`` for an error code."""
    if code in CONFLICTING_ERRORS:
        theirs = _CANABLE_ERRORS[code]
        ours = _OPENBAFANG_ERRORS[code]
        return (
            f"{ours[0]} -- or {theirs[0].lower()}; the two upstream projects "
            "disagree about this code",
            f"{ours[1]} If that is not it: {theirs[1]}",
        )
    if code in ERROR_DESCRIPTIONS:
        return ERROR_DESCRIPTIONS[code]
    if code in KNOWN_UNDOCUMENTED_ERRORS:
        return (f"Code {code}: known code, no description available", "-")
    return (f"Code {code}: unknown code", "-")
