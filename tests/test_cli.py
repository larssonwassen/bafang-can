"""End-to-end CLI tests against the built-in simulator.

These run the real argument parser, the real protocol stack and the real
codecs; only the CAN adapter is simulated. That is the part that cannot be
exercised without hardware, so everything else stays covered.
"""

from __future__ import annotations

import json

import pytest

from bafang_can.cli import main

SIM = ["--interface", "sim", "--sim-errors", "8,21"]


def run(*args: str) -> int:
    return main([*SIM, *args])


def test_scan_finds_every_simulated_node(capsys):
    assert run("scan") == 0
    out = capsys.readouterr().out
    for name in ("DRIVE_UNIT", "DISPLAY", "TORQUE_SENSOR", "BATTERY"):
        assert f"{name}: present" in out


def test_info_json_is_machine_readable(capsys):
    assert run("info", "--device", "drive_unit", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["DRIVE_UNIT"]["ModelNumber"] == "M200.G210"


def test_probe_marks_unsupported_commands(capsys):
    assert run("probe", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["Parameter1"] == "answered"
    assert data["Parameter6017"] == "refused"


def test_probe_separates_a_refusal_from_silence(capsys):
    """ERROR_ACK and no answer are different claims about the firmware.

    A unit that answers identity reads and nothing else raises the question of
    whether the rest is missing or present and blocked. Only this distinction
    can settle it, and reporting both as "unsupported" throws it away.
    """
    assert run("probe", "--json") == 0
    data = json.loads(capsys.readouterr().out)

    assert data["Parameter6017"] == "refused"
    assert data["ControlUnassigned"] == "silent"


def test_probe_says_what_a_refusal_implies(capsys):
    assert run("probe") == 0
    out = capsys.readouterr().out

    assert "a command that does not exist was silent" in out
    assert "has handlers for them and is declining" in out
    # The named list is what carries the claim, so check membership rather
    # than the whole string -- the command table grows.
    declined = out.split("declining: ", 1)[1]
    assert "Parameter6017" in declined


def test_the_control_command_is_not_counted_as_a_capability(capsys):
    """It is a measurement of the bus, not a command anyone wants."""
    assert run("probe", "--json") == 0
    total = len(json.loads(capsys.readouterr().out))

    assert run("probe") == 0
    assert f"/{total - 1} commands answered" in capsys.readouterr().out


def test_probe_declines_to_infer_blocking_when_nothing_distinguishes_them(capsys):
    """If unanswered commands look exactly like nonexistent ones, say so.

    This is the branch that matters for the real bike: claiming a firmware is
    "locked" when every unanswered command behaves like an absent one would be
    a confident wrong answer to the question the probe exists to settle.
    """
    from bafang_can.cli import _probe_verdict
    from bafang_can.constants import DeviceId

    verdict = _probe_verdict(
        {
            "HardwareVersion": "answered",
            "Parameter1": "silent",
            "ControlUnassigned": "silent",
        },
        DeviceId.DRIVE_UNIT,
    )

    assert "nothing here suggests they are implemented and blocked" in verdict
    assert "declining" not in verdict


def test_diagnose_reports_errors_and_checklist(capsys):
    assert run("diagnose", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert [entry["code"] for entry in data["errors"]] == [8, 21]
    assert data["parameter1_checksum_ok"] is True
    assert len(data["checklist"]) >= 5


def test_errors_are_not_cleared_without_apply(capsys):
    assert run("errors", "--clear", "-y") == 0
    out = capsys.readouterr().out
    assert "NOT cleared" in out


def test_set_is_a_dry_run_by_default(capsys):
    assert run("set", "Parameter1.current_limit=12") == 0
    out = capsys.readouterr().out
    assert "15 -> 12" in out
    assert "Dry run" in out


def test_set_applies_and_verifies(capsys):
    assert run("-y", "set", "Parameter1.current_limit=12", "--apply") == 0
    out = capsys.readouterr().out
    assert "Verified" in out


def test_set_refuses_values_outside_the_profile(capsys):
    assert run("set", "Parameter1.current_limit=45", "--apply", "-y") == 1
    out = capsys.readouterr().out
    assert "exceeds the M200 profile limit" in out
    assert "Refusing to write" in out


def test_force_overrides_the_profile_check(capsys):
    assert run("set", "Parameter1.current_limit=45", "--force") == 0
    assert "Dry run" in capsys.readouterr().out


def test_set_rejects_a_malformed_assignment():
    with pytest.raises(SystemExit):
        run("set", "Parameter1.current_limit")


def test_nested_field_assignment(tmp_path, capsys):
    state = ["--sim-state", str(tmp_path / "bike.json")]
    assert main([*SIM, *state, "-y", "set",
                 "Parameter1.assist_levels.2.current_limit=33", "--apply"]) == 0
    capsys.readouterr()
    assert main([*SIM, *state, "get", "Parameter1.assist_levels.2", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["current_limit"] == 33


def test_wheel_change_roundtrips(tmp_path, capsys):
    state = ["--sim-state", str(tmp_path / "bike.json")]
    assert main([*SIM, *state, "-y", "wheel", "--diameter", "29",
                 "--circumference", "2300", "--apply"]) == 0
    capsys.readouterr()
    assert main([*SIM, *state, "get", "SpeedParameters", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["wheel"] == "29"
    assert data["circumference"] == 2300


def test_wheel_refuses_impossible_circumference(capsys):
    assert run("wheel", "--diameter", "29", "--circumference", "900", "--apply", "-y") == 1
    assert "circumference" in capsys.readouterr().out


def test_dump_and_restore_roundtrip(tmp_path, capsys):
    path = tmp_path / "backup.json"
    assert run("dump", "-o", str(path)) == 0
    capsys.readouterr()
    data = json.loads(path.read_text())
    assert len(bytes.fromhex(data["drive_unit"]["Parameter1"]["raw"])) == 64

    assert run("restore", str(path)) == 0
    out = capsys.readouterr().out
    assert "would write" in out
    assert "Dry run" in out

    assert run("-y", "restore", str(path), "--apply") == 0
    assert "Parameter1: written" in capsys.readouterr().out


def test_calibration_needs_apply(capsys):
    assert run("calibrate", "torque") == 0
    assert "Dry run" in capsys.readouterr().out
    assert run("-y", "calibrate", "torque", "--apply") == 0
    assert "acknowledged" in capsys.readouterr().out


def test_raw_read(capsys):
    assert run("raw", "0x62", "0xd9") == 0
    assert "62/d9" in capsys.readouterr().out


def test_unknown_command_reports_error_ack(capsys):
    assert run("raw", "0x60", "0x17") == 2
    assert "ERROR_ACK" in capsys.readouterr().err


def test_global_flags_work_after_the_subcommand(capsys):
    assert main(["scan", "--interface", "sim", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["DRIVE_UNIT"] == "present"


def test_decode_is_offline(capsys):
    assert main(["decode", "0x85110011", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["source"] == 5
    assert data["target"] == 2


def test_monitor_prints_live_lines(capsys):
    assert run("monitor", "--seconds", "0.3", "--interval", "0.1") == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines and "km/h" in lines[0]


def test_sniff_sees_foreign_traffic_without_acknowledging_it(capsys):
    assert main(["--interface", "sim", "sniff", "--seconds", "0.5", "--passive"]) == 0
    out = capsys.readouterr().out
    assert "DISPLAY->" in out


def test_simulator_state_survives_between_runs(tmp_path, capsys):
    """A rehearsal of the real workflow: back up, change, verify, restore."""
    state = ["--sim-state", str(tmp_path / "bike.json")]
    backup = tmp_path / "baseline.json"

    assert main([*SIM, *state, "dump", "-o", str(backup)]) == 0
    assert main([*SIM, *state, "-y", "set", "Parameter1.current_limit=12", "--apply"]) == 0
    capsys.readouterr()

    assert main([*SIM, *state, "get", "Parameter1.current_limit", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == 12

    assert main([*SIM, *state, "-y", "restore", str(backup), "--apply"]) == 0
    capsys.readouterr()
    assert main([*SIM, *state, "get", "Parameter1.current_limit", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == 15


def test_probe_admits_when_a_blanket_error_ack_makes_it_useless(capsys):
    """Some firmware ERROR_ACKs anything it does not know.

    On such a unit a refusal carries no information at all, and reading it as
    "the firmware knows this command and declined" would invent a finding.
    """
    from bafang_can.cli import _probe_verdict
    from bafang_can.constants import DeviceId

    verdict = _probe_verdict(
        {
            "HardwareVersion": "answered",
            "Parameter1": "refused",
            "ControlUnassigned": "refused",
        },
        DeviceId.DRIVE_UNIT,
    )

    assert "cannot tell blocked from absent" in verdict
    assert "declining" not in verdict


def test_scan_retries_before_calling_a_device_absent():
    """A single missed answer must not be reported as a missing device.

    `scan` lost a display that was plainly there on one run in six against a
    real bus, and `diagnose` turns an absent drive unit into "check CAN-H/CAN-L
    wiring and polarity" -- sending someone after a fault in a good harness.
    """
    from bafang_can.protocol import BafangClient, TimeoutError_

    attempts = []

    class FlakyClient(BafangClient):
        def __init__(self):
            pass

        def read(self, target, command, data=b"", timeout=None, retries=1):
            for _ in range(retries + 1):
                attempts.append(target)
                if len(attempts) >= 2:
                    return object()
            raise TimeoutError_("no answer")

    assert FlakyClient().ping(0x03) is True
    assert len(attempts) == 2


def test_the_stored_fault_log_is_looked_for_on_more_than_one_node():
    """A CR X210.350.FC does not answer 60/07; its DP C340.CAN does.

    Asking only the drive unit and giving up reported "no readable fault log"
    on a bike whose display was holding a stored overvoltage code the whole
    time -- the exact fault that bike had.
    """
    from bafang_can.constants import DeviceId
    from bafang_can.protocol import TimeoutError_
    from bafang_can.system import BafangSystem

    asked = []

    class OnlyTheDisplayAnswers:
        def read(self, target, command, *args, **kwargs):
            asked.append(target)
            if target is not DeviceId.DISPLAY:
                raise TimeoutError_("no answer")
            return type("M", (), {"data": b"0736"})()

        def add_listener(self, callback):
            pass

    system = BafangSystem(OnlyTheDisplayAnswers())
    codes, source = system.errors_with_source()

    assert [code for code, _, _ in codes] == [7, 36]
    assert source is DeviceId.DISPLAY
    assert asked == [DeviceId.DRIVE_UNIT, DeviceId.DISPLAY]


def test_naming_a_device_asks_only_that_device():
    from bafang_can.constants import DeviceId
    from bafang_can.protocol import TimeoutError_
    from bafang_can.system import BafangSystem

    asked = []

    class NothingAnswers:
        def read(self, target, command, *args, **kwargs):
            asked.append(target)
            raise TimeoutError_("no answer")

        def add_listener(self, callback):
            pass

    system = BafangSystem(NothingAnswers())
    with pytest.raises(TimeoutError_):
        system.errors_with_source(DeviceId.DRIVE_UNIT)

    assert asked == [DeviceId.DRIVE_UNIT]


def test_display_data_survives_a_block_the_display_will_not_answer():
    """A DP C340.C answers 63/01 and is silent on 63/02.

    A partial result is the normal case on this hardware, so one unanswered
    block must not cost the one that did answer.
    """
    from bafang_can.protocol import TimeoutError_
    from bafang_can.system import BafangSystem

    class OnlyBlock1:
        def read(self, target, command, *args, **kwargs):
            if command.subcode != 0x01:
                raise TimeoutError_("no answer")
            return type("M", (), {"data": bytes.fromhex("760100a30e005603")})()

        def add_listener(self, callback):
            pass

    data = BafangSystem(OnlyBlock1()).display_data()

    assert data["DisplayDataBlock1"]["total_mileage"] == 374
    assert data["DisplayDataBlock1"]["single_mileage"] == 374.7
    assert "not readable" in data["DisplayDataBlock2"]


def test_speed_parameters_fall_back_to_the_broadcast():
    """A CR X210.350.FC will not answer 32/03 but broadcasts it every 2 s.

    The write command is the same 32/03, so without this fallback `wheel` was
    unusable on exactly the firmware that hands over no parameter block either.
    """
    from bafang_can.frame import BafangId, BafangMessage
    from bafang_can.protocol import TimeoutError_
    from bafang_can.system import BafangSystem

    class SilentButBroadcasting:
        def __init__(self):
            self.listeners = []

        def read(self, target, command, *args, **kwargs):
            raise TimeoutError_("no answer")

        def add_listener(self, callback):
            self.listeners.append(callback)
            callback(
                BafangMessage(
                    id=BafangId(source=0x02, target=0x1F, operation=0, code=0x32,
                                subcode=0x03),
                    data=bytes.fromhex("c409c0019d08"),
                )
            )

        def remove_listener(self, callback):
            self.listeners.remove(callback)

    bus = SilentButBroadcasting()
    params = BafangSystem(bus).read_speed_parameters()

    assert params.speed_limit == 25.0
    assert params.circumference == 2205
    assert bus.listeners == []


def test_the_broadcast_fallback_gives_up_rather_than_hanging():
    from bafang_can.protocol import TimeoutError_
    from bafang_can.system import BafangSystem

    class NothingAtAll:
        def read(self, target, command, *args, **kwargs):
            raise TimeoutError_("no answer")

        def add_listener(self, callback):
            pass

        def remove_listener(self, callback):
            pass

    system = BafangSystem(NothingAtAll())
    system.SPEED_BROADCAST_WAIT = 0.05
    with pytest.raises(TimeoutError_):
        system.read_speed_parameters()


def test_an_empty_answer_is_not_reported_as_an_absence(capsys):
    """A zero-length reply proves the handler exists. Silence proves nothing.

    A real M200 answers 0x60/0x06 with MULTIFRAME_START declaring length zero
    followed straight by MULTIFRAME_END. Folding that into "silent" would deny
    a handler that demonstrably ran; folding it into "answered" would imply a
    value it did not send.
    """
    assert run("probe", "--json") == 0
    data = json.loads(capsys.readouterr().out)

    assert data["SystemParams"] == "empty"


def test_an_empty_answer_is_never_listed_among_the_refused(capsys):
    """The refused list is the claim that a firmware is blocking commands.

    An "empty" outcome is a reply, so putting it there would manufacture
    evidence of blocking out of a command the bike answered.
    """
    assert run("probe") == 0
    out = capsys.readouterr().out

    assert "empty     1" in out
    if "declining: " in out:
        assert "SystemParams" not in out.split("declining: ", 1)[1]


def test_a_speed_write_is_confirmed_on_the_broadcast_not_the_ack(tmp_path, capsys):
    """This firmware will not answer a read of 32/03, so a read-back cannot
    verify the write and the acknowledgement alone was being trusted.

    It broadcasts 32/03 anyway, and that broadcast does reflect a write --
    measured on a real G210 as NORMAL_ACK after 5 ms and the new value on the
    broadcast 300 ms later. The broadcast is better evidence than a read-back
    would be: it is what the drive unit tells the rest of the bike.
    """
    state = ["--sim-state", str(tmp_path / "bike.json")]
    assert main([*SIM, *state, "-y", "wheel", "--circumference", "2240",
                 "--apply"]) == 0
    out = capsys.readouterr().out

    assert "confirmed on the drive unit's own 32/03 broadcast" in out


def test_a_write_the_drive_unit_ignores_is_reported_as_a_failure(tmp_path, capsys):
    """An acknowledged write whose value never reaches the bus is a failure.

    Without this the tool would print "Written." for a setting that never took
    effect -- the worst outcome for a command that changes how fast a bike is
    allowed to go.
    """
    from bafang_can import simulator

    original = simulator.SimBus.BROADCAST_AFTER_WRITE
    simulator.SimBus.BROADCAST_AFTER_WRITE = frozenset()
    try:
        state = ["--sim-state", str(tmp_path / "bike.json")]
        assert main([*SIM, *state, "-y", "wheel", "--circumference", "2240",
                     "--apply"]) != 0
    finally:
        simulator.SimBus.BROADCAST_AFTER_WRITE = original

    assert "was not applied" in capsys.readouterr().err
