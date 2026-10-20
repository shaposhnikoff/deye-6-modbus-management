"""Tests for CLI argument parsing (no hardware, no network)."""

from __future__ import annotations

import pytest

from deye_monitoring.cli import build_parser


def test_serve_defaults() -> None:
    args = build_parser().parse_args(["serve"])
    assert args.command == "serve"
    assert args.listen == "0.0.0.0:9101"
    assert args.interval == pytest.approx(10.0)
    assert args.port == "/dev/modbus"
    assert args.baud == 9600
    assert args.slave == 1


def test_serve_overrides() -> None:
    args = build_parser().parse_args(
        ["serve", "--listen", "127.0.0.1:9200", "--interval", "5", "--port", "/dev/ttyUSB0"]
    )
    assert args.listen == "127.0.0.1:9200"
    assert args.interval == pytest.approx(5.0)
    assert args.port == "/dev/ttyUSB0"


def test_serve_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEYE_LISTEN", "0.0.0.0:9300")
    monkeypatch.setenv("DEYE_INTERVAL", "20")
    args = build_parser().parse_args(["serve"])
    assert args.listen == "0.0.0.0:9300"
    assert args.interval == pytest.approx(20.0)


def test_watch_interval_and_human_flag() -> None:
    args = build_parser().parse_args(["watch", "--interval", "3", "--human"])
    assert args.interval == pytest.approx(3.0)
    assert args.human is True


def test_command_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_serve_wires_the_cache_writer(monkeypatch, tmp_path) -> None:
    """Regression: the cache writer must actually be hooked into the collector."""
    from deye_monitoring import cli
    from deye_monitoring.exporter import Sample
    from deye_monitoring.snapshot import Reading

    captured: dict[str, object] = {}

    class FakeCollector:
        def __init__(self, reader, interval=10.0, on_sample=None):
            captured["on_sample"] = on_sample

        def start(self):
            return None

        def stop(self):
            return None

    class FakeServer:
        server_address = ("127.0.0.1", 9101)

        def serve_forever(self):
            return None

        def shutdown(self):
            return None

        def server_close(self):
            return None

    class ImmediateEvent:
        def wait(self, *_args):
            return True

        def set(self):
            return None

    class FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            self._target = target

        def start(self):
            return None

    from types import SimpleNamespace

    monkeypatch.setattr(cli, "Collector", FakeCollector)
    monkeypatch.setattr(cli, "create_server", lambda listen, collector: FakeServer())
    monkeypatch.setattr(cli, "threading", SimpleNamespace(Event=ImmediateEvent, Thread=FakeThread))

    path = tmp_path / "state.json"
    args = build_parser().parse_args(["serve", "--cache-path", str(path)])
    assert cli.cmd_serve(args) == 0

    on_sample = captured["on_sample"]
    assert on_sample is not None
    on_sample(Sample(readings=(Reading("PV1 Power", 654, "W", 0x00BA, "pv"),), serial="x"))
    assert path.exists()
