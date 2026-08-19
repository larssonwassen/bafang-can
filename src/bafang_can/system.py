"""High-level access to a Bafang CAN system.

Everything here is built on :class:`~bafang_can.protocol.BafangClient` and is
deliberately read-first: the diagnose/dump paths never write, and every write
path takes the block that was just read as its base.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar

from . import codecs
from .commands import READ, WRITE, Command
from .constants import DeviceId, error_text
from .frame import BafangMessage, string_from_bytes, string_to_bytes
from .protocol import BafangClient, BafangError, DeviceError

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


class BroadcastRealtime:
    """Latest realtime values, collected from broadcasts as they arrive.

    Nothing is transmitted: this only reads what the bus already carries, so
    it works with the adapter in listen-only mode. Values are whatever came
    in most recently, and a key is absent until its first broadcast, which is
    the honest representation -- a node that never broadcasts is not reported
    as zero.
    """

    #: (code, subcode) -> (snapshot key, codec)
    #:
    #: Everything here was observed arriving unasked on a real G210 bus. The
    #: last three are not broadcasts the drive unit pushes but traffic between
    #: other nodes -- the display polls the battery for 64/01 about ten
    #: times a second, and the answer is on the wire whether or not we ask for
    #: it. Reading them costs nothing and works in listen-only mode, where an
    #: active read is not available at all.
    SOURCES: ClassVar[dict[tuple[int, int], tuple[str, Any]]] = {
        (0x32, 0x00): ("controller0", codecs.ControllerRealtime0),
        (0x32, 0x01): ("controller1", codecs.ControllerRealtime1),
        (0x31, 0x00): ("sensor", codecs.SensorRealtime),
        (0x34, 0x01): ("battery", codecs.BatteryState),
        (0x34, 0x00): ("battery_capacity", codecs.BatteryCapacity),
        (0x64, 0x01): ("battery_charging", codecs.BatteryChargingInfo),
        (0x32, 0x03): ("speed_parameters", codecs.SpeedParameters),
        (0x12, 0x00): ("state", codecs.ControllerState),
        (0x32, 0x07): ("shaft", codecs.OutputShaftCounter),
        (0x63, 0x00): ("display", codecs.DisplayRealtime),
        (0x30, 0x00): ("uptime", codecs.SystemUptime),
    }

    def __init__(self, client: BafangClient) -> None:
        self._values: dict[str, Any] = {}
        self._seen: dict[str, float] = {}
        self._undecodable: dict[str, str] = {}
        self._shaft_rpm: float | None = None
        self._shaft_history: deque[tuple[float, codecs.OutputShaftCounter]] = deque()
        client.add_listener(self._on_message)

    def _on_message(self, message: BafangMessage) -> None:
        entry = self.SOURCES.get((message.id.code, message.id.subcode))
        if entry is None:
            return
        key, codec = entry
        try:
            decoded = codec.decode(message.data)
        except codecs.DecodeError as exc:
            # A broadcast whose length does not match what the codec expects
            # is worth surfacing, not swallowing: it is the signal that this
            # firmware lays the payload out differently.
            self._undecodable[key] = str(exc)
            return
        if isinstance(decoded, codecs.OutputShaftCounter):
            self._update_shaft_rpm(decoded, message.timestamp)
        self._values[key] = decoded
        self._seen[key] = message.timestamp
        self._undecodable.pop(key, None)

    #: Derive the shaft rate over at least this long a baseline.
    #:
    #: Adjacent broadcasts are about 96 ms apart, but the gs_usb adapter
    #: delivers a few frames a second out of order, so an adjacent pair can be
    #: 35 ms apart and turn a genuine 550 rpm into a reported 1621. Measuring
    #: across a window instead makes the reading immune to that jitter at the
    #: cost of a little lag.
    SHAFT_WINDOW = 1.0
    MIN_SHAFT_BASELINE = 0.4

    #: Reject a derived shaft speed above this as not physically credible.
    #:
    #: This is a plausibility bound, not a specification. The output shaft of a
    #: mid-drive is the crank spindle: a rider turns it at 60--90 rpm, and the
    #: fastest anything has driven it here is 518 rpm on a power drill. A
    #: reading far above that does not mean the shaft was fast, it means the
    #: capture is damaged -- a stale frame at the head of a log leaves a jump
    #: of 1357 counts across the boundary between the old stream and the new,
    #: which reads as 7977 rpm. Captures made since ``open_bus`` started
    #: draining the adapter do not contain that boundary, but logs recorded
    #: before it, or by other tools, still do.
    MAX_CREDIBLE_SHAFT_RPM = 1500.0

    def _update_shaft_rpm(
        self, counter: codecs.OutputShaftCounter, timestamp: float
    ) -> None:
        history = self._shaft_history
        history.append((timestamp, counter))
        while len(history) > 2 and timestamp - history[0][0] > self.SHAFT_WINDOW:
            history.popleft()
        oldest_time, oldest = history[0]
        elapsed = timestamp - oldest_time
        if elapsed < self.MIN_SHAFT_BASELINE:
            return
        rpm = counter.rpm_since(oldest, elapsed)
        if rpm is not None and rpm > self.MAX_CREDIBLE_SHAFT_RPM:
            log.debug("discarding an implausible shaft speed of %.0f rpm", rpm)
            rpm = None
        if rpm is None:
            # The counter restarted. Drop the history rather than keep
            # measuring against a baseline from before it, and stop reporting
            # a speed that is no longer derived from anything.
            history.clear()
            history.append((timestamp, counter))
            self._shaft_rpm = None
            return
        self._shaft_rpm = rpm

    #: Fractional disagreement between two independently broadcast voltages
    #: that is too large to be measurement noise.
    VOLTAGE_TOLERANCE = 0.1

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self._values)
        if self._shaft_rpm is not None:
            out["shaft_rpm"] = round(self._shaft_rpm, 1)
        for key, reason in self._undecodable.items():
            out[f"{key}_error"] = reason
        disagreement = self._voltage_disagreement()
        if disagreement is not None:
            out["layout_warning"] = disagreement
        return out

    def _voltage_disagreement(self) -> str | None:
        """Do the controller and the battery agree about the pack voltage?

        They measure the same pack and broadcast it separately, so a large
        disagreement means one of two things, and the tool cannot tell them
        apart from a single reading:

        * the drive unit's voltage sense really is out of calibration, or
        * the controller payload is not laid out the way this codec assumes.

        The first is what it turned out to be on the unit this was written
        against: a CR X210.350.FC decoded 51.5 V while its battery decoded
        37.47 V on a 36 V pack, and varying the supply showed the field
        tracking real voltage scaled by a constant 1.363 -- a gain error in
        the analog divider ahead of the ADC, traced to a dirt bridge across
        it. The codec was reading the right bytes the whole time. See
        ``docs/m200.md`` for how to separate the two with a bench supply.

        Either way, reporting a confident wrong number is worse than reporting
        the doubt, so the disagreement is printed instead of either voltage.
        """
        controller = self._values.get("controller1")
        battery = self._values.get("battery")
        if controller is None or battery is None or not battery.voltage:
            return None
        error = abs(controller.voltage - battery.voltage) / battery.voltage
        if error <= self.VOLTAGE_TOLERANCE:
            return None
        ratio = controller.voltage / battery.voltage
        return (
            f"controller reports {controller.voltage:.2f} V but the battery "
            f"reports {battery.voltage:.2f} V, a factor of {ratio:.3f}. Either "
            "the drive unit's voltage sense is out of calibration or its "
            "payload is laid out differently on this firmware; a multimeter on "
            "the pack settles which. Treat the controller voltage, and any "
            "fault that depends on it, as unverified until it is settled."
        )

    @property
    def seen(self) -> dict[str, float]:
        """Timestamp of the most recent broadcast per key."""
        return dict(self._seen)


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

    #: A command code no Bafang device implements, probed as a control.
    #:
    #: 0x60 is the page the parameter blocks live on and 0x7F is unassigned on
    #: it, so this measures what *this* firmware does with a request it has no
    #: handler for -- on the same page as the commands under suspicion. Without
    #: it, "Parameter1 did not answer" has no baseline to be compared against.
    CONTROL_COMMAND: ClassVar[Command] = Command(
        name="ControlUnassigned", code=0x60, subcode=0x7F, devices=tuple(DeviceId)
    )

    #: Attempts before a command is called silent.
    #:
    #: A false "silent" is the one error this probe cannot afford: it is the
    #: reading that says a firmware never implemented a command, and the whole
    #: lock question turns on it. A single attempt is not enough to support
    #: that claim, and the number needed was measured rather than guessed.
    #:
    #: Ten attempts at each command, on a real bike, separate two populations
    #: cleanly. The drive unit is deterministic -- every command it implements
    #: answered 10 out of 10 and every one it does not answered 0 out of 10.
    #: The DP C340.C display is not: its implemented commands answered between
    #: 6 and 9 times out of 10, while the ones it lacks still answered 0 out of
    #: 10. So the categories never overlap, but on the display a command has to
    #: be asked several times before silence means anything.
    #:
    #: Six attempts puts a false "silent" below 1% even at the worst measured
    #: rate (0.4 ** 6). Three does not: at 6-in-10 it fails 6% of the time,
    #: which is what made a first pass report this display's ``ErrorCode`` and
    #: ``SoftwareVersion`` as absent when both answer. Only commands about to
    #: be called absent pay for the extra attempts.
    PROBE_ATTEMPTS: ClassVar[int] = 6

    def _outcome(self, device: DeviceId, command: Command, timeout: float) -> str:
        """How a device responds to one read: answered, empty, refused, silent.

        The four are different claims and the difference is the whole point.
        ``refused`` means the firmware has a handler for that command and
        declined -- it knows the command exists. ``silent`` means nothing came
        back at all, which is what a firmware without the handler does.

        ``empty`` is the case a three-way split hid. This M200 answers
        ``60/06`` with a properly formed multi-frame transfer whose declared
        length is zero: the handler is there and the value is blank. Reporting
        that as ``answered`` alongside a serial number would overstate it, and
        reporting it as ``silent`` would deny a handler that demonstrably ran.

        Only ``silent`` is retried. A refusal and an answer are both positive
        evidence and arrive first time; silence is the absence of evidence and
        has to survive :attr:`PROBE_ATTEMPTS` attempts before it is reported.
        """
        for attempt in range(self.PROBE_ATTEMPTS):
            try:
                message = self.client.read(device, command, timeout=timeout, retries=0)
            except DeviceError:
                return "refused"
            except BafangError:
                if attempt + 1 == self.PROBE_ATTEMPTS:
                    return "silent"
                continue
            return "answered" if message.data else "empty"
        raise AssertionError("unreachable")  # pragma: no cover

    def capabilities(self, device: DeviceId = DeviceId.DRIVE_UNIT) -> dict[str, str]:
        """Probe how this particular unit responds to each read command.

        Bafang firmware varies a lot between motor generations; this is the
        honest way to find out what a given M200/G210 supports rather than
        assuming the M500-era command set is complete.

        **This sends reads only.** A silent result is a statement about the
        read handler and nothing else: a real G210 answers a read of ``32/03``
        0 times in 10 and accepts a write to it in 5 ms, applying the value.
        Read "silent" as "does not answer a read", never as "not implemented".

        Each command reports ``answered``, ``empty``, ``refused`` or ``silent``
        rather than a bare true/false. Collapsing the last two into
        "unsupported" throws away the only evidence on the bus that separates a
        firmware which never implemented a command from one that implements it
        and says no -- which is exactly the question when a unit answers
        identity reads and nothing else. ``ControlUnassigned`` is a command
        that does not exist, included so the silent case has a measured
        baseline: if it is silent and ``Parameter1`` is refused, the firmware
        is discriminating between them.
        """
        result: dict[str, str] = {}
        for name, command in READ.items():
            if not command.applies_to(device) or name.startswith("FwUpdate"):
                continue
            result[name] = self._outcome(device, command, timeout=0.6)
        result[self.CONTROL_COMMAND.name] = self._outcome(
            device, self.CONTROL_COMMAND, timeout=0.6
        )
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

    #: How long to wait for a 32/03 broadcast before giving up.
    #:
    #: Measured at 2.004 s between broadcasts, steady to +-0.01 s across twelve
    #: captures, so this allows two clear periods plus slack.
    SPEED_BROADCAST_WAIT = 5.0

    def await_broadcast(
        self, code: int, subcode: int, timeout: float
    ) -> BafangMessage | None:
        """Wait for one broadcast of a given command, or ``None`` on timeout.

        Transmits nothing, so it works in listen-only mode and against firmware
        that answers no read at all.
        """
        import queue as _queue

        received: _queue.Queue[BafangMessage] = _queue.Queue(maxsize=1)

        def listener(message: BafangMessage) -> None:
            if (message.id.code, message.id.subcode) == (code, subcode):
                try:
                    received.put_nowait(message)
                except _queue.Full:
                    pass

        self.client.add_listener(listener)
        try:
            return received.get(timeout=timeout)
        except _queue.Empty:
            return None
        finally:
            self.client.remove_listener(listener)

    def read_speed_parameters(self) -> codecs.SpeedParameters:
        """The wheel size, circumference and speed limit currently in force.

        Asks the drive unit first, and falls back to listening for the
        broadcast. A `CR X210.350.FC` does not answer a `32/03` read -- but it
        *broadcasts* `32/03` every two seconds, and the write command is the
        same 32/03. So the values are on the wire even where the read is not
        available, and without this fallback `wheel` was unusable on exactly
        the firmware that most needs it: the one that will not hand over any
        parameter block either.
        """
        try:
            message = self.client.read(DeviceId.DRIVE_UNIT, READ["SpeedParameters"])
        except BafangError:
            broadcast = self.await_broadcast(0x32, 0x03, self.SPEED_BROADCAST_WAIT)
            if broadcast is None:
                raise
            log.info("32/03 was not answered; read from the broadcast instead")
            return codecs.SpeedParameters.decode(broadcast.data)
        return codecs.SpeedParameters.decode(message.data)

    #: How long to watch the broadcast for a written value to appear.
    #:
    #: Measured at 0.30 s and 1.81 s on a real G210 across two writes, against
    #: a 2.004 s broadcast period. Three periods is generous and still bounded.
    SPEED_VERIFY_WAIT: ClassVar[float] = 6.0

    def write_speed_parameters(
        self, params: codecs.SpeedParameters, verify: bool = True
    ) -> BafangMessage | None:
        """Write the speed parameters and confirm they took effect.

        This firmware will not answer a read of ``32/03``, so the usual
        read-back verification is unavailable here -- which is why this used to
        report success on the acknowledgement alone. It broadcasts ``32/03``
        every 2.004 s regardless, and that broadcast does reflect a write:
        measured on a real G210, writing a circumference of 2206 mm produced
        ``NORMAL_ACK`` after 5 ms and a broadcast carrying the new value 300 ms
        later.

        So the value on the wire is the verification. It is better evidence
        than a read-back would be, because it is what the drive unit tells the
        rest of the bike rather than what it tells the tool that just wrote.
        """
        import queue as _queue

        payload = params.encode()
        # Listen before writing, not after. On the bike the confirming
        # broadcast came 300 ms behind the acknowledgement, but nothing
        # guarantees that gap -- a bus that broadcast sooner would have the
        # evidence arrive while this was still returning from the write, and
        # the verification would fail on a write that had succeeded.
        seen: _queue.Queue[bytes] = _queue.Queue()

        def listener(message: BafangMessage) -> None:
            # Only the broadcast counts. The write's own NORMAL_ACK carries the
            # same code and subcode, and letting it through would make the
            # failure message quote the acknowledgement as though it were the
            # value on the wire.
            if (
                message.id.target == DeviceId.BROADCAST
                and (message.id.code, message.id.subcode) == (0x32, 0x03)
            ):
                seen.put(bytes(message.data))

        self.client.add_listener(listener)
        try:
            ack = self.client.write(
                DeviceId.DRIVE_UNIT, WRITE["SpeedParameters"], payload
            )
            if not verify:
                return ack
            deadline = time.monotonic() + self.SPEED_VERIFY_WAIT
            last: bytes | None = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    last = seen.get(timeout=remaining)
                except _queue.Empty:
                    break
                if last[: len(payload)] == payload:
                    return ack
        finally:
            self.client.remove_listener(listener)
        raise BafangError(
            "the drive unit acknowledged the speed parameter write but its "
            f"32/03 broadcast still reads {last.hex() if last else 'nothing'} "
            f"rather than {payload.hex()}. The value was not applied."
        )

    # -- live data -------------------------------------------------------

    def passive_realtime(self) -> BroadcastRealtime:
        """Realtime values taken from broadcasts instead of asked for.

        Some firmware answers no realtime read at all and instead pushes the
        same payloads onto the bus unsolicited. Measured on a CR X210.350.FC:
        ``probe`` reports ControllerRealtime0/1 and SensorRealtime as
        unsupported, while all three arrive continuously as broadcasts. This
        reads that stream, so :meth:`realtime` and this method cover the two
        ways a system can offer the same data.
        """
        return BroadcastRealtime(self.client)

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

    #: Devices asked for the stored fault log, in order, when none is named.
    #:
    #: The drive unit is asked first because that is where the fault
    #: originates. But on a `CR X210.350.FC` it does not answer `60/07` at all,
    #: while the `DP C340.CAN` on the same bus answers with the whole history --
    #: which is how a stored "Over voltage protection" was found on a bike this
    #: tool had reported as having no readable fault log. Asking one device and
    #: giving up hid a fault that was sitting there the whole time.
    ERROR_SOURCES: ClassVar[tuple[DeviceId, ...]] = (
        DeviceId.DRIVE_UNIT,
        DeviceId.DISPLAY,
    )

    def errors(
        self, device: DeviceId | None = None
    ) -> list[tuple[int, str, str]]:
        """Stored fault codes, with descriptions.

        With no ``device``, every node in :attr:`ERROR_SOURCES` is asked and
        the first that answers wins. Name a device to ask only that one.
        """
        codes, _ = self.errors_with_source(device)
        return codes

    def errors_with_source(
        self, device: DeviceId | None = None
    ) -> tuple[list[tuple[int, str, str]], DeviceId | None]:
        """As :meth:`errors`, and which device the log came from.

        Worth reporting: "the display holds a stored overvoltage fault" and
        "the drive unit holds one" are different situations, and a log read
        from a display that has outlived several controllers may describe a
        fault the current motor never had.
        """
        candidates = (device,) if device is not None else self.ERROR_SOURCES
        last: BafangError | None = None
        for candidate in candidates:
            try:
                message = self.client.read(candidate, READ["ErrorCode"])
            except BafangError as exc:
                last = exc
                continue
            codes = codecs.decode_error_codes(message.data)
            return [(code, *error_text(code)) for code in codes], candidate
        assert last is not None
        raise last

    def clear_errors(self, device: DeviceId = DeviceId.DRIVE_UNIT) -> BafangMessage | None:
        return self.client.write_short(device, WRITE["ClearErrorCodes"], b"\x00")

    #: Display blocks worth reading, and the codec for each.
    #:
    #: Both have had decoders and differential tests against the vendored
    #: JavaScript since this project started, and nothing ever asked a device
    #: for them -- so a readable odometer sat unreachable behind a codec that
    #: was already proven correct. On a bus whose drive unit answers no
    #: parameter block at all, this is the only stored configuration anything
    #: will hand over.
    DISPLAY_BLOCKS: ClassVar[tuple[tuple[str, Any], ...]] = (
        ("DisplayDataBlock1", codecs.DisplayData1),
        ("DisplayDataBlock2", codecs.DisplayData2),
        ("DisplayAutoShutdownTime", codecs.DisplayAutoShutdown),
        ("DisplayLightLevels", codecs.DisplayLightLevels),
    )

    def display_data(self) -> dict[str, Any]:
        """Read what the display has stored, skipping what it will not answer.

        A `DP C340.C` answers ``63/01`` and is silent on ``63/02``, so a
        partial result is the normal case rather than a failure.
        """
        out: dict[str, Any] = {}
        for name, codec in self.DISPLAY_BLOCKS:
            try:
                message = self.client.read(DeviceId.DISPLAY, READ[name])
            except BafangError as exc:
                out[name] = f"not readable: {exc}"
                continue
            try:
                out[name] = codec.decode(message.data).to_dict()
            except codecs.DecodeError as exc:
                out[name] = f"not decodable: {exc}"
        return out

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
