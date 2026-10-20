"""Tests for the local JSON cache."""

from __future__ import annotations

import json
import time

from deye_monitoring.cache import (
    CacheSnapshot,
    read_snapshot,
    tou_from_readings,
    write_snapshot,
)
from deye_monitoring.exporter import Sample
from deye_monitoring.snapshot import Reading


def _sample(error: str | None = None) -> Sample:
    readings = [
        Reading("PV1 Power", 654, "W", 0x00BA, "pv"),
        Reading("Battery", 89, "%", 0x00B8, "battery"),
        Reading("Time of Use enabled", 1, "", 248, "tou", metric="deye_tou_enabled"),
        Reading(
            "Time of Use day mon", 1, "", 248, "tou", labels={"day": "mon"}, metric="deye_tou_day"
        ),
        Reading(
            "Time of Use slot 1 info",
            1,
            "",
            250,
            "tou",
            labels={
                "slot": "1",
                "start": "01:00",
                "power_w": "5000",
                "voltage_v": "49.00",
                "soc_pct": "85",
                "grid": "on",
                "gen": "off",
            },
            metric="deye_tou_slot_info",
        ),
    ]
    return Sample(readings=tuple(readings), serial="2601255637", error=error)


def test_write_then_read_round_trip(tmp_path) -> None:
    path = tmp_path / "state.json"
    write_snapshot(str(path), _sample(), interval=10.0)
    snapshot = read_snapshot(str(path))
    assert snapshot is not None
    assert snapshot.serial == "2601255637"
    assert snapshot.online is True
    assert snapshot.values["PV1 Power"] == 654
    assert snapshot.interval == 10.0


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path) -> None:
    path = tmp_path / "state.json"
    write_snapshot(str(path), _sample())
    assert sorted(item.name for item in tmp_path.iterdir()) == ["state.json"]
    json.loads(path.read_text())  # must be valid JSON


def test_write_creates_missing_directory(tmp_path) -> None:
    path = tmp_path / "nested" / "dir" / "state.json"
    write_snapshot(str(path), _sample())
    assert path.exists()


def test_error_sample_is_marked_offline(tmp_path) -> None:
    path = tmp_path / "state.json"
    write_snapshot(str(path), _sample(error="no reply"))
    snapshot = read_snapshot(str(path))
    assert snapshot is not None
    assert snapshot.online is False
    assert snapshot.error == "no reply"


def test_freshness_uses_interval(tmp_path) -> None:
    path = tmp_path / "state.json"
    now = time.time()
    write_snapshot(str(path), _sample(), interval=10.0, now=now)
    snapshot = read_snapshot(str(path))
    assert snapshot is not None
    assert snapshot.is_fresh(now=now + 5)
    assert not snapshot.is_fresh(now=now + 31)


def test_missing_file_returns_none(tmp_path) -> None:
    assert read_snapshot(str(tmp_path / "absent.json")) is None


def test_corrupt_file_returns_none(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert read_snapshot(str(path)) is None


def test_wrong_shape_returns_none(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1}))
    assert read_snapshot(str(path)) is None


def test_tou_block_is_rebuilt_from_readings() -> None:
    tou = tou_from_readings(_sample().readings)
    assert tou is not None
    assert tou["enabled"] is True
    assert tou["days"] == {"mon": True}
    slot = tou["slots"][0]
    assert slot["slot"] == 1
    assert slot["start"] == "01:00"
    assert slot["power_w"] == 5000
    assert slot["grid_charge"] is True
    assert slot["gen_charge"] is False


def test_tou_is_absent_without_tou_readings() -> None:
    readings = (Reading("PV1 Power", 654, "W", 0x00BA, "pv"),)
    assert tou_from_readings(readings) is None


def test_snapshot_age_is_never_negative() -> None:
    snapshot = CacheSnapshot(
        unix=time.time() + 100, serial=None, online=True, interval=10.0, values={}
    )
    assert snapshot.age() == 0.0
