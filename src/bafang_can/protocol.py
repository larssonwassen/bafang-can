"""Request/response layer: framing, multi-frame transfers, acknowledgements.

Framing rules (identical in both upstream projects, verified against
``vendor/bafang_canable_pro/canbus.js`` and ``bafang-serializer.js``):

Read
    Tool sends ``READ_CMD`` with the command code/subcode and an empty (or
    short) payload. A short answer arrives as a single ``NORMAL_ACK`` frame
    carrying the data. A long answer arrives as ``MULTIFRAME_START`` (payload
    byte 0 = total length), then ``MULTIFRAME`` frames whose *subcode* is the
    sequence number, then ``MULTIFRAME_END``. The tool must acknowledge every
    received frame of the transfer with ``NORMAL_ACK`` carrying ``0x00`` and
    the *original* command code/subcode.

Write
    Payloads of 8 bytes or less: a single ``WRITE_CMD`` frame. Longer: a
    ``WRITE_CMD`` frame whose single payload byte is the total length, then
    ``MULTIFRAME_START`` with the first 8 bytes, then ``MULTIFRAME`` frames
    (code 0, subcode = sequence), then ``MULTIFRAME_END`` with the remainder.
    The device answers the whole transfer with one ``NORMAL_ACK``.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .commands import Command
from .constants import CanOperation, DeviceId
from .frame import BafangId, BafangMessage

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 2.0
#: Gap between frames of a multi-frame write. The drive unit drops frames that
#: arrive faster than this.
INTERFRAME_DELAY = 0.02
MULTIFRAME_TIMEOUT = 3.0


class BafangError(RuntimeError):
    pass


class TimeoutError_(BafangError):
    """No answer from the device within the timeout."""


class DeviceError(BafangError):
    """The device answered with ERROR_ACK."""


@dataclass
class _Pending:
    queue: "queue.Queue[BafangMessage]"


@dataclass
class _MultiframeBuffer:
    origin: BafangId
    expected_length: int
    chunks: dict[int, bytes]
    started: float

    def assembled(self) -> bytes:
        out = bytearray()
        for seq in sorted(self.chunks):
            out.extend(self.chunks[seq])
        return bytes(out[: self.expected_length])


class BafangClient:
    """Talks the Bafang service protocol over a python-can bus."""

    def __init__(
        self,
        bus,
        source: int = DeviceId.TOOL,
        timeout: float = DEFAULT_TIMEOUT,
        send_acks: bool = True,
    ) -> None:
        self.bus = bus
        self.source = int(source)
        self.timeout = timeout
        self.send_acks = send_acks

        self._pending: dict[tuple[int, int, int], _Pending] = {}
        self._buffers: dict[tuple[int, int, int], _MultiframeBuffer] = {}
        self._lock = threading.Lock()
        self._listeners: list[Callable[[BafangMessage], None]] = []
        self._stop = threading.Event()
        self._rx_thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------

    def start(self) -> "BafangClient":
        if self._rx_thread is not None:
            return self
        self._stop.clear()
        self._rx_thread = threading.Thread(
            target=self._rx_loop, name="bafang-rx", daemon=True
        )
        self._rx_thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._rx_thread is not None:
            self._rx_thread.join(timeout=2.0)
            self._rx_thread = None

    def close(self) -> None:
        self.stop()
        try:
            self.bus.shutdown()
        except Exception:  # pragma: no cover - driver dependent
            log.debug("bus shutdown raised", exc_info=True)

    def __enter__(self) -> "BafangClient":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.close()

    def add_listener(self, callback: Callable[[BafangMessage], None]) -> None:
        """Register a callback for every message, including foreign traffic."""
        self._listeners.append(callback)

    # -- low level ------------------------------------------------------

    def _send(self, bafang_id: BafangId, data: bytes) -> None:
        import can  # imported lazily so the codecs work without python-can

        message = can.Message(
            arbitration_id=bafang_id.encode(),
            data=bytes(data),
            is_extended_id=True,
        )
        log.debug("TX %s %s", bafang_id, data.hex())
        self.bus.send(message)

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                message = self.bus.recv(timeout=0.2)
            except Exception:  # pragma: no cover - driver dependent
                if self._stop.is_set():
                    return
                log.exception("CAN receive failed")
                time.sleep(0.1)
                continue
            if message is None:
                self._expire_buffers()
                continue
            if not message.is_extended_id or message.is_error_frame:
                continue
            self._handle(message)

    def _handle(self, message) -> None:
        ident = BafangId.decode(message.arbitration_id)
        data = bytes(message.data)
        if ident.source == self.source:
            return  # our own echo
        log.debug("RX %s %s", ident, data.hex())

        if ident.target != self.source:
            # Traffic between other nodes. Useful for sniffing, but never
            # acknowledged or matched against our requests.
            self._emit(BafangMessage(id=ident, data=data, timestamp=message.timestamp))
            return

        op = ident.operation
        if op == CanOperation.MULTIFRAME_START:
            key = (ident.source, ident.code, ident.subcode)
            with self._lock:
                self._buffers[key] = _MultiframeBuffer(
                    origin=ident,
                    expected_length=data[0] if data else 0,
                    chunks={},
                    started=time.monotonic(),
                )
            self._ack(ident)
            return

        if op in (CanOperation.MULTIFRAME, CanOperation.MULTIFRAME_END):
            with self._lock:
                key = next(
                    (k for k in self._buffers if k[0] == ident.source), None
                )
                buffer = self._buffers.get(key) if key else None
                if buffer is None:
                    log.debug("multi-frame part without an open transfer: %s", ident)
                    return
                buffer.chunks[ident.subcode] = data
                buffer.started = time.monotonic()
                finished = op == CanOperation.MULTIFRAME_END
                if finished:
                    del self._buffers[key]
            self._ack(buffer.origin)
            if finished:
                assembled = buffer.assembled()
                self._deliver(
                    BafangMessage(
                        id=buffer.origin,
                        data=assembled,
                        timestamp=message.timestamp,
                        multiframe=True,
                    )
                )
            return

        self._deliver(BafangMessage(id=ident, data=data, timestamp=message.timestamp))

    def _ack(self, origin: BafangId) -> None:
        if not self.send_acks:
            return
        ack_id = BafangId(
            source=self.source,
            target=origin.source,
            operation=CanOperation.NORMAL_ACK,
            code=origin.code,
            subcode=origin.subcode,
        )
        try:
            self._send(ack_id, b"\x00")
        except Exception:  # pragma: no cover - driver dependent
            log.exception("failed to acknowledge %s", origin)

    def _expire_buffers(self) -> None:
        now = time.monotonic()
        with self._lock:
            stale = [
                key
                for key, buffer in self._buffers.items()
                if now - buffer.started > MULTIFRAME_TIMEOUT
            ]
            for key in stale:
                log.warning("multi-frame transfer %s timed out, discarding", key)
                del self._buffers[key]

    def _deliver(self, message: BafangMessage) -> None:
        key = (message.id.source, message.id.code, message.id.subcode)
        with self._lock:
            pending = self._pending.get(key)
        if pending is not None:
            pending.queue.put(message)
        self._emit(message)

    def _emit(self, message: BafangMessage) -> None:
        for listener in list(self._listeners):
            try:
                listener(message)
            except Exception:  # pragma: no cover - user callback
                log.exception("listener failed")

    def _register(self, key: tuple[int, int, int]) -> _Pending:
        pending = _Pending(queue=queue.Queue())
        with self._lock:
            self._pending[key] = pending
        return pending

    def _unregister(self, key: tuple[int, int, int]) -> None:
        with self._lock:
            self._pending.pop(key, None)

    def _wait(
        self, pending: _Pending, timeout: float, what: str
    ) -> BafangMessage:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError_(f"no answer to {what} within {timeout:.1f} s")
            try:
                message = pending.queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if message.is_error_ack:
                raise DeviceError(f"device rejected {what} (ERROR_ACK)")
            return message

    # -- public API -----------------------------------------------------

    def read(
        self,
        target: int,
        command: Command,
        data: bytes = b"",
        timeout: float | None = None,
        retries: int = 1,
    ) -> BafangMessage:
        """Send a read request and return the (possibly reassembled) answer."""
        timeout = self.timeout if timeout is None else timeout
        what = f"read {command.name} ({command.code:#04x}/{command.subcode:#04x})"
        key = (int(target), command.code, command.subcode)
        last: Exception | None = None
        for attempt in range(retries + 1):
            pending = self._register(key)
            try:
                self._send(
                    BafangId(
                        source=self.source,
                        target=int(target),
                        operation=CanOperation.READ_CMD,
                        code=command.code,
                        subcode=command.subcode,
                    ),
                    data,
                )
                return self._wait(pending, timeout, what)
            except TimeoutError_ as exc:
                last = exc
                log.debug("%s timed out (attempt %d)", what, attempt + 1)
            finally:
                self._unregister(key)
        assert last is not None
        raise last

    def write_short(
        self,
        target: int,
        command: Command,
        data: bytes,
        timeout: float | None = None,
        wait_ack: bool = True,
    ) -> BafangMessage | None:
        if len(data) > 8:
            raise ValueError("short writes carry at most 8 bytes")
        timeout = self.timeout if timeout is None else timeout
        what = f"write {command.name} ({command.code:#04x}/{command.subcode:#04x})"
        key = (int(target), command.code, command.subcode)
        pending = self._register(key) if wait_ack else None
        try:
            self._send(
                BafangId(
                    source=self.source,
                    target=int(target),
                    operation=command.operation
                    if command.operation is not None
                    else CanOperation.WRITE_CMD,
                    code=command.code,
                    subcode=command.subcode,
                ),
                data,
            )
            if pending is None:
                return None
            return self._wait(pending, timeout, what)
        finally:
            if pending is not None:
                self._unregister(key)

    def write_long(
        self,
        target: int,
        command: Command,
        data: bytes,
        timeout: float | None = None,
        delay: float = INTERFRAME_DELAY,
    ) -> BafangMessage:
        """Multi-frame write; returns the device's acknowledgement."""
        timeout = self.timeout if timeout is None else timeout
        what = f"write {command.name} ({command.code:#04x}/{command.subcode:#04x})"
        key = (int(target), command.code, command.subcode)
        pending = self._register(key)
        try:
            target = int(target)

            def send(operation: int, code: int, subcode: int, payload: bytes) -> None:
                self._send(
                    BafangId(
                        source=self.source,
                        target=target,
                        operation=operation,
                        code=code,
                        subcode=subcode,
                    ),
                    payload,
                )
                time.sleep(delay)

            # 1. announce the total length
            send(
                CanOperation.WRITE_CMD,
                command.code,
                command.subcode,
                bytes([len(data)]),
            )
            # 2. first 8 bytes
            send(
                CanOperation.MULTIFRAME_START,
                command.code,
                command.subcode,
                data[:8],
            )
            rest = data[8:]
            # 3. middle frames, sequence number in the subcode
            sequence = 0
            while len(rest) > 8:
                send(CanOperation.MULTIFRAME, 0, sequence, rest[:8])
                rest = rest[8:]
                sequence += 1
            # 4. remainder
            send(CanOperation.MULTIFRAME_END, 0, sequence, rest)
            return self._wait(pending, timeout, what)
        finally:
            self._unregister(key)

    def write(
        self, target: int, command: Command, data: bytes, **kwargs
    ) -> BafangMessage | None:
        """Write, picking single- or multi-frame framing by payload size."""
        if len(data) > 8:
            return self.write_long(target, command, data, **kwargs)
        return self.write_short(target, command, data, **kwargs)

    def ping(self, target: int, timeout: float = 0.6) -> bool:
        """True when the device answers a hardware-version read."""
        from .commands import READ

        try:
            self.read(target, READ["HardwareVersion"], timeout=timeout, retries=0)
            return True
        except BafangError:
            return False
