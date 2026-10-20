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
