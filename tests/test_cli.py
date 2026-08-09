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
    assert data["Parameter1"] is True
    assert data["Parameter6017"] is False


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
