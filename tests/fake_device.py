"""A fake Bafang drive unit that speaks the protocol over a queue.

Enough of a device to exercise framing end to end: single frame reads,
multi-frame reads with per-frame acknowledgement, and multi-frame writes.
"""

from __future__ import annotations

import queue
import time

import can

from bafang_can.constants import CanOperation, DeviceId
from bafang_can.frame import BafangId, checksum


class FakeBus:
    """Minimal python-can-like bus wired to a simulated drive unit."""

    def __init__(self, blocks: dict[tuple[int, int], bytes] | None = None) -> None:
        self.blocks = blocks or {}
        self.rx: "queue.Queue[can.Message]" = queue.Queue()
        self.sent: list[can.Message] = []
        self.written: dict[tuple[int, int], bytearray] = {}
        self._pending_write: tuple[int, int] | None = None
        self._write_buffer = bytearray()
        self.device = int(DeviceId.DRIVE_UNIT)

    # -- python-can surface ---------------------------------------------

    def send(self, message: can.Message, timeout: float | None = None) -> None:
        self.sent.append(message)
        self._handle(message)

    def recv(self, timeout: float | None = None):
        try:
            return self.rx.get(timeout=timeout)
        except queue.Empty:
            return None

    def shutdown(self) -> None:  # pragma: no cover - nothing to release
        pass

    # -- device behaviour -------------------------------------------------

    def _emit(self, operation: int, code: int, subcode: int, data: bytes) -> None:
        ident = BafangId(
            source=self.device,
            target=int(DeviceId.TOOL),
            operation=operation,
            code=code,
            subcode=subcode,
        )
        self.rx.put(
            can.Message(
                arbitration_id=ident.encode(),
                data=data,
                is_extended_id=True,
                timestamp=time.time(),
            )
        )

    def _handle(self, message: can.Message) -> None:
        ident = BafangId.decode(message.arbitration_id)
        if ident.target != self.device:
            return
        data = bytes(message.data)
        key = (ident.code, ident.subcode)

        if ident.operation == CanOperation.READ_CMD:
            payload = self.blocks.get(key)
            if payload is None:
                self._emit(CanOperation.ERROR_ACK, ident.code, ident.subcode, b"\x00")
            elif len(payload) <= 8:
                self._emit(CanOperation.NORMAL_ACK, ident.code, ident.subcode, payload)
            else:
                self._emit(
                    CanOperation.MULTIFRAME_START,
                    ident.code,
                    ident.subcode,
                    bytes([len(payload)]),
                )
                rest = payload[8:]
                self._emit(
                    CanOperation.MULTIFRAME, ident.code, 0, payload[:8]
                )
                sequence = 1
                while len(rest) > 8:
                    self._emit(CanOperation.MULTIFRAME, ident.code, sequence, rest[:8])
                    rest = rest[8:]
                    sequence += 1
                self._emit(CanOperation.MULTIFRAME_END, ident.code, sequence, rest)
            return

        if ident.operation == CanOperation.WRITE_CMD:
            if len(data) == 1 and key in self.blocks and len(self.blocks[key]) > 8:
                # length announcement of a multi-frame write
                self._pending_write = key
                self._write_buffer = bytearray()
                return
            self.blocks[key] = data
            self.written[key] = bytearray(data)
            self._emit(CanOperation.NORMAL_ACK, ident.code, ident.subcode, b"\x00")
            return

        if ident.operation == CanOperation.MULTIFRAME_START:
            self._pending_write = key
            self._write_buffer = bytearray(data)
            return

        if ident.operation in (CanOperation.MULTIFRAME, CanOperation.MULTIFRAME_END):
            self._write_buffer.extend(data)
            if ident.operation == CanOperation.MULTIFRAME_END and self._pending_write:
                code, subcode = self._pending_write
                self.blocks[self._pending_write] = bytes(self._write_buffer)
                self.written[self._pending_write] = bytearray(self._write_buffer)
                self._pending_write = None
                self._emit(CanOperation.NORMAL_ACK, code, subcode, b"\x00")
            return


def make_block(fill: int = 0x11) -> bytes:
    body = bytes([fill] * 63)
    return body + bytes([checksum(body)])
