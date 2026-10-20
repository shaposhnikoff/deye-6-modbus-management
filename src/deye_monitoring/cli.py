"""Command line interface: read, json, dump and watch the inverter."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from datetime import UTC, datetime

from deye_monitoring.collector import Collector, modbus_reader
from deye_monitoring.modbus import ModbusClient, ModbusConfig, ModbusError
from deye_monitoring.notify import Notifier, NotifyConfig
from deye_monitoring.snapshot import (
    BLOCKS,
    Reading,
    as_mapping,
    decode_all,
    read_registers,
    serial_number,
)
from deye_monitoring.web import create_server

DEFAULT_PORT = os.environ.get("DEYE_PORT", "/dev/modbus")
DEFAULT_BAUD = int(os.environ.get("DEYE_BAUD", "9600"))
DEFAULT_SLAVE = int(os.environ.get("DEYE_SLAVE", "1"))


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with all subcommands."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", default=DEFAULT_PORT, help="serial device (default: %(default)s)")
    common.add_argument(
        "--baud", type=int, default=DEFAULT_BAUD, help="baud rate (default: %(default)s)"
    )
    common.add_argument(
        "--slave", type=int, default=DEFAULT_SLAVE, help="Modbus address (default: %(default)s)"
    )
    common.add_argument("--timeout", type=float, default=1.0, help="reply timeout in seconds")

    parser = argparse.ArgumentParser(
        prog="dmon",
        description="Read-only Modbus RTU monitoring for a Deye hybrid inverter.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "read", parents=[common], help="read once and print a human readable table"
    )
    subparsers.add_parser("json", parents=[common], help="read once and print one JSON line")
    subparsers.add_parser("dump", parents=[common], help="read once and print raw register words")

    watch = subparsers.add_parser(
        "watch", parents=[common], help="poll forever, one JSON line per cycle"
    )
    watch.add_argument("--interval", type=float, default=10.0, help="seconds between reads")
    watch.add_argument("--human", action="store_true", help="print tables instead of JSON lines")

    serve = subparsers.add_parser(
        "serve", parents=[common], help="serve Prometheus metrics on /metrics and /health"
    )
    serve.add_argument(
        "--listen",
        default=os.environ.get("DEYE_LISTEN", "0.0.0.0:9101"),
        help="host:port to bind (default: %(default)s)",
    )
    serve.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("DEYE_INTERVAL", "10")),
        help="seconds between Modbus polls (default: %(default)s)",
    )
    serve.add_argument(
        "--cache-path",
        default=os.environ.get("DEYE_CACHE_PATH", "/run/deye/state.json"),
        help="local JSON cache written after every poll (default: %(default)s)",
    )
    serve.add_argument(
        "--mqtt",
        action="store_true",
        help="publish state and Home Assistant discovery over MQTT",
    )
    serve.add_argument(
        "--notify",
        action="store_true",
        help="send Telegram alerts (needs DEYE_TELEGRAM_TOKEN and DEYE_TELEGRAM_CHAT_ID)",
    )
    mqtt_cmd = subparsers.add_parser(
        "mqtt", parents=[common], help="run collector and publish to MQTT only"
    )
    mqtt_cmd.add_argument("--interval", type=float, default=10.0)
    mqtt_cmd.add_argument("--broker", default=os.environ.get("DEYE_MQTT_HOST", "192.168.10.68"))
    mqtt_cmd.add_argument(
        "--broker-port", type=int, default=int(os.environ.get("DEYE_MQTT_PORT", "1883"))
    )
    mqtt_cmd.add_argument("--prefix", default=os.environ.get("DEYE_MQTT_PREFIX", "homeassistant"))
    mqtt_cmd.add_argument("--node", default=os.environ.get("DEYE_MQTT_NODE", "deye_6k"))

    publish_cmd = subparsers.add_parser(
        "publish",
        parents=[common],
        help="publish MQTT state from the local cache (no serial access)",
    )
    publish_cmd.add_argument(
        "--cache",
        default=os.environ.get("DEYE_CACHE_PATH", "/run/deye/state.json"),
        help="cache file written by serve (default: %(default)s)",
    )
    publish_cmd.add_argument("--interval", type=float, default=10.0)
    publish_cmd.add_argument("--broker", default=os.environ.get("DEYE_MQTT_HOST", "192.168.10.68"))
    publish_cmd.add_argument(
        "--broker-port", type=int, default=int(os.environ.get("DEYE_MQTT_PORT", "1883"))
    )
    publish_cmd.add_argument(
        "--prefix", default=os.environ.get("DEYE_MQTT_PREFIX", "homeassistant")
    )
    publish_cmd.add_argument("--node", default=os.environ.get("DEYE_MQTT_NODE", "deye_6k"))
    return parser


def _config(args: argparse.Namespace) -> ModbusConfig:
    return ModbusConfig(
        port=args.port,
        baudrate=args.baud,
        slave=args.slave,
        timeout=args.timeout,
    )


def _collect(config: ModbusConfig) -> tuple[dict[int, int], list[Reading], object | None]:
    with ModbusClient(config) as client:
        registers = read_registers(client)
    readings, tou_state = decode_all(registers)
    return registers, readings, tou_state


def _print_table(readings: list[Reading], serial: str | None) -> None:
    if serial:
        print(f"inverter serial: {serial}")
    group = None
    for reading in readings:
        if reading.group != group:
            group = reading.group
            print(f"\n[{group}]")
        print(
            f"  0x{reading.address:04X}  {reading.name:<38} "
            f"{reading.value:>10g} {reading.unit}".rstrip()
        )
    print()


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def cmd_read(args: argparse.Namespace) -> int:
    """Print a single human readable snapshot."""
    registers, readings, _tou_state = _collect(_config(args))
    _print_table(readings, serial_number(registers))
    return 0


def cmd_json(args: argparse.Namespace) -> int:
    """Print a single JSON snapshot."""
    registers, readings, tou_state = _collect(_config(args))
    payload: dict[str, object] = {
        "timestamp": _timestamp(),
        "serial": serial_number(registers),
        "values": as_mapping(readings),
    }
    if tou_state is not None:
        payload["tou"] = tou_state.as_dict()
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    """Print the raw holding registers, eight words per line."""
    registers, _readings, _tou_state = _collect(_config(args))
    addresses = sorted(registers)
    for block_start, _count in BLOCKS:
        print(f"-- block 0x{block_start:04X}")
        block = [address for address in addresses if block_start <= address < block_start + 0x7D]
        for index in range(0, len(block), 8):
            chunk = block[index : index + 8]
            cells = " ".join(f"0x{address:04X}={registers[address]:5d}" for address in chunk)
            print("   " + cells)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Poll the inverter until interrupted."""
    config = _config(args)
    while True:
        started = time.monotonic()
        try:
            registers, readings, _tou_state = _collect(config)
        except ModbusError as error:
            print(json.dumps({"timestamp": _timestamp(), "error": str(error)}), flush=True)
        else:
            if args.human:
                _print_table(readings, serial_number(registers))
            payload = {
                "timestamp": _timestamp(),
                "serial": serial_number(registers),
                "values": as_mapping(readings),
            }
            print(json.dumps(payload, ensure_ascii=False), flush=True)
        elapsed = time.monotonic() - started
        time.sleep(max(args.interval - elapsed, 0.0))


def _mqtt_publisher(args: argparse.Namespace):
    from deye_monitoring.mqtt import MqttConfig, MqttPublisher

    config = MqttConfig(
        host=getattr(args, "broker", None) or os.environ.get("DEYE_MQTT_HOST", "192.168.10.68"),
        port=getattr(args, "broker_port", None) or int(os.environ.get("DEYE_MQTT_PORT", "1883")),
        username=os.environ.get("DEYE_MQTT_USER"),
        password=os.environ.get("DEYE_MQTT_PASSWORD"),
        prefix=getattr(args, "prefix", None) or os.environ.get("DEYE_MQTT_PREFIX", "homeassistant"),
        node=getattr(args, "node", None) or os.environ.get("DEYE_MQTT_NODE", "deye_6k"),
    )
    return MqttPublisher(config)


def _cache_writer(path: str, interval: float):
    from deye_monitoring.cache import write_snapshot

    def handle(sample):
        write_snapshot(path, sample, interval=interval)

    return handle


def _chain(*callbacks):
    active = [callback for callback in callbacks if callback is not None]
    if not active:
        return None

    def handle(sample):
        for callback in active:
            callback(sample)

    return handle


def _notify_config() -> NotifyConfig:
    return NotifyConfig(
        token=os.environ.get("DEYE_TELEGRAM_TOKEN"),
        chat_id=os.environ.get("DEYE_TELEGRAM_CHAT_ID"),
        temp_warning_c=float(os.environ.get("DEYE_TEMP_WARNING_C", "65")),
        stale_seconds=float(os.environ.get("DEYE_STALE_SECONDS", "120")),
        cooldown=float(os.environ.get("DEYE_NOTIFY_COOLDOWN", "1800")),
    )


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the collector and serve Prometheus metrics until terminated."""
    notify_enabled = args.notify or os.environ.get("DEYE_NOTIFY", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    notifier: Notifier | None = None
    if notify_enabled:
        config = _notify_config()
        if config.enabled:
            notifier = Notifier(config)
        else:
            print(
                "telegram disabled: set DEYE_TELEGRAM_TOKEN and DEYE_TELEGRAM_CHAT_ID",
                file=sys.stderr,
                flush=True,
            )

    publisher = _mqtt_publisher(args) if args.mqtt else None
    if publisher is not None:
        publisher.connect()

    collector = Collector(
        modbus_reader(_config(args)),
        interval=args.interval,
        on_sample=_chain(
            _cache_writer(args.cache_path, args.interval) if args.cache_path else None,
            notifier.handle if notifier else None,
            publisher.handle if publisher else None,
        ),
    )
    server = create_server(args.listen, collector)
    stop = threading.Event()

    def handle_signal(signum: int, _frame: object) -> None:
        print(f"received signal {signum}, stopping", file=sys.stderr, flush=True)
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, handle_signal)

    collector.start()
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, name="deye-http", daemon=True)
    thread.start()
    print(
        f"serving metrics on http://{host}:{port}/metrics "
        f"(poll interval {args.interval:g}s, device {args.port}, "
        f"telegram {'on' if notifier else 'off'})",
        flush=True,
    )
    try:
        stop.wait()
    finally:
        server.shutdown()
        server.server_close()
        collector.stop()
        if publisher is not None:
            publisher.close()
    return 0


def cmd_mqtt(args: argparse.Namespace) -> int:
    """Run the collector and publish to MQTT until interrupted."""
    publisher = _mqtt_publisher(args)
    publisher.connect()
    collector = Collector(
        modbus_reader(_config(args)), interval=args.interval, on_sample=publisher.handle
    )
    collector.start()
    print(
        f"publishing to {args.broker}:{args.broker_port} node {args.node} "
        f"(interval {args.interval:g}s)",
        flush=True,
    )
    stop = threading.Event()

    def handle_signal(signum: int, _frame: object) -> None:
        print(f"received signal {signum}, stopping", file=sys.stderr, flush=True)
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, handle_signal)
    try:
        stop.wait()
    finally:
        collector.stop()
        publisher.close()
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    """Publish MQTT state from the local cache until interrupted."""
    from deye_monitoring.cache import read_snapshot

    publisher = _mqtt_publisher(args)
    publisher.connect()
    stop = threading.Event()

    def handle_signal(signum: int, _frame: object) -> None:
        print(f"received signal {signum}, stopping", file=sys.stderr, flush=True)
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, handle_signal)

    online = True
    print(
        f"publishing {args.cache} to {args.broker}:{args.broker_port} "
        f"(interval {args.interval:g}s)",
        flush=True,
    )
    try:
        while not stop.is_set():
            snapshot = read_snapshot(args.cache)
            if snapshot is None:
                if online:
                    publisher.publish_offline(f"cache {args.cache} is missing")
                    online = False
            else:
                publisher.publish_from_cache(snapshot)
                online = snapshot.is_fresh()
            stop.wait(args.interval)
    finally:
        publisher.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``dmon`` command."""
    import logging

    logging.basicConfig(
        level=os.environ.get("DEYE_LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)
    handlers = {
        "read": cmd_read,
        "json": cmd_json,
        "dump": cmd_dump,
        "watch": cmd_watch,
        "serve": cmd_serve,
        "mqtt": cmd_mqtt,
        "publish": cmd_publish,
    }
    try:
        return handlers[args.command](args)
    except ModbusError as error:
        print(f"modbus error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
