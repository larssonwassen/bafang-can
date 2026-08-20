"""Link quality: what a capture actually received, as opposed to what it says.

A Bafang bus carries enough redundancy to tell a complete recording from a
damaged one without a reference to compare against. Both captures in this
repository turned out to be damaged, in three different ways, and nothing
reported any of it at the time:

* **Lost frames.** The torque sensor stamps a rolling counter into byte 3 of
  its ``31/00`` broadcast. Every gap in that sequence is a frame that reached
  the transceiver and never reached the log. One capture here lost 53% of them.

* **Corrupt frames.** An slcan adapter that drops a serial byte resynchronises
  mid-frame and emits an identifier wider than the 29 bits CAN has;
  python-can's slcan backend parses the hex without a range check.
  :meth:`BafangId.decode` masks such an identifier down to 29 bits, which
  turns line noise into a plausible-looking message from a device that does
  not exist. Five frames in one capture here are of this kind.

* **Impossible timing.** CAN is synchronous, so a frame cannot arrive sooner
  than the shortest frame takes to clock out. Timestamps closer together than
  that are host read times from a drained buffer, not arrival times -- the
  whole recording is real, but its timing is fiction. One capture here is
  compressed 54x this way.

* **Bus errors.** The adapter reports a malformed frame on the wire as an
  error frame, which is not a message from any node and decodes as garbage if
  treated as one. This tool used to throw them away as corrupt identifiers --
  and with them the only direct evidence the bus has of its own electrical
  health. On the ride in ``docs/m200.md`` all eight of them arrive inside the
  two seconds of highest motor current.

None of these are Bafang-specific beyond the counter offset, and none of them
need a second recording to detect.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constants import BITRATE, DeviceId
from .frame import CAN_EFF_MASK, BafangId

#: Bits on the wire for the shortest possible CAN 2.0B extended frame: SOF 1,
#: identifier 11 + SRR 1 + IDE 1 + 18, RTR 1, r1 r0 2, DLC 4, CRC 15, CRC
#: delimiter 1, ACK slot + delimiter 2, EOF 7, inter-frame space 3. Bit
#: stuffing only ever makes a frame longer, and a payload only adds to it, so
#: no real frame can be shorter than this.
SHORTEST_FRAME_BITS = 67

#: (source, code, subcode) -> payload offset of a rolling message counter.
#: Only the torque sensor is known to carry one. It is a plain 8-bit counter
#: that wraps at 0xFF, incrementing once per broadcast.
SEQUENCE_COUNTERS: dict[tuple[int, int, int], int] = {
    (0x01, 0x31, 0x00): 3,
}

#: Node addresses that exist on a Bafang bus. A frame from or to anything else
#: is suspicious but not provably corrupt, so it is counted and reported, never
#: dropped -- an unlisted address could be a device this tool does not know.
KNOWN_DEVICES = frozenset(int(d) for d in DeviceId)

#: SocketCAN's error-frame flag. An error frame is not a message from a node:
#: it is the CAN controller reporting that a frame on the wire was malformed,
#: that arbitration was lost, or that the controller itself is in trouble. The
#: error classes ride in the low 29 bits and the detail in the payload.
CAN_ERR_FLAG = 0x2000_0000

CAN_ERR_TX_TIMEOUT = 0x0000_0001
CAN_ERR_LOSTARB = 0x0000_0002
CAN_ERR_CRTL = 0x0000_0004
CAN_ERR_PROT = 0x0000_0008
CAN_ERR_TRX = 0x0000_0010
CAN_ERR_ACK = 0x0000_0020
CAN_ERR_BUSOFF = 0x0000_0040
CAN_ERR_BUSERROR = 0x0000_0080
CAN_ERR_RESTARTED = 0x0000_0100
CAN_ERR_CNT = 0x0000_0200

#: The one identifier ``can.CanutilsLogWriter`` writes for an error frame,
#: whatever the frame actually said::
#:
#:     if msg.is_error_frame:
#:         framestr += f" {CAN_ERR_FLAG | CAN_ERR_BUSERROR:08X}#"
#:
#: So in a candump log this value is python-can's stamp, not the controller's
#: error class -- the class does not survive the format, and the reader that
#: parses it back drops the payload as well. The payload *is* preserved on the
#: way out, which is why this module reads candump text directly.
#:
#: A real Bafang frame cannot collide with it: the low 29 bits decode to source
#: 0, which is not a device on the bus.
CAN_ERROR_STAMP = CAN_ERR_FLAG | CAN_ERR_BUSERROR

#: data[2] of an error frame, when the ``CAN_ERR_PROT`` class is set.
PROTOCOL_ERRORS: dict[int, str] = {
    0x00: "unspecified",
    0x01: "single bit error",
    0x02: "frame format error",
    0x04: "bit stuffing error",
    0x08: "dominant bit where recessive was expected",
    0x10: "recessive bit where dominant was expected",
    0x20: "bus overload",
    0x40: "active error announcement",
    0x80: "error while transmitting",
}

#: data[1] of an error frame, when the ``CAN_ERR_CRTL`` class is set.
CONTROLLER_ERRORS: dict[int, str] = {
    0x01: "receive buffer overflow",
    0x02: "transmit buffer overflow",
    0x04: "receive error counter in the warning band",
    0x08: "transmit error counter in the warning band",
    0x10: "receiver has gone error-passive",
    0x20: "transmitter has gone error-passive",
    0x40: "back to error-active",
}

#: Error classes, for reporting. Ordered worst first.
ERROR_CLASSES: list[tuple[int, str]] = [
    (CAN_ERR_BUSOFF, "bus off"),
    (CAN_ERR_TRX, "transceiver fault"),
    (CAN_ERR_PROT, "protocol violation"),
    (CAN_ERR_ACK, "no acknowledgement"),
    (CAN_ERR_CRTL, "controller status"),
    (CAN_ERR_LOSTARB, "arbitration lost"),
    (CAN_ERR_TX_TIMEOUT, "transmit timeout"),
    (CAN_ERR_BUSERROR, "bus error"),
    (CAN_ERR_RESTARTED, "controller restarted"),
]


@dataclass
class BusError:
    """One error frame: the controller reporting trouble on the wire.

    ``classes`` is ``None`` when the source could not preserve it -- a candump
    log stamps every error frame with the same identifier, so offline the only
    honest answer about the error class is that it is unknown. The payload
    still carries the protocol error and the error counters.
    """

    timestamp: float
    classes: int | None
    data: bytes

    @property
    def protocol_error(self) -> int | None:
        return self.data[2] if len(self.data) > 2 else None

    @property
    def controller_error(self) -> int | None:
        return self.data[1] if len(self.data) > 1 else None

    @property
    def counters(self) -> tuple[int, int] | None:
        """``(transmit, receive)`` error counters, or None.

        The kernel only guarantees these when ``CAN_ERR_CNT`` is set, and that
        bit is one of the things a candump log destroys. They are reported
        where the payload is long enough to hold them, and the caller should
        read them as the adapter's claim rather than as a promise.
        """
        if len(self.data) < 8:
            return None
        return self.data[6], self.data[7]

    def describe(self) -> str:
        parts: list[str] = []
        if self.classes is not None:
            named = [name for bit, name in ERROR_CLASSES if self.classes & bit]
            parts.append(", ".join(named) if named else "unspecified")
        protocol = self.protocol_error
        if protocol is not None and (self.classes is None or protocol):
            parts.append(PROTOCOL_ERRORS.get(protocol, f"protocol {protocol:#04x}"))
        controller = self.controller_error
        if controller:
            parts.append(
                CONTROLLER_ERRORS.get(controller, f"controller {controller:#04x}")
            )
        counters = self.counters
        if counters and any(counters):
            parts.append(f"error counters tx {counters[0]} rx {counters[1]}")
        return "; ".join(parts) if parts else "unspecified"


def shortest_frame_time(bitrate: int = BITRATE) -> float:
    """Seconds the shortest extended frame occupies the bus."""
    return SHORTEST_FRAME_BITS / bitrate


@dataclass
class SequenceGap:
    key: tuple[int, int, int]
    previous: int
    current: int
    missing: int
    elapsed: float


@dataclass
class LinkQuality:
    """Counts what a stream of raw CAN frames says about its own integrity.

    Feed it every frame, in order, with :meth:`observe`. It is deliberately
    cheap enough to run on the receive path of a live capture.
    """

    bitrate: int = BITRATE

    frames: int = 0
    invalid_ids: int = 0
    implausible_devices: int = 0
    duplicates: int = 0
    too_fast: int = 0
    counted: int = 0
    lost: int = 0
    bus_errors: int = 0
    gaps: list[SequenceGap] = field(default_factory=list)
    errors: list[BusError] = field(default_factory=list)

    _last_counter: dict[tuple[int, int, int], int] = field(
        default_factory=dict, repr=False
    )
    _last_time: dict[tuple[int, int, int], float] = field(
        default_factory=dict, repr=False
    )
    _previous_timestamp: float | None = field(default=None, repr=False)

    #: Gaps are listed, not just counted, but a capture that lost thousands of
    #: frames should not produce thousands of lines.
    MAX_LISTED_GAPS = 20

    #: Error frames come in bursts -- eight in two seconds in the ride this was
    #: written for -- and a bus that is failing continuously would otherwise
    #: fill the report with the same line.
    MAX_LISTED_ERRORS = 20

    def observe(
        self,
        can_id: int,
        data: bytes,
        timestamp: float = 0.0,
        is_error_frame: bool = False,
    ) -> bool:
        """Record one raw frame. Returns False if it should not be trusted.

        A False return means the frame is not a message from a node, so the
        caller should not decode it: either the identifier could not have come
        off a CAN bus, or the frame is the controller reporting an error.

        ``is_error_frame`` is what a live driver says. Offline there is no such
        flag, so :data:`CAN_ERROR_STAMP` stands in for it -- see that constant
        for why an exact match is the right test and not a mask.
        """
        self.frames += 1

        if is_error_frame or can_id == CAN_ERROR_STAMP:
            # Live, the class bits survive in the identifier because python-can
            # masks off only the flag above them. Offline they do not survive
            # at all, and claiming a class we did not read would be worse than
            # saying so.
            classes = can_id & CAN_EFF_MASK if is_error_frame else None
            self.bus_errors += 1
            if len(self.errors) < self.MAX_LISTED_ERRORS:
                self.errors.append(BusError(timestamp, classes, bytes(data)))
            return False

        if can_id > CAN_EFF_MASK or can_id < 0:
            self.invalid_ids += 1
            return False

        if self._previous_timestamp is not None and timestamp:
            delta = timestamp - self._previous_timestamp
            if 0 <= delta < shortest_frame_time(self.bitrate):
                self.too_fast += 1
        if timestamp:
            self._previous_timestamp = timestamp

        ident = BafangId.decode(can_id)
        if (
            ident.source not in KNOWN_DEVICES
            or ident.target not in KNOWN_DEVICES
        ):
            # Corruption that happens to land inside 29 bits looks like a valid
            # frame from a device that is not on the bus. Two frames in
            # captures/display-interaction-2026-08-17.log are of this kind and
            # no range check can catch them.
            self.implausible_devices += 1
        key = (ident.source, ident.code, ident.subcode)
        offset = SEQUENCE_COUNTERS.get(key)
        if offset is None or len(data) <= offset:
            return True

        self.counted += 1
        value = data[offset]
        previous = self._last_counter.get(key)
        self._last_counter[key] = value
        last_time = self._last_time.get(key, timestamp)
        self._last_time[key] = timestamp
        if previous is None:
            return True

        step = (value - previous) % 256
        if step == 0:
            self.duplicates += 1
        elif step > 1:
            missing = step - 1
            self.lost += missing
            if len(self.gaps) < self.MAX_LISTED_GAPS:
                self.gaps.append(
                    SequenceGap(
                        key=key,
                        previous=previous,
                        current=value,
                        missing=missing,
                        elapsed=timestamp - last_time,
                    )
                )
        return True

    # -- reporting ------------------------------------------------------

    @property
    def loss_ratio(self) -> float | None:
        """Fraction of counted broadcasts that never arrived.

        ``None`` when nothing carrying a counter was seen, which is the honest
        answer: a capture with no sequenced broadcast in it cannot be checked
        this way, and reporting 0% would claim otherwise.
        """
        expected = self.counted + self.lost
        return self.lost / expected if expected else None

    @property
    def healthy(self) -> bool:
        """True when nothing about this stream contradicts itself.

        Bus errors count against it. Every other item here is damage between
        the wire and the log; an error frame is damage on the wire itself, and
        a frame destroyed there never reaches any recorder to be missed.
        """
        return not (
            self.lost
            or self.invalid_ids
            or self.too_fast
            or self.implausible_devices
            or self.bus_errors
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "frames": self.frames,
            "invalid_ids": self.invalid_ids,
            "frames_from_unknown_devices": self.implausible_devices,
            "sequenced_frames": self.counted,
            "lost_frames": self.lost,
            "duplicate_frames": self.duplicates,
            "frames_faster_than_the_bus": self.too_fast,
            "bus_errors": self.bus_errors,
        }
        ratio = self.loss_ratio
        if ratio is not None:
            out["loss_percent"] = round(ratio * 100, 1)
        if self.errors:
            out["errors"] = [
                {
                    "at": round(error.timestamp, 3),
                    "detail": error.describe(),
                    "raw": error.data.hex(),
                }
                for error in self.errors
            ]
        if self.gaps:
            out["gaps"] = [
                {
                    "source": f"{gap.key[0]:#04x}",
                    "command": f"{gap.key[1]:#04x}/{gap.key[2]:#04x}",
                    "counter": f"{gap.previous:02X}->{gap.current:02X}",
                    "missing": gap.missing,
                    "elapsed_ms": round(gap.elapsed * 1000, 2),
                }
                for gap in self.gaps
            ]
        return out

    def warnings(self) -> list[str]:
        """Plain-language problems, worst first. Empty when the link is clean."""
        out: list[str] = []
        if self.bus_errors:
            detail = ""
            if self.errors:
                span = self.errors[-1].timestamp - self.errors[0].timestamp
                kinds = sorted({error.describe() for error in self.errors})
                detail = (
                    f" They span {span:.1f} s and report: "
                    + "; ".join(kinds)
                    + "."
                )
            out.append(
                f"{self.bus_errors} error frames: the CAN controller reporting "
                "that a frame on the wire was malformed, not a message from a "
                f"node.{detail} Frames destroyed this way are gone before any "
                "recorder sees them, so they cannot show up as loss. On this "
                "bike they arrive with motor current -- check what the drive "
                "unit was drawing at those timestamps before suspecting the "
                "adapter."
            )
        ratio = self.loss_ratio
        if ratio:
            out.append(
                f"{self.lost} of {self.counted + self.lost} sequenced broadcasts "
                f"never arrived ({ratio * 100:.1f}% lost). The torque sensor "
                "numbers its 31/00 broadcasts and this many numbers are missing, "
                "so the frames reached the transceiver and were dropped between "
                "there and here. Anything derived from this stream is partial."
            )
        if self.invalid_ids:
            out.append(
                f"{self.invalid_ids} frames carried an identifier wider than the "
                "29 bits CAN has, so they are corrupt and were dropped. On slcan "
                "that is a lost serial byte resynchronising mid-frame; the real "
                "frames it destroyed are gone too. Error frames are not counted "
                "here -- those are a working adapter reporting a failing bus, "
                "and they are reported separately."
            )
        if self.too_fast:
            floor = shortest_frame_time(self.bitrate)
            out.append(
                f"{self.too_fast} frames are timestamped less than "
                f"{floor * 1e6:.0f} us after the one before, which is shorter "
                f"than the shortest frame at {self.bitrate // 1000} kbit/s. "
                "These are host read times from a drained buffer, not arrival "
                "times: the frames are real but their timing is not."
            )
        if self.implausible_devices:
            out.append(
                f"{self.implausible_devices} frames name a source or target "
                "that is not a device on a Bafang bus. Corruption that lands "
                "inside 29 bits is indistinguishable from a real frame, so "
                "these were decoded rather than dropped -- but treat them as "
                "noise unless this bus has a node the tool does not know."
            )
        if self.duplicates:
            out.append(
                f"{self.duplicates} broadcasts repeated a counter value they had "
                "already used, which means the same frame was recorded twice."
            )
        return out


def analyse(messages: Any, bitrate: int = BITRATE) -> LinkQuality:
    """Run :class:`LinkQuality` over python-can messages from a log."""
    quality = LinkQuality(bitrate=bitrate)
    for message in messages:
        error = bool(getattr(message, "is_error_frame", False))
        # An error frame does not carry the extended-id flag -- it is not an
        # addressed frame at all -- so it has to be tested for before the
        # extended-only filter, or it is silently skipped.
        if not error and not getattr(message, "is_extended_id", False):
            continue
        quality.observe(
            message.arbitration_id,
            bytes(message.data),
            message.timestamp,
            is_error_frame=error,
        )
    return quality

#: ``(timestamp) channel ID#DATA [R|T]`` -- the candump text format that
#: ``sniff`` writes and ``import-capture`` reads.
_CANDUMP = re.compile(
    r"^\((?P<ts>[\d.]+)\)\s+(?P<chan>\S+)\s+(?P<id>[0-9A-Fa-f]+)#(?P<data>[0-9A-Fa-f]*)"
)


def iter_candump(path: str | Path) -> Iterator[tuple[int, bytes, float]]:
    """Yield ``(can_id, data, timestamp)`` from a candump log, unmasked.

    python-can's reader for this format masks the identifier down to 29 bits
    before handing it over, which is exactly the corruption
    :class:`LinkQuality` needs to see. Reading the text directly is the only
    way to keep it. Lines that are not frames are skipped.
    """
    for line in Path(path).read_text().splitlines():
        match = _CANDUMP.match(line.strip())
        if match is None:
            continue
        data = match["data"]
        if len(data) % 2:  # a truncated frame, not a decodable payload
            data = data[:-1]
        yield int(match["id"], 16), bytes.fromhex(data), float(match["ts"])


def analyse_log(path: str | Path, bitrate: int = BITRATE) -> LinkQuality:
    """Run :class:`LinkQuality` over a recorded log file.

    candump logs are parsed here rather than through python-can so that a
    corrupt identifier survives to be counted; every other format goes through
    python-can, where the reader has already masked it and only frame loss and
    impossible timing can still be detected.
    """
    quality = LinkQuality(bitrate=bitrate)
    if str(path).endswith(".log"):
        for can_id, data, timestamp in iter_candump(path):
            quality.observe(can_id, data, timestamp)
        return quality

    import can

    with can.LogReader(str(path)) as reader:
        return analyse(reader, bitrate=bitrate)


def iter_frames(path: str | Path) -> Iterator[tuple[int, bytes, float]]:
    """Yield ``(can_id, data, timestamp)`` from any log python-can can read.

    candump logs go through :func:`iter_candump` so that a corrupt identifier
    arrives intact and can be rejected; every other format goes through
    python-can, whose readers have already masked it. Extended frames only --
    a Bafang bus carries nothing else.
    """
    if str(path).endswith(".log"):
        yield from iter_candump(path)
        return

    import can

    with can.LogReader(str(path)) as reader:
        for message in reader:
            if not message.is_extended_id:
                continue
            yield message.arbitration_id, bytes(message.data), message.timestamp
