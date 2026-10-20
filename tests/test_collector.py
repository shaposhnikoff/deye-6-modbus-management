"""Tests for the collector that owns the serial line."""

from __future__ import annotations

import time

from deye_monitoring.collector import Collector
from deye_monitoring.snapshot import Reading


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
