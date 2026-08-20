"""Device profiles: recorded answers from a real bike.

The simulator ships with invented values (see :mod:`bafang_can.simulator`). A
*device profile* replaces them with bytes an actual motor sent, so the
simulator stops being a guess about how an M200 behaves and becomes a replay
of how yours does.

Two ways to make one:

* ``bafang-can capture`` -- interrogate the bike directly. Complete and keyed
  by command, so this is the better source.
* ``bafang-can import-capture ride.log`` -- reconstruct answers from a passive
  sniff, including reassembling multi-frame transfers. Use this when the log
  already exists, or when a session was recorded by something else.

Profiles are plain JSON and are meant to be shared. ``--anonymize`` blanks the
serial and customer numbers first; nothing else in a profile identifies a
bike or a rider.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .commands import READ
from .constants import CanOperation, DeviceId
from .frame import CAN_EFF_MASK, BafangId, BafangMessage
from .quality import CAN_ERROR_STAMP

FORMAT = "bafang-can-device-profile"
VERSION = 1

#: Commands whose payload can identify a specific bike.
IDENTIFYING = ("SerialNumber", "CustomerNumber")


def _key(device: int, code: int, subcode: int) -> str:
    return f"{device}:{code}:{subcode}"


def _parse_key(key: str) -> tuple[int, int, int]:
    device, code, subcode = (int(part) for part in key.split(":"))
    return device, code, subcode


def command_name(code: int, subcode: int) -> str | None:
    for name, command in READ.items():
        if (command.code, command.subcode) == (code, subcode):
            return name
    return None


@dataclass
class DeviceProfile:
    """Recorded answers, keyed by ``device:code:subcode``."""

    responses: dict[str, str] = field(default_factory=dict)
    source: str = ""
    recorded: str = ""
    note: str = ""
    anonymized: bool = False

    # -- building ------------------------------------------------------

    def record(self, device: int, code: int, subcode: int, payload: bytes) -> None:
        self.responses[_key(int(device), code, subcode)] = bytes(payload).hex()

    def blocks(self) -> dict[tuple[int, int, int], bytearray]:
        """The recorded answers in the form the simulator stores them."""
        return {
            _parse_key(key): bytearray(bytes.fromhex(value))
            for key, value in self.responses.items()
        }

    def anonymize(self) -> DeviceProfile:
        """Blank the fields that identify a specific bike."""
        targets = {
            (READ[name].code, READ[name].subcode) for name in IDENTIFYING if name in READ
        }
        for key in list(self.responses):
            _, code, subcode = _parse_key(key)
            if (code, subcode) in targets:
                self.responses[key] = b"REDACTED\x00".hex()
        self.anonymized = True
        return self

    # -- persistence -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "version": VERSION,
            "source": self.source,
            "recorded": self.recorded or datetime.now(timezone.utc).isoformat(),
            "note": self.note,
            "anonymized": self.anonymized,
            "responses": dict(sorted(self.responses.items())),
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> DeviceProfile:
        data = json.loads(Path(path).read_text())
        if data.get("format") != FORMAT:
            raise ValueError(
                f"{path} is not a device profile "
                f"(format is {data.get('format')!r}, expected {FORMAT!r})"
            )
        if data.get("version", 0) > VERSION:
            raise ValueError(
                f"{path} was written by a newer version of this tool "
                f"(profile version {data['version']}, this tool understands {VERSION})"
            )
        return cls(
            responses=dict(data.get("responses", {})),
            source=data.get("source", ""),
            recorded=data.get("recorded", ""),
            note=data.get("note", ""),
            anonymized=bool(data.get("anonymized", False)),
        )

    # -- inspection --------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        by_device: dict[str, list[dict[str, Any]]] = {}
        for key, value in sorted(self.responses.items()):
            device, code, subcode = _parse_key(key)
            try:
                device_name = DeviceId(device).name
            except ValueError:
                device_name = f"0x{device:02x}"
            by_device.setdefault(device_name, []).append(
                {
                    "command": command_name(code, subcode) or "unknown",
                    "code": f"{code:#04x}/{subcode:#04x}",
                    "bytes": len(value) // 2,
                }
            )
        return {
            "source": self.source,
            "recorded": self.recorded,
            "note": self.note,
            "anonymized": self.anonymized,
            "responses": len(self.responses),
            "devices": by_device,
        }


# ---------------------------------------------------------------------------
# Reassembling answers out of a passive capture
# ---------------------------------------------------------------------------


def assemble(messages: Iterable[Any], tool: int = int(DeviceId.TOOL)) -> Iterator[BafangMessage]:
    """Walk raw CAN messages and yield complete Bafang answers.

    Multi-frame transfers are reassembled the same way the live client does
    it: ``MULTIFRAME_START`` carries the total length, the sequence number
    lives in the *subcode* of each continuation frame, and ``MULTIFRAME_END``
    closes the transfer. Frames addressed to anyone are considered, not just
    those aimed at the tool, so a capture of the bike talking to its own
    display is still usable.
    """
    buffers: dict[tuple[int, int], dict[str, Any]] = {}

    for message in messages:
        if getattr(message, "is_error_frame", False):
            # The controller reporting a malformed frame on the wire, not a
            # message from a node. Its payload is error-class detail and
            # decodes as nonsense. quality.LinkQuality is what counts these.
            continue
        if not getattr(message, "is_extended_id", False):
            continue
        if message.arbitration_id == CAN_ERROR_STAMP:
            # The same thing arriving from a candump log, where python-can
            # writes one fixed identifier for every error frame.
            continue
        if message.arbitration_id > CAN_EFF_MASK:
            # A corrupt identifier from a resynchronising slcan reader.
            # BafangId.decode would mask it into a message that looks like it
            # came from a real device, and that answer would then be recorded
            # into a profile and replayed by the simulator as if a bike had
            # sent it.
            continue
        ident = BafangId.decode(message.arbitration_id)
        data = bytes(message.data)
        pair = (ident.source, ident.target)

        if ident.operation == CanOperation.MULTIFRAME_START:
            buffers[pair] = {
                "origin": ident,
                "length": data[0] if data else 0,
                "chunks": {},
                "timestamp": message.timestamp,
            }
            continue

        if ident.operation in (CanOperation.MULTIFRAME, CanOperation.MULTIFRAME_END):
            buffer = buffers.get(pair)
            if buffer is None:
                continue
            buffer["chunks"][ident.subcode] = data
            if ident.operation == CanOperation.MULTIFRAME_END:
                del buffers[pair]
                payload = bytearray()
                for sequence in sorted(buffer["chunks"]):
                    payload.extend(buffer["chunks"][sequence])
                yield BafangMessage(
                    id=buffer["origin"],
                    data=bytes(payload[: buffer["length"]]),
                    timestamp=buffer["timestamp"],
                    multiframe=True,
                )
            continue

        if ident.operation == CanOperation.NORMAL_ACK:
            # A bare 0x00 is an acknowledgement, not an answer with data.
            if data in (b"", b"\x00") and ident.target != tool:
                continue
            if data == b"\x00":
                continue
            yield BafangMessage(id=ident, data=data, timestamp=message.timestamp)


def profile_from_log(path: str | Path, tool: int = int(DeviceId.TOOL)) -> DeviceProfile:
    """Build a device profile from a recorded capture.

    Later answers win: a log covering a whole session ends up holding the most
    recent value each device reported.
    """
    import can

    profile = DeviceProfile(source=f"log: {Path(path).name}")
    with can.LogReader(str(path)) as reader:
        for message in assemble(reader, tool=tool):
            profile.record(message.id.source, message.id.code, message.id.subcode, message.data)
    return profile
