"""High-level access to a Bafang CAN system.

Everything here is built on :class:`~bafang_can.protocol.BafangClient` and is
deliberately read-first: the diagnose/dump paths never write, and every write
path takes the block that was just read as its base.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from . import codecs
from .commands import READ, WRITE, Command
from .constants import DeviceId, error_text
from .frame import BafangMessage, string_from_bytes, string_to_bytes
from .protocol import BafangClient, BafangError

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from . import capture as capture_module

log = logging.getLogger(__name__)

FORMAT_VERSION = 1

INFO_FIELDS = (
    "HardwareVersion",
    "SoftwareVersion",
    "ModelNumber",
    "SerialNumber",
    "CustomerNumber",
    "Manufacturer",
    "BootloaderVersion",
)


@dataclass
class DeviceInfo:
    device: DeviceId
    present: bool
    fields: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device.name,
            "present": self.present,
            **self.fields,
        }


class BafangSystem:
    def __init__(self, client: BafangClient) -> None:
        self.client = client

    # -- discovery ------------------------------------------------------

    def scan(self, devices: Iterable[DeviceId] | None = None) -> dict[DeviceId, bool]:
        devices = devices or (
            DeviceId.DRIVE_UNIT,
            DeviceId.DISPLAY,
            DeviceId.TORQUE_SENSOR,
            DeviceId.BATTERY,
        )
        return {device: self.client.ping(device) for device in devices}

    def info(self, device: DeviceId) -> DeviceInfo:
        fields: dict[str, str] = {}
        present = False
        for name in INFO_FIELDS:
            command = READ[name]
            if not command.applies_to(device):
                continue
            try:
                message = self.client.read(device, command, retries=0)
            except BafangError:
                continue
            present = True
            fields[name] = string_from_bytes(message.data)
        return DeviceInfo(device=device, present=present, fields=fields)

    def capabilities(self, device: DeviceId = DeviceId.DRIVE_UNIT) -> dict[str, bool]:
        """Probe which read commands this particular unit answers.

        Bafang firmware varies a lot between motor generations; this is the
        honest way to find out what a given M200/G210 supports rather than
        assuming the M500-era command set is complete.
        """
        result: dict[str, bool] = {}
        for name, command in READ.items():
            if not command.applies_to(device) or name.startswith("FwUpdate"):
                continue
            try:
                self.client.read(device, command, timeout=0.6, retries=0)
                result[name] = True
            except BafangError:
                result[name] = False
        return result

    # -- configuration blocks -------------------------------------------

    def read_block(self, name: str) -> codecs.Block:
        cls = codecs.CONFIG_BLOCKS[name]
        message = self.client.read(DeviceId.DRIVE_UNIT, READ[name])
        block = cls.decode(message.data)
        if not block.checksum_ok:
            log.warning("%s checksum mismatch -- treat the values with suspicion", name)
        return block

    def write_block(self, name: str, block: codecs.Block) -> BafangMessage:
        payload = block.encode()
        return self.client.write_long(DeviceId.DRIVE_UNIT, WRITE[name], payload)

    def read_speed_parameters(self) -> codecs.SpeedParameters:
        message = self.client.read(DeviceId.DRIVE_UNIT, READ["SpeedParameters"])
        return codecs.SpeedParameters.decode(message.data)

    def write_speed_parameters(
        self, params: codecs.SpeedParameters
    ) -> BafangMessage | None:
        return self.client.write(
            DeviceId.DRIVE_UNIT, WRITE["SpeedParameters"], params.encode()
        )

    # -- live data -------------------------------------------------------

    def realtime(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            message = self.client.read(
                DeviceId.DRIVE_UNIT, READ["ControllerRealtime0"], retries=0
            )
            out["controller0"] = codecs.ControllerRealtime0.decode(message.data)
        except (BafangError, codecs.DecodeError) as exc:
            out["controller0_error"] = str(exc)
        try:
            message = self.client.read(
                DeviceId.DRIVE_UNIT, READ["ControllerRealtime1"], retries=0
            )
            out["controller1"] = codecs.ControllerRealtime1.decode(message.data)
        except (BafangError, codecs.DecodeError) as exc:
            out["controller1_error"] = str(exc)
        try:
            message = self.client.read(
                DeviceId.TORQUE_SENSOR, READ["SensorRealtime"], retries=0
            )
            out["sensor"] = codecs.SensorRealtime.decode(message.data)
        except (BafangError, codecs.DecodeError):
            pass  # many systems have the sensor inside the drive unit
        try:
            message = self.client.read(
                DeviceId.BATTERY, READ["BatteryState"], retries=0
            )
            out["battery"] = codecs.BatteryState.decode(message.data)
        except (BafangError, codecs.DecodeError):
            pass  # non-CAN (dumb) batteries do not answer
        return out

    # -- diagnostics ------------------------------------------------------

    def errors(self, device: DeviceId = DeviceId.DRIVE_UNIT) -> list[tuple[int, str, str]]:
        message = self.client.read(device, READ["ErrorCode"])
        codes = codecs.decode_error_codes(message.data)
        return [(code, *error_text(code)) for code in codes]

    def clear_errors(self, device: DeviceId = DeviceId.DRIVE_UNIT) -> BafangMessage | None:
        return self.client.write_short(device, WRITE["ClearErrorCodes"], b"\x00")

    # -- calibration -------------------------------------------------------

    def calibrate_torque_sensor(self) -> BafangMessage | None:
        """Zero the torque sensor.

        The bike must be upright, unloaded, with no foot on the pedals and the
        cranks stationary. The drive unit samples its zero point when this
        command arrives; calibrating under load bakes that load into every
        future assist calculation.
        """
        return self.client.write_short(
            DeviceId.DRIVE_UNIT, WRITE["CalibrateTorqueSensor"], b"\x00", timeout=5.0
        )

    def calibrate_position_sensor(self) -> BafangMessage | None:
        """Re-learn the rotor position sensor offset.

        The rear wheel must be off the ground and free to spin: the drive unit
        turns the motor during this procedure.
        """
        return self.client.write_short(
            DeviceId.DRIVE_UNIT, WRITE["CalibratePositionSensor"], b"\x00", timeout=10.0
        )

    def startup_angle(self) -> int | None:
        try:
            message = self.client.read(
                DeviceId.DRIVE_UNIT, READ["ControllerStartupAngle"], retries=0
            )
        except BafangError:
            return None
        if len(message.data) < 2:
            return None
        return message.data[0] | (message.data[1] << 8)

    # -- identity writes ---------------------------------------------------

    def write_string(
        self, device: DeviceId, name: str, value: str
    ) -> BafangMessage | None:
        command: Command = WRITE[name]
        if not command.applies_to(device):
            raise ValueError(f"{name} cannot be written to {device.name}")
        return self.client.write(device, command, bytes(string_to_bytes(value)))

    # -- recording a device profile ------------------------------------------

    def capture(
        self,
        devices: Iterable[DeviceId] | None = None,
        timeout: float = 0.8,
    ) -> capture_module.DeviceProfile:
        """Record every answer this bike gives, as raw bytes.

        Unlike :meth:`dump`, nothing is interpreted: the payloads are stored
        exactly as they arrived. That makes the result usable as a replay
        source for the simulator, and as evidence when a field turns out to
        mean something different on this motor than the block layouts assume.
        """
        from . import capture as capture_module

        profile = capture_module.DeviceProfile(source="live capture")
        devices = devices or (
            DeviceId.DRIVE_UNIT,
            DeviceId.DISPLAY,
            DeviceId.TORQUE_SENSOR,
            DeviceId.BATTERY,
        )
        for device in devices:
            for name, command in READ.items():
                if not command.applies_to(device) or name.startswith("FwUpdate"):
                    continue
                try:
                    message = self.client.read(
                        device, command, timeout=timeout, retries=0
                    )
                except BafangError:
                    continue
                profile.record(
                    int(device), command.code, command.subcode, message.data
                )
        return profile

    # -- backup / restore ---------------------------------------------------

    def dump(self) -> dict[str, Any]:
        """Read everything that is safe to read, as a JSON-ready dict."""
        present = self.scan()
        data: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "devices": {},
            "drive_unit": {},
        }
        for device, found in present.items():
            if not found:
                data["devices"][device.name] = {"present": False}
                continue
            data["devices"][device.name] = self.info(device).to_dict()

        if not present.get(DeviceId.DRIVE_UNIT):
            return data

        for name in codecs.CONFIG_BLOCKS:
            try:
                block = self.read_block(name)
            except (BafangError, codecs.DecodeError) as exc:
                data["drive_unit"][name] = {"error": str(exc)}
                continue
            entry = block.to_dict()
            entry["checksum_ok"] = block.checksum_ok
            data["drive_unit"][name] = entry
        try:
            data["drive_unit"]["SpeedParameters"] = self.read_speed_parameters().to_dict()
        except (BafangError, codecs.DecodeError) as exc:
            data["drive_unit"]["SpeedParameters"] = {"error": str(exc)}
        try:
            data["drive_unit"]["errors"] = [
                {"code": code, "description": desc, "recommendation": rec}
                for code, desc, rec in self.errors()
            ]
        except BafangError:
            pass
        return data

    def save(self, path: str) -> dict[str, Any]:
        data = self.dump()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, default=str)
        return data

    def apply(
        self, data: dict[str, Any], blocks: Iterable[str] | None = None, dry_run: bool = True
    ) -> list[str]:
        """Write blocks from a dump back to the drive unit.

        Each block is written as raw bytes exactly as it was recorded, after
        the checksum has been verified. Returns a log of what was (or would
        have been) written.
        """
        report: list[str] = []
        drive_unit = data.get("drive_unit", {})
        names = list(blocks) if blocks else list(codecs.CONFIG_BLOCKS)
        for name in names:
            entry = drive_unit.get(name)
            if not entry or "raw" not in entry:
                report.append(f"{name}: not present in the backup, skipped")
                continue
            payload = bytes.fromhex(entry["raw"])
            block = codecs.CONFIG_BLOCKS[name].decode(payload)
            if not block.checksum_ok:
                report.append(f"{name}: checksum invalid in the backup, refused")
                continue
            if dry_run:
                report.append(f"{name}: would write {len(payload)} bytes")
                continue
            self.client.write_long(DeviceId.DRIVE_UNIT, WRITE[name], payload)
            report.append(f"{name}: written")
            time.sleep(0.2)
        if "SpeedParameters" in names or not blocks:
            entry = drive_unit.get("SpeedParameters")
            if entry and "raw" in entry:
                payload = bytes.fromhex(entry["raw"])
                if dry_run:
                    report.append(f"SpeedParameters: would write {len(payload)} bytes")
                else:
                    self.client.write(
                        DeviceId.DRIVE_UNIT, WRITE["SpeedParameters"], payload
                    )
                    report.append("SpeedParameters: written")
        return report
