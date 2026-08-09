"""Bafang M200 (motor code G210) profile.

What is asserted here as fact, and what is not
----------------------------------------------
Fact: the M200 is a CAN-bus mid-drive, so it sits on the bus as
``DRIVE_UNIT`` (0x02) and speaks the same service protocol as the M400/M500/
M600 family that both upstream projects target.

Not fact, and deliberately not hard-coded: which subset of the command set a
given M200 firmware implements, and the per-byte meaning of the parameter
blocks *on this specific motor*. Neither upstream project lists the M200 as
tested hardware. Bafang reuses block layouts across the family, but has also
moved fields between firmware generations -- the under-voltage field is a
documented example (see :class:`bafang_can.codecs.Parameter1`).

So the workflow this profile encodes is: probe first, back up, then change one
thing at a time and re-read it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..codecs import Parameter1, SpeedParameters
from ..constants import DeviceId

MOTOR_NAME = "Bafang M200 (G210)"
DRIVE_UNIT_ID = DeviceId.DRIVE_UNIT

#: Nominal ratings from Bafang's published M200 specification.
NOMINAL_POWER_W = 250
NOMINAL_VOLTAGE_V = 36
RATED_TORQUE_NM = 80

#: Commands worth probing first on an unknown M200 firmware.
PROBE_ORDER = (
    "HardwareVersion",
    "SoftwareVersion",
    "ModelNumber",
    "SerialNumber",
    "Parameter1",
    "SpeedParameters",
    "ControllerRealtime0",
    "ControllerRealtime1",
    "Parameter0",
    "Parameter2",
    "ErrorCode",
)


@dataclass(frozen=True)
class SafetyLimits:
    """Guard rails applied before a write, independent of what the block allows.

    These are conservative defaults for a 250 W / 36 V class mid-drive, not
    legal limits. Raising the speed limit or the current limit past the values
    your local law and your drivetrain allow is your decision to make; the tool
    refuses silently-dangerous values, not deliberate ones (``--force``).
    """

    max_current_a: int = 18
    max_speed_kmh: float = 32.0
    min_undervoltage_v: float = 29.0
    max_overvoltage_v: float = 45.0
    max_assist_percent: int = 100


DEFAULT_LIMITS = SafetyLimits()


def check_parameter1(block: Parameter1, limits: SafetyLimits = DEFAULT_LIMITS) -> list[str]:
    problems = block.validate()
    if block.current_limit > limits.max_current_a:
        problems.append(
            f"current_limit {block.current_limit} A exceeds the M200 profile "
            f"limit of {limits.max_current_a} A"
        )
    if block.undervoltage / 10 < limits.min_undervoltage_v:
        problems.append(
            f"undervoltage {block.undervoltage / 10:.1f} V is below the profile "
            f"floor of {limits.min_undervoltage_v} V -- this is how packs get "
            "over-discharged"
        )
    if block.overvoltage > limits.max_overvoltage_v:
        problems.append(
            f"overvoltage {block.overvoltage} V exceeds the profile ceiling of "
            f"{limits.max_overvoltage_v} V"
        )
    for index, level in enumerate(block.assist_levels, start=1):
        if level.current_limit > limits.max_assist_percent:
            problems.append(f"assist level {index} current above 100 %")
    return problems


def check_speed(params: SpeedParameters, limits: SafetyLimits = DEFAULT_LIMITS) -> list[str]:
    problems = params.validate()
    if params.speed_limit > limits.max_speed_kmh:
        problems.append(
            f"speed limit {params.speed_limit} km/h exceeds the profile limit "
            f"of {limits.max_speed_kmh} km/h"
        )
    return problems


#: Ordered checklist the CLI prints for `diagnose`.
DIAGNOSTIC_STEPS = (
    "Bus reachable: at least the drive unit answers a hardware-version read.",
    "Identity: hardware/software/model/serial readable and self-consistent.",
    "Stored errors: read and record before clearing anything.",
    "Live electrics: battery voltage under no load, controller temperature.",
    "Torque sensor: zero reading with no foot on the pedal, rising smoothly.",
    "Cadence: counts up when the crank turns, no dropouts.",
    "Speed: wheel circumference matches the real wheel, speed reads sanely.",
)
