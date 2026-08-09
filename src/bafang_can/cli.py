"""Command line interface.

Read commands run without ceremony. Every command that changes the bike is a
dry run unless ``--apply`` is given, and prints exactly which bytes would go
out. Calibration and error clearing additionally require ``--yes``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import time
from typing import Any

from . import codecs
from .commands import READ, WRITE, Command
from .constants import DeviceId, WHEEL_TABLE, wheel_by_text
from .frame import BafangId
from .profiles import m200
from .protocol import BafangClient, BafangError
from .system import BafangSystem
from .transport import AdapterConfig, describe_adapters, open_bus

log = logging.getLogger("bafang_can")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _as_json(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        if hasattr(value, "to_dict"):
            return value.to_dict()
        return dataclasses.asdict(value)
    if isinstance(value, dict):
        return {k: _as_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_as_json(v) for v in value]
    if isinstance(value, bytes):
        return value.hex()
    return value


def _print(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_as_json(value), indent=2, default=str))
        return
    _print_human(_as_json(value))


def _print_human(value: Any, indent: int = 0) -> None:
    pad = "  " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                print(f"{pad}{key}:")
                _print_human(item, indent + 1)
            else:
                print(f"{pad}{key}: {item}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            if isinstance(item, (dict, list)):
                print(f"{pad}[{i}]")
                _print_human(item, indent + 1)
            else:
                print(f"{pad}- {item}")
    else:
        print(f"{pad}{value}")


def _connect(args) -> tuple[BafangClient, BafangSystem]:
    config = AdapterConfig(
        interface=args.interface,
        channel=args.channel,
        bitrate=args.bitrate,
        index=args.index,
    )
    bus = open_bus(config)
    client = BafangClient(bus, timeout=args.timeout).start()
    return client, BafangSystem(client)


def _device(name: str) -> DeviceId:
    try:
        return DeviceId[name.upper()]
    except KeyError:
        raise SystemExit(
            f"unknown device '{name}'. Known: "
            + ", ".join(d.name.lower() for d in DeviceId)
        )


def _confirm(args, what: str) -> bool:
    if args.yes:
        return True
    answer = input(f"{what} Type 'yes' to continue: ").strip().lower()
    return answer == "yes"


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_adapters(args) -> int:
    lines = list(describe_adapters())
    found = [line for line in lines if "cannot enumerate" not in line]
    for line in lines:
        if line not in found:
            print(line)
    if not found:
        print("No CAN adapter found.")
        print(
            "CANable Pro 2.0 with candleLight-FD firmware appears as a gs_usb "
            "device; with slcan firmware it appears as a serial port."
        )
        print("macOS needs libusb for gs_usb: brew install libusb")
        return 1
    for line in found:
        print(line)
    return 0


def cmd_scan(args) -> int:
    client, system = _connect(args)
    try:
        result = system.scan()
        _print(
            {device.name: ("present" if ok else "-") for device, ok in result.items()},
            args.json,
        )
        return 0 if any(result.values()) else 2
    finally:
        client.close()


def cmd_info(args) -> int:
    client, system = _connect(args)
    try:
        devices = [_device(args.device)] if args.device else list(
            (DeviceId.DRIVE_UNIT, DeviceId.DISPLAY, DeviceId.TORQUE_SENSOR, DeviceId.BATTERY)
        )
        out = {}
        for device in devices:
            info = system.info(device)
            out[device.name] = info.to_dict()
        _print(out, args.json)
        return 0
    finally:
        client.close()


def cmd_probe(args) -> int:
    client, system = _connect(args)
    try:
        device = _device(args.device) if args.device else DeviceId.DRIVE_UNIT
        result = system.capabilities(device)
        _print(result, args.json)
        if not args.json:
            supported = [name for name, ok in result.items() if ok]
            print(f"\n{len(supported)}/{len(result)} commands answered by {device.name}")
        return 0
    finally:
        client.close()


def cmd_diagnose(args) -> int:
    client, system = _connect(args)
    try:
        report: dict[str, Any] = {"motor_profile": m200.MOTOR_NAME}
        present = system.scan()
        report["bus"] = {d.name: ok for d, ok in present.items()}
        if not present.get(DeviceId.DRIVE_UNIT):
            report["verdict"] = (
                "Drive unit did not answer. Check CAN-H/CAN-L wiring and "
                "polarity, that the bike is powered on, and the bit rate "
                "(Bafang uses 250 kbit/s)."
            )
            _print(report, args.json)
            return 2
        report["identity"] = system.info(DeviceId.DRIVE_UNIT).to_dict()
        try:
            report["errors"] = [
                {"code": c, "description": d, "recommendation": r}
                for c, d, r in system.errors()
            ]
        except BafangError as exc:
            report["errors"] = f"not readable: {exc}"
        report["realtime"] = system.realtime()
        try:
            speed = system.read_speed_parameters()
            report["speed"] = speed.to_dict()
            report["speed_problems"] = m200.check_speed(speed)
        except (BafangError, codecs.DecodeError) as exc:
            report["speed"] = f"not readable: {exc}"
        try:
            block = system.read_block("Parameter1")
            report["parameter1_checksum_ok"] = block.checksum_ok
            report["parameter1_problems"] = m200.check_parameter1(block)
        except (BafangError, codecs.DecodeError) as exc:
            report["parameter1"] = f"not readable: {exc}"
        report["checklist"] = list(m200.DIAGNOSTIC_STEPS)
        _print(report, args.json)
        return 0
    finally:
        client.close()


def cmd_monitor(args) -> int:
    client, system = _connect(args)
    try:
        end = time.monotonic() + args.seconds if args.seconds else None
        while end is None or time.monotonic() < end:
            data = system.realtime()
            if args.json:
                print(json.dumps(_as_json(data), default=str))
            else:
                c0 = data.get("controller0")
                c1 = data.get("controller1")
                sensor = data.get("sensor")
                parts = []
                if c1:
                    parts.append(f"{c1.speed:5.1f} km/h")
                    parts.append(f"{c1.voltage:5.1f} V")
                    parts.append(f"{c1.current:5.1f} A")
                    parts.append(f"ctrl {c1.temperature}C")
                    parts.append(f"motor {c1.motor_temperature}C")
                if c0:
                    parts.append(f"cad {c0.cadence:3d}")
                    parts.append(f"torque {c0.torque:5d}")
                    parts.append(f"soc {c0.remaining_capacity:3d}%")
                if sensor:
                    parts.append(f"sensor {sensor.torque:5d}/{sensor.cadence:3d}")
                print(" | ".join(parts) or "no data")
            time.sleep(args.interval)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        client.close()


def cmd_sniff(args) -> int:
    client, _ = _connect(args)
    try:
        def on_message(message) -> None:
            print(
                f"{message.timestamp:14.3f} {message.id} "
                f"[{len(message.data)}] {message.data.hex()}"
                + ("  (multiframe)" if message.multiframe else "")
            )

        client.add_listener(on_message)
        client.send_acks = not args.passive
        end = time.monotonic() + args.seconds if args.seconds else None
        while end is None or time.monotonic() < end:
            time.sleep(0.2)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        client.close()


def cmd_errors(args) -> int:
    client, system = _connect(args)
    try:
        device = _device(args.device) if args.device else DeviceId.DRIVE_UNIT
        errors = system.errors(device)
        if not errors:
            print("No stored error codes.")
        else:
            _print(
                [
                    {"code": code, "description": desc, "recommendation": rec}
                    for code, desc, rec in errors
                ],
                args.json,
            )
        if args.clear:
            if not args.apply:
                print("\n--apply not given: error codes were NOT cleared.")
                return 0
            if not _confirm(args, "Clearing erases the stored fault history."):
                print("Aborted.")
                return 1
            system.clear_errors(device)
            print("Error codes cleared.")
        return 0
    finally:
        client.close()


def cmd_dump(args) -> int:
    client, system = _connect(args)
    try:
        data = system.dump()
        if args.output:
            with open(args.output, "w", encoding="utf-8") as handle:
                json.dump(_as_json(data), handle, indent=2, default=str)
            print(f"Wrote {args.output}")
        else:
            _print(data, args.json)
        return 0
    finally:
        client.close()


def cmd_restore(args) -> int:
    with open(args.file, encoding="utf-8") as handle:
        data = json.load(handle)
    client, system = _connect(args)
    try:
        blocks = args.blocks.split(",") if args.blocks else None
        if args.apply and not _confirm(
            args,
            f"About to write {blocks or 'all configuration blocks'} to the drive unit.",
        ):
            print("Aborted.")
            return 1
        for line in system.apply(data, blocks=blocks, dry_run=not args.apply):
            print(line)
        if not args.apply:
            print("\nDry run. Re-run with --apply to write.")
        return 0
    finally:
        client.close()


def _resolve_path(obj: Any, path: list[str]) -> Any:
    for part in path:
        if isinstance(obj, list):
            obj = obj[int(part)]
        else:
            obj = getattr(obj, part)
    return obj


def _set_path(obj: Any, path: list[str], value: str) -> tuple[Any, Any]:
    parent = _resolve_path(obj, path[:-1])
    last = path[-1]
    current = parent[int(last)] if isinstance(parent, list) else getattr(parent, last)
    if isinstance(current, bool):
        new: Any = value.strip().lower() in ("1", "true", "yes", "on")
    elif isinstance(current, int):
        new = int(value, 0)
    elif isinstance(current, float):
        new = float(value)
    else:
        new = value
    if isinstance(parent, list):
        parent[int(last)] = new
    else:
        setattr(parent, last, new)
    return current, new


def cmd_get(args) -> int:
    client, system = _connect(args)
    try:
        name, _, rest = args.path.partition(".")
        if name == "SpeedParameters":
            value: Any = system.read_speed_parameters()
        elif name in codecs.CONFIG_BLOCKS:
            value = system.read_block(name)
        else:
            raise SystemExit(
                "unknown block. Known: "
                + ", ".join([*codecs.CONFIG_BLOCKS, "SpeedParameters"])
            )
        if rest:
            value = _resolve_path(value, rest.split("."))
        _print(value, args.json)
        return 0
    finally:
        client.close()


def cmd_set(args) -> int:
    assignments: list[tuple[str, str]] = []
    for item in args.assignment:
        key, sep, value = item.partition("=")
        if not sep:
            raise SystemExit(f"expected block.field=value, got '{item}'")
        assignments.append((key.strip(), value.strip()))

    block_names = {key.split(".")[0] for key, _ in assignments}
    if len(block_names) != 1:
        raise SystemExit("all assignments in one call must target the same block")
    block_name = block_names.pop()

    client, system = _connect(args)
    try:
        if block_name == "SpeedParameters":
            block: Any = system.read_speed_parameters()
        elif block_name in codecs.CONFIG_BLOCKS:
            block = system.read_block(block_name)
            if not block.checksum_ok and not args.force:
                print(
                    f"{block_name} read back with a bad checksum. Refusing to "
                    "write on top of a block that may be corrupt (--force "
                    "overrides)."
                )
                return 1
        else:
            raise SystemExit(f"unknown block '{block_name}'")

        for key, value in assignments:
            path = key.split(".")[1:]
            if not path:
                raise SystemExit(f"'{key}' does not name a field")
            old, new = _set_path(block, path, value)
            print(f"{key}: {old} -> {new}")

        problems: list[str] = []
        if block_name == "Parameter1":
            problems = m200.check_parameter1(block)
        elif block_name == "SpeedParameters":
            problems = m200.check_speed(block)
        if problems:
            print("\nSafety checks:")
            for problem in problems:
                print(f"  ! {problem}")
            if not args.force:
                print("\nRefusing to write. Re-run with --force if intended.")
                return 1

        payload = block.encode()
        print(f"\nPayload ({len(payload)} bytes): {payload.hex()}")
        if not args.apply:
            print("Dry run. Re-run with --apply to write.")
            return 0
        if not _confirm(args, f"Write {block_name} to the drive unit?"):
            print("Aborted.")
            return 1
        if block_name == "SpeedParameters":
            system.write_speed_parameters(block)
        else:
            system.write_block(block_name, block)
        print("Written. Re-reading to verify...")
        time.sleep(0.3)
        if block_name == "SpeedParameters":
            after = system.read_speed_parameters().encode()
        else:
            after = system.read_block(block_name).encode()
        if after == payload:
            print("Verified: the drive unit reports the new values.")
            return 0
        print(f"Mismatch after write. Device now reports: {after.hex()}")
        return 3
    finally:
        client.close()


def cmd_wheel(args) -> int:
    client, system = _connect(args)
    try:
        params = system.read_speed_parameters()
        print(
            f"current: speed limit {params.speed_limit} km/h, wheel "
            f"{params.wheel.text if params.wheel else params.wheel_code}, "
            f"circumference {params.circumference} mm"
        )
        if args.diameter:
            wheel = wheel_by_text(args.diameter)
            if wheel is None:
                raise SystemExit(
                    "unknown wheel size. Known: "
                    + ", ".join(w.text for w in WHEEL_TABLE)
                )
            params.wheel_code = wheel.code
        if args.circumference:
            params.circumference = args.circumference
        if args.speed_limit:
            params.speed_limit = args.speed_limit
        problems = m200.check_speed(params)
        if problems:
            print("\nSafety checks:")
            for problem in problems:
                print(f"  ! {problem}")
            if not args.force:
                print("\nRefusing to write. Re-run with --force if intended.")
                return 1
        payload = params.encode()
        print(f"new: {payload.hex()}")
        if not args.apply:
            print("Dry run. Re-run with --apply to write.")
            return 0
        if not _confirm(args, "Write speed parameters to the drive unit?"):
            print("Aborted.")
            return 1
        system.write_speed_parameters(params)
        print("Written.")
        return 0
    finally:
        client.close()


def cmd_calibrate(args) -> int:
    client, system = _connect(args)
    try:
        if args.target == "torque":
            prompt = (
                "Torque sensor zero: the bike must be upright and unloaded, "
                "with no foot on the pedals and the cranks still."
            )
            action = system.calibrate_torque_sensor
        else:
            prompt = (
                "Position sensor calibration: the rear wheel must be off the "
                "ground and free to spin -- the motor will turn."
            )
            action = system.calibrate_position_sensor
        print(prompt)
        if not args.apply:
            print("Dry run. Re-run with --apply to actually calibrate.")
            return 0
        if not _confirm(args, "Ready?"):
            print("Aborted.")
            return 1
        action()
        print("Calibration command acknowledged by the drive unit.")
        if args.target == "torque":
            time.sleep(0.5)
            data = system.realtime()
            sensor = data.get("sensor") or data.get("controller0")
            if sensor is not None:
                print(f"Torque reading after calibration: {sensor.torque}")
        return 0
    finally:
        client.close()


def cmd_raw(args) -> int:
    client, _ = _connect(args)
    try:
        device = _device(args.device)
        command = Command(
            name="raw",
            code=int(args.code, 0),
            subcode=int(args.subcode, 0),
            devices=(device,),
        )
        if args.data is None:
            message = client.read(device, command)
            print(f"{message.id} [{len(message.data)}] {message.data.hex()}")
            return 0
        payload = bytes.fromhex(args.data.replace(" ", ""))
        if not args.apply:
            print(f"Dry run: would write {payload.hex()} to {device.name}.")
            return 0
        if not _confirm(args, f"Write raw {payload.hex()} to {device.name}?"):
            print("Aborted.")
            return 1
        answer = client.write(device, command, payload)
        print(f"answer: {answer.data.hex() if answer else 'none'}")
        return 0
    finally:
        client.close()


def cmd_commands(args) -> int:
    rows = []
    for table, entries in (("read", READ), ("write", WRITE)):
        for name, command in entries.items():
            rows.append(
                {
                    "table": table,
                    "name": name,
                    "code": f"{command.code:#04x}",
                    "subcode": f"{command.subcode:#04x}",
                    "devices": [d.name for d in command.devices],
                }
            )
    _print(rows, args.json)
    return 0


def cmd_decode(args) -> int:
    """Decode a CAN identifier without touching hardware."""
    ident = BafangId.decode(int(args.can_id, 0))
    _print(dataclasses.asdict(ident) | {"text": str(ident)}, args.json)
    return 0


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bafang-can",
        description=(
            "Diagnose, calibrate and configure a Bafang M200 (G210) over CAN "
            "using a CANable Pro 2.0."
        ),
    )
    parser.add_argument("--interface", default="gs_usb", help="python-can interface (default: gs_usb)")
    parser.add_argument("--channel", default=None, help="channel, e.g. a serial port for slcan")
    parser.add_argument("--bitrate", type=int, default=250_000, help="default: 250000")
    parser.add_argument("--index", type=int, default=0, help="gs_usb device index")
    parser.add_argument("--timeout", type=float, default=2.0, help="request timeout in seconds")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("-y", "--yes", action="store_true", help="skip confirmations")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("adapters", help="list CAN adapters that are attached").set_defaults(func=cmd_adapters)

    sub.add_parser("scan", help="which devices answer on the bus").set_defaults(func=cmd_scan)

    p = sub.add_parser("info", help="identity of every device")
    p.add_argument("--device", help="drive_unit, display, torque_sensor, battery")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("probe", help="find out which commands this firmware answers")
    p.add_argument("--device", help="default: drive_unit")
    p.set_defaults(func=cmd_probe)

    sub.add_parser("diagnose", help="full read-only health report").set_defaults(func=cmd_diagnose)

    p = sub.add_parser("monitor", help="live telemetry")
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--seconds", type=float, default=0, help="0 = until interrupted")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("sniff", help="decode every frame on the bus")
    p.add_argument("--seconds", type=float, default=0)
    p.add_argument("--passive", action="store_true", help="never transmit, not even ACKs")
    p.set_defaults(func=cmd_sniff)

    p = sub.add_parser("errors", help="read (and optionally clear) stored error codes")
    p.add_argument("--device")
    p.add_argument("--clear", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_errors)

    p = sub.add_parser("dump", help="back up the whole configuration to JSON")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("restore", help="write a dump back to the drive unit")
    p.add_argument("file")
    p.add_argument("--blocks", help="comma separated subset, e.g. Parameter1,Parameter2")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("get", help="read a block or a single field")
    p.add_argument("path", help="e.g. Parameter1 or Parameter1.assist_levels.0.current_limit")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("set", help="change fields (read-modify-write, verified)")
    p.add_argument("assignment", nargs="+", help="block.field=value")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--force", action="store_true", help="ignore safety checks")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("wheel", help="wheel size, circumference and speed limit")
    p.add_argument("--diameter", help='e.g. 27.5 or 29 or "700mm"')
    p.add_argument("--circumference", type=int, help="mm")
    p.add_argument("--speed-limit", type=float, help="km/h")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_wheel)

    p = sub.add_parser("calibrate", help="torque sensor zero or rotor position")
    p.add_argument("target", choices=("torque", "position"))
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("raw", help="raw command access for protocol work")
    p.add_argument("code")
    p.add_argument("subcode")
    p.add_argument("--device", default="drive_unit")
    p.add_argument("--data", help="hex payload; omit to read")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_raw)

    sub.add_parser("commands", help="print the known command tables").set_defaults(func=cmd_commands)

    p = sub.add_parser("decode", help="decode a 29-bit Bafang CAN id (offline)")
    p.add_argument("can_id")
    p.set_defaults(func=cmd_decode)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level={0: logging.WARNING, 1: logging.INFO}.get(args.verbose, logging.DEBUG),
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except BafangError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
