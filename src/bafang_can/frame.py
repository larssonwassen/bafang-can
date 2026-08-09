"""Bafang CAN identifier encoding/decoding.

The Bafang service protocol packs five fields into the 29-bit extended
identifier::

    bit 28..24  source device id   (5 bit, DeviceId)
    bit 23..19  target device id   (5 bit, DeviceId)
    bit 18..16  operation code     (3 bit, CanOperation)
    bit 15..8   command code
    bit  7..0   command sub code

Both reference projects build the same identifier; ``bafang_canable_pro``
expresses it as a 4-byte array where byte 0 is ``0x80 | source`` because it
writes the raw SocketCAN/gs_usb word including the EFF flag. Here the EFF flag
is left to the transport layer, so only the 29 bits are handled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .constants import CanOperation, DeviceId

CAN_EFF_MASK = 0x1FFF_FFFF


def encode_id(
    source: int, target: int, operation: int, code: int, subcode: int
) -> int:
    """Build a 29-bit Bafang identifier."""
    return (
        ((source & 0x1F) << 24)
        | ((target & 0x1F) << 19)
        | ((operation & 0x07) << 16)
        | ((code & 0xFF) << 8)
        | (subcode & 0xFF)
    )


@dataclass(frozen=True)
class BafangId:
    source: int
    target: int
    operation: int
    code: int
    subcode: int

    @classmethod
    def decode(cls, can_id: int) -> "BafangId":
        can_id &= CAN_EFF_MASK
        return cls(
            source=(can_id >> 24) & 0x1F,
            target=(can_id >> 19) & 0x1F,
            operation=(can_id >> 16) & 0x07,
            code=(can_id >> 8) & 0xFF,
            subcode=can_id & 0xFF,
        )

    def encode(self) -> int:
        return encode_id(
            self.source, self.target, self.operation, self.code, self.subcode
        )

    def __str__(self) -> str:  # pragma: no cover - display only
        def name(value: int) -> str:
            try:
                return DeviceId(value).name
            except ValueError:
                return f"0x{value:02x}"

        try:
            op = CanOperation(self.operation).name
        except ValueError:  # pragma: no cover - 3 bits are always valid
            op = str(self.operation)
        return (
            f"{name(self.source)}->{name(self.target)} {op} "
            f"{self.code:02x}/{self.subcode:02x}"
        )


@dataclass
class BafangMessage:
    """A complete (possibly reassembled) Bafang message."""

    id: BafangId
    data: bytes = b""
    timestamp: float = 0.0
    #: True when the payload came from a multi-frame transfer.
    multiframe: bool = False
    raw_frames: list[bytes] = field(default_factory=list, repr=False)

    @property
    def is_error_ack(self) -> bool:
        return self.id.operation == CanOperation.ERROR_ACK

    @property
    def is_ack(self) -> bool:
        return self.id.operation == CanOperation.NORMAL_ACK


def checksum(data: Sequence[int]) -> int:
    """Bafang block checksum: the low byte of the sum of all bytes."""
    return sum(data) & 0xFF


def int_to_bytes_le(value: int, length: int) -> list[int]:
    """Little-endian byte list, as used inside Bafang payloads."""
    return [(value >> (8 * i)) & 0xFF for i in range(length)]


def string_from_bytes(data: Sequence[int]) -> str:
    """ASCII string, truncated at the first NUL, as Bafang stores strings."""
    out = []
    for byte in data:
        if byte in (0x00, 0xFF):
            break
        out.append(chr(byte))
    return "".join(out).strip()


def string_to_bytes(value: str) -> list[int]:
    return [*value.encode("ascii"), 0]
