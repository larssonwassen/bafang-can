"""Device profiles: recording a real bike and replaying it in the simulator."""

from __future__ import annotations

import json
from pathlib import Path

import can
import pytest

from bafang_can.capture import DeviceProfile, assemble, profile_from_log
from bafang_can.cli import main
from bafang_can.constants import CanOperation, DeviceId
from bafang_can.frame import BafangId, checksum
from bafang_can.protocol import BafangClient
from bafang_can.simulator import SimBus
from bafang_can.system import BafangSystem

SIM = ["--interface", "sim"]


def message(source, target, operation, code, subcode, data=b"", timestamp=1.0):
    return can.Message(
        arbitration_id=BafangId(
            source=int(source),
            target=int(target),
            operation=int(operation),
            code=code,
            subcode=subcode,
        ).encode(),
        data=data,
        is_extended_id=True,
        timestamp=timestamp,
    )


def block(fill: int = 0x5A) -> bytes:
    body = bytes([fill] * 63)
    return body + bytes([checksum(body)])


# -- reassembling a passive capture -------------------------------------


def test_assemble_reconstructs_a_multiframe_answer():
    payload = block()
    frames = [
        message(DeviceId.TOOL, DeviceId.DRIVE_UNIT, CanOperation.READ_CMD, 0x60, 0x11),
        message(
            DeviceId.DRIVE_UNIT, DeviceId.TOOL, CanOperation.MULTIFRAME_START,
            0x60, 0x11, bytes([len(payload)]),
        ),
    ]
    rest = payload
    sequence = 0
    while len(rest) > 8:
        frames.append(
            message(
                DeviceId.DRIVE_UNIT, DeviceId.TOOL, CanOperation.MULTIFRAME,
                0x60, sequence, rest[:8],
            )
        )
        rest = rest[8:]
        sequence += 1
    frames.append(
        message(
            DeviceId.DRIVE_UNIT, DeviceId.TOOL, CanOperation.MULTIFRAME_END,
            0x60, sequence, rest,
        )
    )

    answers = list(assemble(frames))
    assert len(answers) == 1
    assert answers[0].multiframe
    assert answers[0].data == payload


def test_assemble_ignores_bare_acknowledgements():
    frames = [
        message(DeviceId.DRIVE_UNIT, DeviceId.TOOL, CanOperation.NORMAL_ACK, 0x60, 0x03, b"\x00"),
        message(
            DeviceId.DRIVE_UNIT, DeviceId.TOOL, CanOperation.NORMAL_ACK,
            0x60, 0x03, b"DP123\x00",
        ),
    ]
    answers = list(assemble(frames))
    assert [a.data for a in answers] == [b"DP123\x00"]


def test_assemble_drops_an_unterminated_transfer():
    frames = [
        message(
            DeviceId.DRIVE_UNIT, DeviceId.TOOL, CanOperation.MULTIFRAME_START,
            0x60, 0x11, bytes([64]),
        ),
        message(DeviceId.DRIVE_UNIT, DeviceId.TOOL, CanOperation.MULTIFRAME, 0x60, 0, b"12345678"),
    ]
    assert list(assemble(frames)) == []


def test_profile_from_log_roundtrip(tmp_path):
    path = tmp_path / "ride.log"
    with can.CanutilsLogWriter(str(path), channel="can0") as writer:
        writer.on_message_received(
            message(
                DeviceId.DRIVE_UNIT, DeviceId.TOOL, CanOperation.NORMAL_ACK,
                0x60, 0x02, b"M200.G210\x00",
            )
        )
        writer.on_message_received(
            message(
                DeviceId.BATTERY, DeviceId.DISPLAY, CanOperation.NORMAL_ACK,
                0x34, 0x01, bytes([0x10, 0x00, 0x10, 0x0E, 60]),
            )
        )

    profile = profile_from_log(path)
    assert profile.responses[f"{int(DeviceId.DRIVE_UNIT)}:96:2"] == b"M200.G210\x00".hex()
    # Traffic between the bike's own nodes is captured too.
    assert f"{int(DeviceId.BATTERY)}:52:1" in profile.responses


# -- profile files --------------------------------------------------------


def test_anonymize_blanks_only_identifying_fields():
    profile = DeviceProfile()
    profile.record(int(DeviceId.DRIVE_UNIT), 0x60, 0x03, b"DP12345678\x00")  # serial
    profile.record(int(DeviceId.DRIVE_UNIT), 0x60, 0x02, b"M200.G210\x00")  # model
    profile.anonymize()

    blocks = profile.blocks()
    assert bytes(blocks[(int(DeviceId.DRIVE_UNIT), 0x60, 0x03)]).startswith(b"REDACTED")
    assert bytes(blocks[(int(DeviceId.DRIVE_UNIT), 0x60, 0x02)]) == b"M200.G210\x00"
    assert profile.anonymized is True


def test_profile_rejects_a_foreign_file(tmp_path):
    path = tmp_path / "not-a-profile.json"
    path.write_text(json.dumps({"format": "something-else"}))
    with pytest.raises(ValueError, match="not a device profile"):
        DeviceProfile.load(path)


def test_profile_rejects_a_newer_version(tmp_path):
    path = tmp_path / "future.json"
    path.write_text(
        json.dumps({"format": "bafang-can-device-profile", "version": 99, "responses": {}})
    )
    with pytest.raises(ValueError, match="newer version"):
        DeviceProfile.load(path)


# -- capture and replay ----------------------------------------------------


def test_capture_records_what_the_bike_answers():
    bus = SimBus(chatter=False)
    with BafangClient(bus, timeout=1.0) as client:
        profile = BafangSystem(client).capture(timeout=0.5)

    # The identity and the configuration blocks are all there...
    assert f"{int(DeviceId.DRIVE_UNIT)}:96:17" in profile.responses  # Parameter1
    assert f"{int(DeviceId.DISPLAY)}:96:2" in profile.responses  # display model
    # ...and nothing was invented for commands the device refuses.
    assert f"{int(DeviceId.DRIVE_UNIT)}:96:23" not in profile.responses  # 0x60/0x17


def test_simulator_answers_from_a_profile(tmp_path):
    profile = DeviceProfile(source="test")
    profile.record(int(DeviceId.DRIVE_UNIT), 0x60, 0x02, b"REAL.MOTOR\x00")
    profile.record(int(DeviceId.DRIVE_UNIT), 0x60, 0x11, block(0x33))
    path = tmp_path / "profile.json"
    profile.save(path)

    bus = SimBus(chatter=False, profile=str(path))
    with BafangClient(bus, timeout=1.0) as client:
        system = BafangSystem(client)
        assert system.info(DeviceId.DRIVE_UNIT).fields["ModelNumber"] == "REAL.MOTOR"
        assert system.read_block("Parameter1").raw == block(0x33)
        # Anything the profile does not cover keeps its default.
        assert system.info(DeviceId.DISPLAY).fields["ModelNumber"] == "DP C01"

    assert bus.is_recorded(int(DeviceId.DRIVE_UNIT), 0x60, 0x11)
    assert not bus.is_recorded(int(DeviceId.DISPLAY), 0x60, 0x02)


def test_recorded_live_data_wins_over_synthesized(tmp_path):
    profile = DeviceProfile(source="test")
    frozen = bytes([0xE8, 0x03, 0x64, 0x00, 0x10, 0x0E, 0x3C, 0xFF])
    profile.record(int(DeviceId.DRIVE_UNIT), 0x32, 0x01, frozen)
    path = tmp_path / "profile.json"
    profile.save(path)

    bus = SimBus(chatter=False, profile=str(path))
    with BafangClient(bus, timeout=1.0) as client:
        system = BafangSystem(client)
        first = system.realtime()["controller1"]
        second = system.realtime()["controller1"]
    assert first.speed == second.speed == 10.0  # replayed, so it does not move


def test_capture_and_replay_through_the_cli(tmp_path, capsys):
    path = tmp_path / "profile.json"
    assert main([*SIM, "capture", "-o", str(path), "--anonymize"]) == 0
    capsys.readouterr()

    assert main([*SIM, "profile", str(path), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["anonymized"] is True
    assert summary["responses"] > 20

    assert main([*SIM, "--sim-profile", str(path), "info", "--device", "drive_unit", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["DRIVE_UNIT"]["SerialNumber"] == "REDACTED"
    assert data["DRIVE_UNIT"]["ModelNumber"] == "M200.G210"


def test_import_capture_reports_an_empty_log(tmp_path, capsys):
    path = tmp_path / "empty.log"
    path.write_text("")
    assert main(["import-capture", str(path), "-o", str(tmp_path / "out.json")]) == 2
    assert "No complete answers" in capsys.readouterr().out


def test_sniff_records_a_log_that_import_capture_can_read(tmp_path, capsys):
    log = tmp_path / "ride.log"
    assert main([*SIM, "sniff", "--seconds", "1.5", "-o", str(log), "--quiet"]) == 0
    capsys.readouterr()
    assert log.stat().st_size > 0

    out = tmp_path / "profile.json"
    assert main(["import-capture", str(log), "-o", str(out)]) == 0
    capsys.readouterr()

    profile = DeviceProfile.load(out)
    assert profile.responses
    assert profile.source.startswith("log: ")


# ---------------------------------------------------------------------------
# corrupt frames must not become recorded answers
# ---------------------------------------------------------------------------


def test_an_identifier_wider_than_29_bits_is_not_assembled():
    """0x2F830100 came off a real capture. It is line noise, not a device.

    BafangId.decode masks to 29 bits, which would turn this into a NORMAL_ACK
    from device 0x0f -- recorded into a profile and replayed by the simulator
    as though a bike had sent it.
    """
    corrupt = can.Message(
        arbitration_id=0x2F830100,
        data=bytes.fromhex("04d40f"),
        is_extended_id=True,
        timestamp=1.0,
    )

    assert list(assemble([corrupt])) == []


def test_a_corrupt_frame_does_not_displace_the_good_one_beside_it():
    good = message(
        DeviceId.BATTERY, DeviceId.TOOL, CanOperation.NORMAL_ACK, 0x64, 0x01,
        data=bytes.fromhex("0b0062175a02"),
    )
    corrupt = can.Message(
        arbitration_id=0x28308000, data=b"", is_extended_id=True, timestamp=1.0
    )

    recovered = list(assemble([corrupt, good, corrupt]))

    assert len(recovered) == 1
    assert recovered[0].data == bytes.fromhex("0b0062175a02")


def test_a_profile_built_from_a_damaged_capture_still_reports_the_damage(tmp_path):
    """The profile is built from what survived; the warning says what did not."""
    from bafang_can.quality import analyse_log

    fixture = Path(__file__).parent / "data" / "display-interaction-excerpt.log"
    quality = analyse_log(fixture)

    assert quality.lost == 12
    assert not quality.healthy
    # And the frames that did survive are still usable.
    assert profile_from_log(fixture).responses
