"""Tests for the Prometheus text exposition rendering."""

from __future__ import annotations

from deye_monitoring.exporter import Sample, render
from deye_monitoring.snapshot import Reading

SERIAL = "2601255637"


def _sample(**overrides: object) -> Sample:
    readings = (
        Reading(name="PV1 Power", value=654, unit="W", address=0x00BA, group="pv"),
        Reading(name="Battery", value=89, unit="%", address=0x00B8, group="battery"),
        Reading(name="Running Status", value=2, unit="", address=0x003B, group="device"),
    )
    defaults: dict[str, object] = {
        "readings": readings,
        "serial": SERIAL,
        "duration": 0.42,
        "last_success": 1_700_000_000.0,
        "error": None,
    }
    defaults.update(overrides)
    return Sample(**defaults)  # type: ignore[arg-type]


def test_render_includes_help_and_type() -> None:
    text = render(_sample())
    assert "# HELP deye_pv1_power_watts PV1 Power" in text
    assert "# TYPE deye_pv1_power_watts gauge" in text


def test_render_labels_include_group_and_serial() -> None:
    text = render(_sample())
    assert 'deye_pv1_power_watts{group="pv",serial="2601255637"} 654' in text
    assert 'deye_battery_percent{group="battery",serial="2601255637"} 89' in text


def test_render_reports_successful_scrape() -> None:
    text = render(_sample())
    assert "deye_scrape_success 1" in text
    assert "deye_scrape_duration_seconds 0.420" in text
    assert "deye_last_successful_scrape_timestamp_seconds 1700000000" in text


def test_render_reports_failed_scrape_without_values() -> None:
    text = render(_sample(error="no reply"))
    assert "deye_scrape_success 0" in text
    assert "deye_pv1_power_watts" not in text


def test_running_status_is_one_hot() -> None:
    text = render(_sample())
    assert 'deye_running_status{status="normal",serial="2601255637"} 1' in text
    assert 'deye_running_status{status="fault",serial="2601255637"} 0' in text
    assert "deye_running_status" in text
    assert "deye_running_status{" in text
    # The raw register must not also appear as a plain gauge.
    assert "deye_running_status_no_unit" not in text


def test_running_status_all_zero_when_scrape_failed() -> None:
    text = render(_sample(error="no reply"))
    assert 'deye_running_status{status="normal",serial="2601255637"} 0' in text


def test_data_age_is_reported() -> None:
    text = render(_sample())
    assert "deye_data_age_seconds" in text
    assert "# TYPE deye_data_age_seconds gauge" in text


def test_serial_with_quotes_is_escaped() -> None:
    text = render(_sample(serial='a"b'))
    assert 'serial="a\\"b"' in text


def test_render_ends_with_newline() -> None:
    assert render(_sample()).endswith("\n")
