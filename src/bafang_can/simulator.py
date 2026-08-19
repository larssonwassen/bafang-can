"""A simulated Bafang CAN system.

This is not a toy: it implements the same framing rules as the real bus
(single-frame reads, multi-frame reads that must be acknowledged frame by
frame, multi-frame writes, error acks for unknown commands), so every CLI path
can be exercised end to end before any hardware exists. Use it with
``--interface sim``.

It answers as a plausible M200-class drive unit plus display, torque sensor
and battery. The numbers move a little between reads so ``monitor`` shows
something alive.

Provenance -- what is real here and what is invented
----------------------------------------------------
* **Framing and behaviour: derived from the vendored implementation**
  (``vendor/bafang_canable_pro/canbus.js`` and ``bafang-serializer.js``), and
  independently verified -- ``tests/test_differential_write.py`` proves the
  frames this package emits are byte-identical to the vendor serializer's.
* **Which commands answer: from the merged command table.** 0x60/0x17 and
  0x60/0x18 deliberately return ERROR_ACK, the codes in ``SILENT_READS``
  deliberately return nothing at all, and those in ``EMPTY_READS`` return a
  transfer of length zero, so ``probe`` shows all four outcomes a real bus
  produces -- answered, empty, refused, and silent.
* **The field values: invented.** They are plausible numbers for a 250 W /
  36 V mid-drive, not a capture from a real M200. The identity strings
  ("M200.G210", "CRX10.M200.1.0") are made up. Nothing here should be used to
  conclude what your motor will report.

Note the circularity this creates: the payloads below are built with this
package's own ``int_to_bytes_le`` and ``checksum``, so a decoding bug shared
by the simulator and the codecs would be invisible to any test that only uses
this module. That is why the differential tests exist -- the vendored
JavaScript is the external oracle. Tests here cover wiring and CLI behaviour;
correctness of the byte layouts is established elsewhere.

Replacing this with a recorded capture from a real bike (see
``bafang-can sniff`` and ``decode-log``) would be a strict improvement.
"""

from __future__ import annotations

import json
import math
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .constants import CanOperation, DeviceId, wheel_by_text
from .frame import BafangId, checksum, int_to_bytes_le

#: How long a simulated device takes to answer, in seconds.
LATENCY = 0.002

#: Read commands the simulated bike ignores completely, rather than declining.
#:
#: A real firmware does both, and they mean different things: ERROR_ACK says a
#: handler exists and said no, silence says nothing handled the request. An
#: unassigned code on the parameter page is here so the probe's control has
#: something honest to measure against.
SILENT_READS: frozenset[tuple[int, int]] = frozenset({
    (0x60, 0x7F),  # unassigned -- the probe's control
    (0x62, 0xD9),  # ControllerStartupAngle
})

#: Read commands answered with a well-formed transfer carrying no bytes.
#:
#: The fourth thing a real firmware does, and the one a three-way probe hid. A
#: real G210 answers ``60/06`` with ``MULTIFRAME_START`` declaring a length of
#: zero followed straight by ``MULTIFRAME_END``: the handler is there and the
#: value is blank. That is neither an answer worth printing nor an absence.
EMPTY_READS: frozenset[tuple[int, int]] = frozenset({
    (0x60, 0x06),  # SystemParams -- observed empty on a real M200
})


def _block(values: dict[int, int]) -> bytearray:
    body = bytearray(64)
    for offset, value in values.items():
        body[offset] = value & 0xFF
    body[63] = checksum(body[:63])
    return body


def _default_parameter0() -> bytearray:
    values: dict[int, int] = {0: 0x01}
    for i in range(9):
        values[1 + i] = 20 + i * 5  # acceleration levels
        low, high = int_to_bytes_le(50 + i * 50, 2)  # assist ratio, percent
        values[10 + i * 2] = low
        values[10 + i * 2 + 1] = high
    low, high = int_to_bytes_le(500, 2)
    values[28] = low
    values[29] = high
    return _block(values)


def _default_parameter1() -> bytearray:
    values = {
        0: 36,  # system voltage
        1: 15,  # current limit A
        2: 43,  # overvoltage V
        9: 10,  # max current on low charge
        10: 15,  # limp mode SOC
        11: 5,
        12: 60,  # full capacity range km
        13: 0,  # torque sensor
        14: 0,  # no coaster brake
        15: 32,  # pedal signals per rotation
        16: 1,  # speed sensor channel
        18: 1,  # mid drive
        19: 8,  # pole pairs
        20: 1,  # speedometer magnets
        21: 1,  # temperature sensor
        34: 12,  # throttle start 1.2 V
        35: 42,  # throttle max 4.2 V
        37: 25,  # start current %
        38: 5,  # loading time 0.5 s
        39: 5,  # shedding time 0.5 s
        58: 0,
        59: 0,
    }
    for index, (low, high) in enumerate(
        [int_to_bytes_le(300, 2), int_to_bytes_le(320, 2)]
    ):
        values[3 + index * 2] = low
        values[4 + index * 2] = high
    values[7], values[8] = int_to_bytes_le(14000, 2)  # capacity mAh
    values[22], values[23] = int_to_bytes_le(2100, 2)  # deceleration ratio 21.00
    values[24], values[25] = int_to_bytes_le(4500, 2)  # max rotor rpm
    for i in range(9):
        values[40 + i] = min(100, 10 + i * 11)  # current limit %
        values[49 + i] = min(100, 20 + i * 10)  # speed limit %
    values[60], values[61] = int_to_bytes_le(600, 2)  # walk assist 6.00 km/h
    return _block(values)


def _default_parameter2() -> bytearray:
    values: dict[int, int] = {54: 3}
    for i in range(6):
        values[0 + i] = 8 + i  # start torque
        values[6 + i] = 90 + i  # max torque
        values[12 + i] = 20 + i  # return torque
        values[18 + i] = 80 + i  # max current
        values[24 + i] = 10 + i  # min current
        values[30 + i] = 4  # torque decay
        values[36 + i] = 3  # start pulse
        values[42 + i] = 6  # current decay -> 30 ms
        values[48 + i] = 5  # stop delay -> 10 ms
    return _block(values)


def _default_speed() -> bytearray:
    wheel = wheel_by_text("27.5")
    assert wheel is not None
    low, high = int_to_bytes_le(2500, 2)  # 25.00 km/h
    circumference = int_to_bytes_le(2215, 2)
    return bytearray([low, high, wheel.code[0], wheel.code[1], *circumference])


@dataclass
class SimulatedDevice:
    device: DeviceId
    #: (code, subcode) -> payload. Callables are evaluated per read.
    responses: dict[tuple[int, int], object] = field(default_factory=dict)


@dataclass
class RideState:
    """Slowly varying numbers so live views have something to show."""

    started: float = field(default_factory=time.monotonic)
    pedalling: bool = True

    def phase(self) -> float:
        return time.monotonic() - self.started

    def speed_kmh(self) -> float:
        if not self.pedalling:
            return 0.0
        return 18 + 6 * math.sin(self.phase() / 4)

    def cadence(self) -> int:
        return int(60 + 15 * math.sin(self.phase() / 3)) if self.pedalling else 0

    def torque(self) -> int:
        base = 1100 if self.pedalling else 780
        return int(base + 240 * max(0.0, math.sin(self.phase() / 2)))

    def voltage(self) -> float:
        return 40.5 - 0.02 * self.phase() - (0.4 if self.pedalling else 0)

    def current(self) -> float:
        return max(0.0, 6 * math.sin(self.phase() / 2.5)) if self.pedalling else 0.2


class SimBus:
    """A python-can-compatible bus backed by simulated Bafang devices."""

    def __init__(
        self,
        errors: list[int] | None = None,
        pedalling: bool = True,
        drop_rate: float = 0.0,
        seed: int | None = 0,
        chatter: bool = True,
        state_path: str | None = None,
        profile: str | None = None,
    ) -> None:
        self.rx: queue.Queue = queue.Queue()
        self.sent: list = []
        self.state = RideState(pedalling=pedalling)
        self.random = random.Random(seed)
        self.drop_rate = drop_rate
        self.errors = errors if errors is not None else [21]
        self.blocks: dict[tuple[int, int, int], bytearray] = {}
        self._pending_write: tuple[int, int, int] | None = None
        self._write_buffer = bytearray()
        self._build()
        self.profile_source = ""
        self.profile_responses: set[tuple[int, int, int]] = set()
        if profile:
            self._load_profile(profile)
        self.state_path = Path(state_path) if state_path else None
        self._load_state()
        self._stop = threading.Event()
        self._chatter: threading.Thread | None = None
        if chatter:
            self._chatter = threading.Thread(
                target=self._chatter_loop, name="sim-chatter", daemon=True
            )
            self._chatter.start()

    # -- recorded device profiles -------------------------------------------

    def _load_profile(self, path: str) -> None:
        """Replace the invented answers with ones a real bike gave.

        Anything the profile does not cover keeps its invented default, so a
        partial capture is still useful. ``profile_responses`` records which
        keys are real, so callers can tell the two apart instead of guessing.
        """
        from .capture import DeviceProfile

        loaded = DeviceProfile.load(path)
        for key, payload in loaded.blocks().items():
            self.blocks[key] = payload
            self.profile_responses.add(key)
        self.profile_source = loaded.source or str(path)

    def is_recorded(self, device: int, code: int, subcode: int) -> bool:
        """True when this answer came from a real bike rather than a default."""
        return (int(device), code, subcode) in self.profile_responses

    # -- optional persistence ----------------------------------------------

    def _load_state(self) -> None:
        """Restore a bike that an earlier run left behind.

        Without this, every CLI invocation gets a factory-fresh motor, so a
        rehearsal of the real workflow (dump, change, verify, restore) cannot
        be practised offline.
        """
        if self.state_path is None or not self.state_path.exists():
            return
        stored = json.loads(self.state_path.read_text())
        for key, value in stored.get("blocks", {}).items():
            device, code, subcode = (int(part) for part in key.split(":"))
            self.blocks[(device, code, subcode)] = bytearray(bytes.fromhex(value))
        if "errors" in stored:
            self.errors = list(stored["errors"])

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        payload = {
            "blocks": {
                f"{device}:{code}:{subcode}": bytes(value).hex()
                for (device, code, subcode), value in self.blocks.items()
            },
            "errors": self.errors,
        }
        self.state_path.write_text(json.dumps(payload, indent=2))

    def _chatter_loop(self) -> None:
        """Traffic between the bike's own nodes, addressed to neither of us.

        A real bus is never quiet: the display polls the drive unit and the
        battery continuously. Reproducing that here keeps the tool honest
        about ignoring (and never acknowledging) frames it is not the target
        of.
        """
        import can

        polls = (
            (DeviceId.DISPLAY, DeviceId.DRIVE_UNIT, CanOperation.READ_CMD, 0x32, 0x00),
            (DeviceId.DRIVE_UNIT, DeviceId.DISPLAY, CanOperation.NORMAL_ACK, 0x32, 0x00),
            (DeviceId.DISPLAY, DeviceId.BATTERY, CanOperation.READ_CMD, 0x34, 0x01),
            (DeviceId.BATTERY, DeviceId.DISPLAY, CanOperation.NORMAL_ACK, 0x34, 0x01),
        )
        while not self._stop.wait(0.1):
            source, target, operation, code, subcode = polls[
                self.random.randrange(len(polls))
            ]
            ident = BafangId(
                source=int(source),
                target=int(target),
                operation=int(operation),
                code=code,
                subcode=subcode,
            )
            if operation == CanOperation.READ_CMD:
                payload = bytearray()  # requests carry no data
            else:
                payload = self._dynamic((int(source), code, subcode)) or bytearray(1)
            self.rx.put(
                can.Message(
                    arbitration_id=ident.encode(),
                    data=bytes(payload[:8]),
                    is_extended_id=True,
                    timestamp=time.time(),
                )
            )

    # -- device tables ---------------------------------------------------

    def _build(self) -> None:
        du = int(DeviceId.DRIVE_UNIT)
        disp = int(DeviceId.DISPLAY)
        sensor = int(DeviceId.TORQUE_SENSOR)
        battery = int(DeviceId.BATTERY)

        def text(value: str) -> bytearray:
            return bytearray(value.encode("ascii") + b"\x00")

        self.blocks.update(
            {
                # drive unit identity
                (du, 0x60, 0x00): text("HM2.1"),
                (du, 0x60, 0x01): text("CRX10.M200.1.0"),
                (du, 0x60, 0x02): text("M200.G210"),
                (du, 0x60, 0x03): text("DP12345678"),
                (du, 0x60, 0x05): text("BAFANG"),
                # drive unit configuration
                (du, 0x60, 0x10): _default_parameter0(),
                (du, 0x60, 0x11): _default_parameter1(),
                (du, 0x60, 0x12): _default_parameter2(),
                (du, 0x32, 0x03): _default_speed(),
                (du, 0x62, 0xD9): bytearray([0x2C, 0x01]),
                # display
                (disp, 0x60, 0x00): text("HD1.0"),
                (disp, 0x60, 0x01): text("DP.C01.1.2"),
                (disp, 0x60, 0x02): text("DP C01"),
                (disp, 0x60, 0x03): text("DP87654321"),
                (disp, 0x60, 0x08): text("BL1.0"),
                (disp, 0x63, 0x01): bytearray(
                    [0x2A, 0x05, 0x00, 0x1E, 0x00, 0x00, 0x0E, 0x01]
                ),
                (disp, 0x63, 0x02): bytearray([0x96, 0x00, 0x10, 0x27, 0x00]),
                (disp, 0x63, 0x03): bytearray([5]),
                # torque sensor
                (sensor, 0x60, 0x00): text("HS1.0"),
                (sensor, 0x60, 0x01): text("SN.TS.1.1"),
                (sensor, 0x60, 0x03): text("TS11223344"),
                # battery
                (battery, 0x60, 0x00): text("HB1.0"),
                (battery, 0x60, 0x01): text("BT1.4"),
                (battery, 0x60, 0x03): text("BT99887766"),
                (battery, 0x34, 0x00): bytearray(
                    [*int_to_bytes_le(14000, 2), *int_to_bytes_le(9800, 2), 70, 68, 96]
                ),
                (battery, 0x64, 0x00): bytearray([10, 4, *int_to_bytes_le(14000, 2)]),
                (battery, 0x64, 0x01): bytearray(
                    [*int_to_bytes_le(42, 2), *int_to_bytes_le(120, 2), *int_to_bytes_le(18, 2)]
                ),
            }
        )

    def _dynamic(self, key: tuple[int, int, int]) -> bytearray | None:
        du = int(DeviceId.DRIVE_UNIT)
        state = self.state
        if key == (du, 0x32, 0x00):
            return bytearray(
                [
                    70,
                    *int_to_bytes_le(int(state.phase() * 3), 2),
                    state.cadence(),
                    *int_to_bytes_le(state.torque(), 2),
                    *int_to_bytes_le(4200, 2),
                ]
            )
        if key == (du, 0x32, 0x01):
            return bytearray(
                [
                    *int_to_bytes_le(int(state.speed_kmh() * 100), 2),
                    *int_to_bytes_le(int(state.current() * 100), 2),
                    *int_to_bytes_le(int(state.voltage() * 100), 2),
                    68,  # 28 C controller
                    72,  # 32 C motor
                ]
            )
        if key == (du, 0x12, 0x00):
            return bytearray([1])
        if key == (int(DeviceId.TORQUE_SENSOR), 0x31, 0x00):
            return bytearray([*int_to_bytes_le(state.torque(), 2), state.cadence()])
        if key == (int(DeviceId.BATTERY), 0x34, 0x01):
            return bytearray(
                [
                    *int_to_bytes_le(int(state.current() * 100), 2),
                    *int_to_bytes_le(int(state.voltage() * 100), 2),
                    62,
                ]
            )
        if key[1] == 0x60 and key[2] == 0x07:
            if key[0] not in (int(DeviceId.DRIVE_UNIT), int(DeviceId.DISPLAY)):
                return None
            text = "".join(f"{code:02d}" for code in self.errors)
            return bytearray(text.encode("ascii") + b"\x00")
        return None

    # -- python-can surface ------------------------------------------------

    def send(self, message, timeout: float | None = None) -> None:
        self.sent.append(message)
        if self.drop_rate and self.random.random() < self.drop_rate:
            return  # simulate a lost frame
        self._handle(message)

    def recv(self, timeout: float | None = None):
        try:
            return self.rx.get(timeout=timeout)
        except queue.Empty:
            return None

    def shutdown(self) -> None:
        self._stop.set()
        if self._chatter is not None:
            self._chatter.join(timeout=1.0)
            self._chatter = None

    # -- protocol behaviour --------------------------------------------------

    def _emit(
        self,
        source: int,
        operation: int,
        code: int,
        subcode: int,
        data: bytes,
        target: int = int(DeviceId.TOOL),
    ) -> None:
        import can

        ident = BafangId(
            source=source,
            target=target,
            operation=operation,
            code=code,
            subcode=subcode,
        )
        self.rx.put(
            can.Message(
                arbitration_id=ident.encode(),
                data=bytes(data),
                is_extended_id=True,
                timestamp=time.time(),
            )
        )

    def _handle(self, message) -> None:
        ident = BafangId.decode(message.arbitration_id)
        target = ident.target
        if target == int(DeviceId.BROADCAST):
            return
        key = (target, ident.code, ident.subcode)
        data = bytes(message.data)
        time.sleep(LATENCY)

        if ident.operation == CanOperation.READ_CMD:
            if key in self.profile_responses:
                # A recorded answer always wins over a synthesized one, even
                # for live data. That means telemetry replayed from a profile
                # is frozen at the captured value; drop --sim-profile if you
                # want the moving numbers back.
                payload = self.blocks.get(key)
            else:
                payload = self._dynamic(key)
                if payload is None:
                    payload = self.blocks.get(key)
            if payload is None:
                if (ident.code, ident.subcode) in EMPTY_READS:
                    self._emit(
                        target, CanOperation.MULTIFRAME_START,
                        ident.code, ident.subcode, b"\x00",
                    )
                    self._emit(target, CanOperation.MULTIFRAME_END, 0x00, 0x00, b"")
                    return
                if (ident.code, ident.subcode) in SILENT_READS:
                    # Deliberately no answer at all, so `probe` can show the
                    # difference between a firmware that declines a command and
                    # one that has no handler for it. Both looked identical
                    # until the probe started reporting them apart.
                    return
                self._emit(target, CanOperation.ERROR_ACK, ident.code, ident.subcode, b"\x00")
                return
            self._send_payload(target, ident.code, ident.subcode, bytes(payload))
            return

        if ident.operation == CanOperation.WRITE_CMD:
            self._handle_write(target, ident, data, key)
            return

        if ident.operation == CanOperation.MULTIFRAME_START:
            self._pending_write = key
            self._write_buffer = bytearray(data)
            return

        if ident.operation in (CanOperation.MULTIFRAME, CanOperation.MULTIFRAME_END):
            if self._pending_write is None:
                return
            self._write_buffer.extend(data)
            if ident.operation == CanOperation.MULTIFRAME_END:
                pending = self._pending_write
                self.blocks[pending] = bytearray(self._write_buffer)
                self._pending_write = None
                self._save_state()
                self._emit(
                    pending[0], CanOperation.NORMAL_ACK, pending[1], pending[2], b"\x00"
                )
            return

    def _handle_write(self, target: int, ident: BafangId, data: bytes, key) -> None:
        # Calibration and clear-errors are commands, not stored values.
        if (ident.code, ident.subcode) == (0x61, 0x01):  # calibrate torque sensor
            self.state.started = time.monotonic()
            self._emit(target, CanOperation.NORMAL_ACK, ident.code, ident.subcode, b"\x00")
            return
        if (ident.code, ident.subcode) == (0x62, 0x00):  # calibrate position sensor
            self._emit(target, CanOperation.NORMAL_ACK, ident.code, ident.subcode, b"\x00")
            return
        if (ident.code, ident.subcode) == (0x60, 0x07):  # clear errors
            self.errors = []
            self._save_state()
            self._emit(target, CanOperation.NORMAL_ACK, ident.code, ident.subcode, b"\x00")
            return

        stored = self.blocks.get(key)
        if len(data) == 1 and stored is not None and len(stored) > 8:
            # length announcement of a multi-frame write
            self._pending_write = key
            self._write_buffer = bytearray()
            return
        self.blocks[key] = bytearray(data)
        self._save_state()
        self._emit(target, CanOperation.NORMAL_ACK, ident.code, ident.subcode, b"\x00")
        self._echo_broadcast(target, ident, bytes(data))

    #: Codes the drive unit re-broadcasts after accepting a write to them.
    #:
    #: A real G210 answers a 32/03 write with NORMAL_ACK after 5 ms and then
    #: puts the new value on its own 2.004 s broadcast 300 ms later. That
    #: broadcast is the only way to confirm the write, because this firmware
    #: will not answer a read of 32/03 -- so the simulator has to produce it or
    #: the verification path is never exercised off the bike.
    BROADCAST_AFTER_WRITE: frozenset[tuple[int, int]] = frozenset({(0x32, 0x03)})

    def _echo_broadcast(self, source: int, ident: BafangId, data: bytes) -> None:
        if (ident.code, ident.subcode) not in self.BROADCAST_AFTER_WRITE:
            return
        self._emit(
            source,
            CanOperation.WRITE_CMD,
            ident.code,
            ident.subcode,
            data,
            target=int(DeviceId.BROADCAST),
        )

    def _send_payload(self, source: int, code: int, subcode: int, payload: bytes) -> None:
        if len(payload) <= 8:
            self._emit(source, CanOperation.NORMAL_ACK, code, subcode, payload)
            return
        self._emit(source, CanOperation.MULTIFRAME_START, code, subcode, bytes([len(payload)]))
        self._emit(source, CanOperation.MULTIFRAME, code, 0, payload[:8])
        rest = payload[8:]
        sequence = 1
        while len(rest) > 8:
            self._emit(source, CanOperation.MULTIFRAME, code, sequence, rest[:8])
            rest = rest[8:]
            sequence += 1
        self._emit(source, CanOperation.MULTIFRAME_END, code, sequence, rest)
