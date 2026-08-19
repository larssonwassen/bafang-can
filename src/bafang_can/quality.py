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
    gaps: list[SequenceGap] = field(default_factory=list)

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

    def observe(
        self, can_id: int, data: bytes, timestamp: float = 0.0
    ) -> bool:
        """Record one raw frame. Returns False if it should not be trusted.

        A False return means the identifier could not have come off a CAN bus,
        so the caller should drop the frame rather than decode it.
        """
        self.frames += 1

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
        return not (
            self.lost
            or self.invalid_ids
            or self.too_fast
            or self.implausible_devices
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
        }
        ratio = self.loss_ratio
        if ratio is not None:
            out["loss_percent"] = round(ratio * 100, 1)
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
                "frames it destroyed are gone too."
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
        if not getattr(message, "is_extended_id", False):
            continue
        quality.observe(
            message.arbitration_id, bytes(message.data), message.timestamp
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
