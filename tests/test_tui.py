"""Tests for the console dashboard helpers (no curses needed)."""

from __future__ import annotations

import time

from deye_monitoring.cache import CacheSnapshot
from deye_monitoring.tui import (
    CRIT,
    OK,
    WARN,
    bar,
    battery_status,
    build_overview,
    build_tou,
    format_age,
    freq_status,
    render_text,
    temp_status,
)

VALUES = {
    "PV1 Power": 654.0,
    "PV2 Power": 12.0,
    "PV1 Voltage": 130.9,
    "PV1 Current": 5.6,
    "Battery": 89.0,
    "Battery Voltage": 53.86,
    "Battery Current": -12.17,
    "Battery Power": -655.0,
    "Battery Temperature": 19.0,
    "Grid Voltage": 216.3,
    "Grid Frequency": 50.01,
    "Grid Power": 21.0,
    "Load Power": 21.0,
    "Load L1 Voltage": 218.5,
    "DC Temperature": 68.4,
    "AC Temperature": 35.5,
    "Daily PV Production": 3.4,
    "Daily Load Consumption": 2.8,
}


def _snapshot(**overrides: object) -> CacheSnapshot:
    defaults: dict[str, object] = {
        "unix": time.time() - 3,
        "serial": "2601255637",
        "online": True,
        "interval": 10.0,
        "values": dict(VALUES),
        "tou": {
            "enabled": True,
            "days": {"mon": True, "tue": True, "wed": False},
            "slots": [
                {
                    "slot": 1,
                    "start": "01:00",
                    "power_w": 5000,
                    "voltage_v": 49.0,
                    "soc_pct": 85,
                    "grid_charge": True,
                    "gen_charge": False,
                },
                {
                    "slot": 2,
                    "start": "05:00",
                    "power_w": 6000,
                    "voltage_v": 49.0,
                    "soc_pct": 85,
                    "grid_charge": True,
                    "gen_charge": True,
                },
            ],
        },
    }
    defaults.update(overrides)
    return CacheSnapshot(**defaults)  # type: ignore[arg-type]


def test_bar_clamps_and_fills() -> None:
    assert bar(0, 0, 100, width=10) == "[----------]"
    assert bar(100, 0, 100, width=10) == "[##########]"
    assert bar(50, 0, 100, width=10) == "[#####-----]"
    assert bar(-10, 0, 100, width=10) == "[----------]"
    assert bar(150, 0, 100, width=10) == "[##########]"


def test_battery_status_thresholds() -> None:
    assert battery_status(90) == OK
    assert battery_status(25) == WARN
    assert battery_status(10) == CRIT


def test_temp_status_thresholds() -> None:
    assert temp_status(35) == OK
    assert temp_status(60) == WARN
    assert temp_status(71) == CRIT


def test_freq_status() -> None:
    assert freq_status(50.0) == OK
    assert freq_status(47.5) == WARN


def test_format_age() -> None:
    assert format_age(5) == "5s"
    assert format_age(65) == "1m05s"
    assert format_age(3700) == "1h01m"
    assert format_age(-5) == "0s"


def test_overview_has_all_sections() -> None:
    rows = build_overview(VALUES)
    sections = [row.section for row in rows if row.section]
    assert sections == ["PV", "BATTERY", "GRID", "LOAD", "INVERTER"]


def test_overview_sums_pv_strings() -> None:
    rows = {row.label: row for row in build_overview(VALUES) if row.label}
    assert rows["PV power"].text == "666 W"


def test_overview_marks_hot_inverter() -> None:
    rows = {row.label: row for row in build_overview(VALUES) if row.label}
    assert rows["DC temperature"].status == WARN  # 68.4 >= 60
    assert rows["AC temperature"].status == OK


def test_overview_shows_battery_gauge() -> None:
    rows = {row.label: row for row in build_overview(VALUES) if row.label}
    assert "89 %" in rows["State of charge"].text
    assert "#" in rows["State of charge"].text
    assert rows["State of charge"].status == OK


def test_overview_skips_missing_values() -> None:
    rows = build_overview({"PV1 Power": 100.0})
    labels = [row.label for row in rows if row.label]
    assert labels == ["PV power"]


def test_tou_rows() -> None:
    rows = build_tou(_snapshot())
    labels = [row.label for row in rows]
    assert "TOU enabled" in labels
    slot_rows = [row for row in rows if row.label.startswith("Slot")]
    assert len(slot_rows) == 2
    assert "grid" in slot_rows[1].text and "gen" in slot_rows[1].text


def test_tou_disabled_is_a_warning() -> None:
    snapshot = _snapshot(tou={"enabled": False, "days": {}, "slots": []})
    rows = {row.label: row for row in build_tou(snapshot)}
    assert rows["TOU enabled"].status == WARN


def test_render_text_when_cache_missing() -> None:
    assert "waiting for cache" in render_text(None, "/run/deye/state.json")


def test_render_text_contains_sections_and_values() -> None:
    text = render_text(_snapshot(), "/run/deye/state.json")
    assert "PV power" in text
    assert "666 W" in text
    assert "[BATTERY]" in text
    assert "[TIME OF USE]" in text
    assert "Slot 1" in text
    assert "serial 2601255637" in text


def test_render_text_marks_offline() -> None:
    text = render_text(_snapshot(online=False), "/x")
    assert "OFFLINE" in text
