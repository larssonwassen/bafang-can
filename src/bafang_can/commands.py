"""Command tables.

Union of the command lists in both vendored projects. ``bafang_canable_pro``
knows about several codes that ``OpenBafangTool`` does not (Parameter0,
ControllerState, SensorRealtime, DisplayRealtime, the calibration commands,
0x60/0x17 and 0x60/0x18, and the firmware update handshake), so this table is
mostly the canable_pro one with OpenBafangTool's device applicability.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import CanOperation, DeviceId


@dataclass(frozen=True)
class Command:
    name: str
    code: int
    subcode: int
    devices: tuple[DeviceId, ...]
    #: Payload length in bytes, when it is fixed and known.
    length: int | None = None
    operation: int | None = None

    def applies_to(self, device: DeviceId) -> bool:
        return device in self.devices


_ALL = (
    DeviceId.TORQUE_SENSOR,
    DeviceId.DRIVE_UNIT,
    DeviceId.DISPLAY,
    DeviceId.BATTERY,
)
_DU = (DeviceId.DRIVE_UNIT,)
_DISP = (DeviceId.DISPLAY,)
_BAT = (DeviceId.BATTERY,)
_SENS = (DeviceId.TORQUE_SENSOR,)


READ = {
    c.name: c
    for c in (
        # --- identification, all devices -------------------------------
        Command("HardwareVersion", 0x60, 0x00, _ALL),
        Command("SoftwareVersion", 0x60, 0x01, _ALL),
        Command("ModelNumber", 0x60, 0x02, _ALL),
        Command("SerialNumber", 0x60, 0x03, _ALL),
        Command("CustomerNumber", 0x60, 0x04, (DeviceId.TORQUE_SENSOR, DeviceId.DISPLAY)),
        Command("Manufacturer", 0x60, 0x05, (DeviceId.DRIVE_UNIT, DeviceId.DISPLAY)),
        Command("ErrorCode", 0x60, 0x07, (DeviceId.DRIVE_UNIT, DeviceId.DISPLAY)),
        Command("BootloaderVersion", 0x60, 0x08, _DISP),
        # --- drive unit parameter blocks (64 byte each) ----------------
        Command("Parameter0", 0x60, 0x10, _DU, length=64),
        Command("Parameter1", 0x60, 0x11, _DU, length=64),
        Command("Parameter2", 0x60, 0x12, _DU, length=64),
        Command("Parameter6017", 0x60, 0x17, _DU),
        Command("Parameter6018", 0x60, 0x18, _DU),
        Command("SpeedParameters", 0x32, 0x03, _DU, length=6),
        # --- drive unit live data --------------------------------------
        Command("ControllerRealtime0", 0x32, 0x00, _DU, length=8),
        Command("ControllerRealtime1", 0x32, 0x01, _DU, length=8),
        Command("ControllerState", 0x12, 0x00, _DU),
        Command("ControllerStartupAngle", 0x62, 0xD9, _DU),
        # --- torque sensor ---------------------------------------------
        Command("SensorRealtime", 0x31, 0x00, _SENS, length=3),
        # --- display ----------------------------------------------------
        Command("DisplayRealtime", 0x63, 0x00, _DISP),
        Command("DisplayDataBlock1", 0x63, 0x01, _DISP),
        Command("DisplayDataBlock2", 0x63, 0x02, _DISP),
        Command("DisplayAutoShutdownTime", 0x63, 0x03, _DISP),
        # --- battery ----------------------------------------------------
        Command("BatteryCapacity", 0x34, 0x00, _BAT),
        Command("BatteryState", 0x34, 0x01, _BAT),
        Command("BatteryDesign", 0x64, 0x00, _BAT),
        Command("BatteryChargingInfo", 0x64, 0x01, _BAT),
        Command("CellsVoltage0", 0x64, 0x02, _BAT),
        Command("CellsVoltage1", 0x64, 0x03, _BAT),
        Command("CellsVoltage2", 0x64, 0x04, _BAT),
        Command("CellsVoltage3", 0x64, 0x05, _BAT),
        # --- firmware update handshake ----------------------------------
        Command("FwUpdateReadyCheckOld", 0x20, 0x00, _DU),
        Command("FwUpdateReadyCheck", 0x40, 0x00, _DU),
    )
}


WRITE = {
    c.name: c
    for c in (
        Command("SerialNumber", 0x60, 0x03, (DeviceId.DISPLAY, DeviceId.DRIVE_UNIT, DeviceId.TORQUE_SENSOR)),
        Command("CustomerNumber", 0x60, 0x04, (DeviceId.DISPLAY, DeviceId.DRIVE_UNIT, DeviceId.TORQUE_SENSOR)),
        Command("Manufacturer", 0x60, 0x05, (DeviceId.DISPLAY, DeviceId.DRIVE_UNIT, DeviceId.TORQUE_SENSOR)),
        Command("ClearErrorCodes", 0x60, 0x07, (DeviceId.DISPLAY, DeviceId.DRIVE_UNIT)),
        Command("Parameter0", 0x60, 0x10, _DU, length=64),
        Command("Parameter1", 0x60, 0x11, _DU, length=64),
        Command("Parameter2", 0x60, 0x12, _DU, length=64),
        Command("SpeedParameters", 0x32, 0x03, _DU, length=6),
        Command("CalibratePositionSensor", 0x62, 0x00, _DU),
        Command("CalibrateTorqueSensor", 0x61, 0x01, _DU),
        Command("DisplayTotalMileage", 0x62, 0x01, _DISP),
        Command("DisplayTime", 0x62, 0x02, _DISP),
        Command("DisplaySingleMileage", 0x62, 0x03, _DISP),
        Command("CleanServiceMileage", 0x63, 0x02, _DISP),
        Command("SetServiceThreshold", 0x63, 0x0B, _DISP),
        Command(
            "FwUpdateSendFirstPackageOld", 0x20, 0x01, _DU,
            operation=CanOperation.MULTIFRAME_START,
        ),
        Command(
            "FwUpdateSendFirstPackage", 0x40, 0x01, _DU,
            operation=CanOperation.MULTIFRAME_START,
        ),
    )
}


BROADCAST = {
    "FwUpdateInitAndEnd": Command(
        "FwUpdateInitAndEnd", 0x30, 0x05, (DeviceId.BROADCAST,),
        operation=CanOperation.MULTIFRAME_WARNING,
    ),
}
