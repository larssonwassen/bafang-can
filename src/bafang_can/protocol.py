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
from .quality import LinkQuality

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 2.0
#: Gap between frames of a multi-frame write. The drive unit drops frames that
#: arrive faster than this.
INTERFRAME_DELAY = 0.02
MULTIFRAME_TIMEOUT = 3.0
#: How many messages may sit between the receive thread and the listeners.
#: A Bafang bus runs at about 165 frames/s, so this is roughly a minute of
#: backlog -- far more than any transient stall, and bounded so a listener
#: that has stopped consuming cannot exhaust memory.
LISTENER_QUEUE_SIZE = 10_000


class BafangError(RuntimeError):
    pass


class TimeoutError_(BafangError):
    """No answer from the device within the timeout."""


class DeviceError(BafangError):
    """The device answered with ERROR_ACK."""


@dataclass
class _Pending:
    queue: queue.Queue[BafangMessage]


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
        interframe_delay: float = INTERFRAME_DELAY,
    ) -> None:
        self.bus = bus
        self.source = int(source)
        self.timeout = timeout
        self.send_acks = send_acks
        self.interframe_delay = interframe_delay

        self._pending: dict[tuple[int, int, int], _Pending] = {}
        self._buffers: dict[tuple[int, int, int], _MultiframeBuffer] = {}
        self._lock = threading.Lock()
        self._listeners: list[Callable[[BafangMessage], None]] = []
        self._stop = threading.Event()
        self._rx_thread: threading.Thread | None = None
        self._emit_thread: threading.Thread | None = None
        self._emit_queue: queue.Queue[BafangMessage] = queue.Queue(
            maxsize=LISTENER_QUEUE_SIZE
        )
        #: What this session received, and what it lost getting it.
        self.quality = LinkQuality()
        #: Messages dropped because the listeners could not keep up.
        self.listener_overflows = 0

    # -- lifecycle ------------------------------------------------------

    def start(self) -> BafangClient:
        if self._rx_thread is not None:
            return self
        self._stop.clear()
        # Two threads on purpose. The receive thread must do nothing but drain
        # the adapter: a listener that prints a line or writes to a log file is
        # slower than the bus, and on slcan -- where python-can reads the
        # serial port one byte at a time -- a stalled reader overflows the tty
        # buffer and the kernel discards frames that already arrived. A capture
        # made this way lost 53% of the torque sensor's broadcasts. Handing
        # messages to a second thread keeps the reader tight against the wire.
        self._emit_thread = threading.Thread(
            target=self._emit_loop, name="bafang-emit", daemon=True
        )
        self._emit_thread.start()
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
        if self._emit_thread is not None:
            self._emit_thread.join(timeout=2.0)
            self._emit_thread = None

    def close(self) -> None:
        self.stop()
        try:
            self.bus.shutdown()
        except Exception:  # pragma: no cover - driver dependent
            log.debug("bus shutdown raised", exc_info=True)

    def __enter__(self) -> BafangClient:
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.close()

    def add_listener(self, callback: Callable[[BafangMessage], None]) -> None:
        """Register a callback for every message, including foreign traffic."""
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[BafangMessage], None]) -> None:
        """Unregister a callback. Silently ignores one that is not registered."""
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

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
            if message.is_error_frame:
                # Not a message from a node: the controller reporting that a
                # frame on the wire was malformed. Counted rather than
                # discarded, because on this bike they arrive in bursts under
                # motor load and that is worth knowing about.
                self.quality.observe(
                    message.arbitration_id,
                    bytes(message.data),
                    message.timestamp,
                    is_error_frame=True,
                )
                log.debug(
                    "bus error frame %#x %s",
                    message.arbitration_id,
                    bytes(message.data).hex(),
                )
                continue
            if not message.is_extended_id:
                continue
            # Corrupt frames are not a theoretical concern: python-can's slcan
            # backend parses the identifier with int(text, 16) and no range
            # check, so one dropped serial byte resynchronises mid-frame and
            # produces an identifier wider than the 29 bits CAN has.
            # BafangId.decode would mask it into a plausible message from a
            # device that does not exist.
            if not self.quality.observe(
                message.arbitration_id, bytes(message.data), message.timestamp
            ):
                log.debug(
                    "dropped a frame with an impossible identifier %#x",
                    message.arbitration_id,
                )
                continue
            # gs_usb echoes our own transmissions back with is_rx False. The
            # source check in _handle catches them too, but only while we are
            # impersonating the BESST tool id.
            if getattr(message, "is_rx", True) is False:
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

    #: Operations a device uses to answer a request addressed to it.
    #:
    #: Reassembled multi-frame answers are delivered carrying the identifier of
    #: their ``MULTIFRAME_START``, which is why that is an answer operation
    #: here while ``MULTIFRAME`` and ``MULTIFRAME_END`` are not.
    ANSWER_OPERATIONS = frozenset({
        CanOperation.NORMAL_ACK,
        CanOperation.ERROR_ACK,
        CanOperation.MULTIFRAME_START,
    })

    def _deliver(self, message: BafangMessage) -> None:
        """Route a received message to whoever is waiting for it.

        A pending request is only satisfied by a message **addressed to this
        tool** with an answer operation. Both halves of that matter, and the
        target check is the one that was missing.

        The drive unit broadcasts ``32/03`` to 0x1F every 2.004 s, and
        ``32/03`` is also the code a speed-parameter write is sent to. Keying
        pending requests on source, code and subcode alone let that broadcast
        satisfy the write and be reported as an acknowledgement -- so a write
        this firmware ignored would still have been reported as successful, as
        long as it arrived within a couple of seconds of a broadcast. That is
        the precise opposite of what "verified write" is supposed to mean, and
        it applied to the one field ``wheel`` writes.

        Nothing about the listener path changes: broadcasts still reach
        listeners, which is how ``read_speed_parameters`` falls back to reading
        ``32/03`` off the wire when the drive unit will not answer a read.
        """
        answers_us = (
            message.id.target == self.source
            and message.id.operation in self.ANSWER_OPERATIONS
        )
        if answers_us:
            key = (message.id.source, message.id.code, message.id.subcode)
            with self._lock:
                pending = self._pending.get(key)
            if pending is not None:
                pending.queue.put(message)
        self._emit(message)

    def _emit(self, message: BafangMessage) -> None:
        """Hand a message to the listener thread. Never blocks the receiver."""
        if not self._listeners:
            return
        try:
            self._emit_queue.put_nowait(message)
        except queue.Full:
            # Dropping here is a last resort, but it is a drop we can count and
            # report, which is strictly better than stalling the receive thread
            # and losing frames silently at the adapter instead.
            self.listener_overflows += 1

    def _emit_loop(self) -> None:
        while not self._stop.is_set():
            try:
                message = self._emit_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._dispatch(message)
        # Drain whatever arrived before the stop, so a capture that ends on a
        # burst still writes every frame it received.
        while True:
            try:
                self._dispatch(self._emit_queue.get_nowait())
            except queue.Empty:
                return

    def _dispatch(self, message: BafangMessage) -> None:
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
        delay: float | None = None,
    ) -> BafangMessage:
        """Multi-frame write; returns the device's acknowledgement."""
        timeout = self.timeout if timeout is None else timeout
        delay = self.interframe_delay if delay is None else delay
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

    def ping(self, target: int, timeout: float = 0.6, retries: int = 2) -> bool:
        """True when the device answers a hardware-version read.

        This retries where most reads do not, because the two failure modes are
        not symmetrical. A missed answer here reports a device as absent, and
        ``diagnose`` turns that into "check CAN-H/CAN-L wiring and polarity" --
        sending someone after a fault in a harness that is fine. A single
        attempt lost a `DP C340.CAN` on one run in six against a bus already
        carrying 166 broadcasts a second; three attempts have not lost it.

        The cost is bounded and only paid when a device really is absent: a
        node that answers does so on the first attempt.
        """
        from .commands import READ

        try:
            self.read(target, READ["HardwareVersion"], timeout=timeout, retries=retries)
            return True
        except BafangError:
            return False
