"""Tests for the collector that owns the serial line."""

from __future__ import annotations

import json
import pathlib
import time

import pytest

from deye_monitoring.collector import Collector
from deye_monitoring.snapshot import Reading

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "raw_registers.json"


@pytest.fixture(scope="module")
def registers() -> dict[int, int]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {int(address, 16): value for address, value in raw.items()}


class FakeSerialContext:
    """Minimal stand-in for ModbusClient used as a context manager."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> FakeSerialContext:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class FakeReader:
    """Stand-in for a Modbus poll, so tests never touch hardware."""

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def __call__(self) -> tuple[list[Reading], str | None]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("no reply from inverter")
        return [Reading("PV1 Power", 654, "W", 0x00BA, "pv")], "2601255637"


def test_poll_once_records_values_and_timestamp() -> None:
    collector = Collector(FakeReader())
    sample = collector.poll_once()
    assert sample.error is None
    assert sample.serial == "2601255637"
    assert sample.readings[0].value == 654
    assert sample.last_success > 0


def test_latest_serves_cache_without_polling() -> None:
    reader = FakeReader()
    collector = Collector(reader)
    collector.poll_once()
    first = collector.latest()
    second = collector.latest()
    assert reader.calls == 1
    assert first is second


def test_error_keeps_previous_readings_and_last_success() -> None:
    reader = FakeReader()
    collector = Collector(reader)
    good = collector.poll_once()
    reader.fail = True
    bad = collector.poll_once()
    assert bad.error is not None
    assert bad.last_success == good.last_success
    assert bad.readings == good.readings
    assert bad.serial == good.serial


def test_duration_is_measured() -> None:
    collector = Collector(FakeReader())
    sample = collector.poll_once()
    assert sample.duration >= 0


def test_start_and_stop_run_the_poll_loop() -> None:
    reader = FakeReader()
    collector = Collector(reader, interval=0.01)
    collector.start()
    time.sleep(0.1)
    collector.stop()
    assert reader.calls >= 2
    assert collector.latest().error is None


def test_start_is_idempotent() -> None:
    reader = FakeReader()
    collector = Collector(reader, interval=0.01)
    collector.start()
    collector.start()
    time.sleep(0.05)
    collector.stop()
    assert reader.calls >= 1


def test_on_sample_receives_every_poll() -> None:
    samples = []
    collector = Collector(FakeReader(), on_sample=samples.append)
    collector.poll_once()
    collector.poll_once()
    assert len(samples) == 2
    assert samples[0].serial == "2601255637"


def test_listener_failure_does_not_break_polling() -> None:
    def broken_listener(sample: object) -> None:
        raise RuntimeError("listener bug")

    collector = Collector(FakeReader(), on_sample=broken_listener)
    assert collector.poll_once().error is None


def test_modbus_reader_includes_the_tou_block(monkeypatch, registers: dict[int, int]) -> None:
    from deye_monitoring import collector as collector_module
    from deye_monitoring.modbus import ModbusConfig

    monkeypatch.setattr("deye_monitoring.modbus.ModbusClient", FakeSerialContext)
    monkeypatch.setattr(collector_module, "read_registers", lambda _client: registers)
    reader = collector_module.modbus_reader(ModbusConfig(port="fake"))
    readings, serial = reader()
    assert serial == "2601255637"
    assert any(reading.metric == "deye_tou_enabled" for reading in readings)
