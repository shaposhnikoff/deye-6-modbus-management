"""``dmon-write`` — guarded Time-of-Use writes.

Dry-run by default: nothing is sent to the inverter without ``--confirm``. Every confirmed write
is read back and appended to an audit log.
"""

from __future__ import annotations

import argparse
import os
import sys

from deye_monitoring.modbus import ModbusConfig, ModbusError
from deye_monitoring.snapshot import serial_number
from deye_monitoring.write import (
    TARGETS,
    PlannedWrite,
    RegisterNotAllowed,
    WriteClient,
    WriteValueError,
    WriteVerificationError,
    apply_change,
    audit,
    matches,
    plan_change,
    resolve,
)

DEFAULT_LOG = os.environ.get("DEYE_WRITE_LOG", "/var/log/deye/writes.log")
TOU_FIRST = 248
TOU_LAST = 279


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", default=os.environ.get("DEYE_PORT", "/dev/modbus"))
    common.add_argument("--baud", type=int, default=int(os.environ.get("DEYE_BAUD", "9600")))
    common.add_argument("--slave", type=int, default=int(os.environ.get("DEYE_SLAVE", "1")))
    common.add_argument("--timeout", type=float, default=1.0)
    common.add_argument("--log", default=DEFAULT_LOG, help="audit log path (default: %(default)s)")
    common.add_argument(
        "--include-unverified",
        action="store_true",
        help="also allow targets marked unverified (raw bitfields)",
    )

    parser = argparse.ArgumentParser(
        prog="dmon-write",
        description="Guarded writes for the Deye inverter (Time-of-Use only).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", parents=[common], help="show writable targets and current values")

    plan = subparsers.add_parser("plan", parents=[common], help="validate changes, write nothing")
    plan.add_argument("changes", nargs="+", metavar="key=value")

    apply_cmd = subparsers.add_parser(
        "set", parents=[common], help="apply changes (needs --confirm)"
    )
    apply_cmd.add_argument("changes", nargs="+", metavar="key=value")
    apply_cmd.add_argument(
        "--confirm", action="store_true", help="actually write; without it this is a dry run"
    )

    verify = subparsers.add_parser(
        "verify",
        parents=[common],
        help="compare the live schedule with expected values (read-only)",
    )
    verify.add_argument("changes", nargs="+", metavar="key=value")
    return parser


def parse_assignments(pairs: list[str]) -> dict[str, float]:
    """Parse ``key=value`` arguments, raising on malformed input."""
    changes: dict[str, float] = {}
    for pair in pairs:
        if "=" not in pair:
            raise WriteValueError(f"expected key=value, got {pair!r}")
        key, _, raw_value = pair.partition("=")
        try:
            changes[key.strip()] = float(raw_value.strip())
        except ValueError as error:
            raise WriteValueError(f"{key}: {raw_value!r} is not a number") from error
    return changes


def _config(args: argparse.Namespace) -> ModbusConfig:
    return ModbusConfig(port=args.port, baudrate=args.baud, slave=args.slave, timeout=args.timeout)


def _read_tou(client: WriteClient) -> dict[int, int]:
    words = client.read_holding_registers(TOU_FIRST, TOU_LAST - TOU_FIRST + 1)
    return {TOU_FIRST + index: word for index, word in enumerate(words)}


def _plan(client: WriteClient, changes: dict[str, float], include_unverified: bool):
    registers = _read_tou(client)
    planned: list[PlannedWrite] = []
    for key, value in changes.items():
        target = resolve(key, include_unverified=include_unverified)
        planned.append(plan_change(target, value, registers.get(target.address)))
    return planned


def cmd_list(args: argparse.Namespace) -> int:
    """Print the allow-list with current register values."""
    with WriteClient(_config(args)) as client:
        registers = _read_tou(client)
    for key, target in TARGETS.items():
        if not target.enabled and not args.include_unverified:
            continue
        raw = registers.get(target.address)
        current = target.describe(raw) if raw is not None else "?"
        flag = "" if target.enabled else "  [unverified]"
        print(f"  {key:<24} 0x{target.address:04X}  {current:>10}{flag}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Validate and describe changes without writing."""
    changes = parse_assignments(args.changes)
    with WriteClient(_config(args)) as client:
        planned = _plan(client, changes, args.include_unverified)
    for change in planned:
        print(f"  plan  {change.describe()}")
    print("dry run: nothing was written")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Read the live registers and report drift against expected values."""
    changes = parse_assignments(args.changes)
    with WriteClient(_config(args)) as client:
        registers = _read_tou(client)
    drift = 0
    for key, value in changes.items():
        target = resolve(key, include_unverified=args.include_unverified)
        expected = target.to_raw(value)
        current = registers.get(target.address)
        ok = matches(target, expected, current)
        actual = target.describe(current) if current is not None else "?"
        status = "MATCH" if ok else "DRIFT"
        expected_text = target.describe(expected)
        print(f"  {status:<6}{key:<22} expected {expected_text:<10} actual {actual}")
        drift += 0 if ok else 1
    print(f"{len(changes) - drift}/{len(changes)} match")
    return 1 if drift else 0


def cmd_set(args: argparse.Namespace) -> int:
    """Apply changes when ``--confirm`` is given, otherwise behave like ``plan``."""
    changes = parse_assignments(args.changes)
    with WriteClient(_config(args)) as client:
        planned = _plan(client, changes, args.include_unverified)
        registers = _read_tou(client)
        serial = serial_number(registers)
        for change in planned:
            print(f"  {'write' if args.confirm else 'plan '}  {change.describe()}")
        if not args.confirm:
            print("dry run: nothing was written; re-run with --confirm")
            return 0
        failures = 0
        for change in planned:
            try:
                apply_change(client, change)
            except WriteVerificationError as error:
                failures += 1
                print(f"  FAILED  {error}", file=sys.stderr)
                audit(args.log, change, serial_number=serial, confirmed=False)
            else:
                print(f"  ok      {change.target.key} verified by readback")
                audit(args.log, change, serial_number=serial, confirmed=True)
    return 3 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``dmon-write`` command."""
    args = build_parser().parse_args(argv)
    handlers = {"list": cmd_list, "plan": cmd_plan, "set": cmd_set, "verify": cmd_verify}
    try:
        return handlers[args.command](args)
    except (RegisterNotAllowed, WriteValueError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    except ModbusError as error:
        print(f"modbus error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
