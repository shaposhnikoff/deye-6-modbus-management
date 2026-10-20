"""Tests for Prometheus metric naming."""

from __future__ import annotations

from deye_monitoring.metrics import metric_name, slugify, unit_suffix
from deye_monitoring.snapshot import Reading


def _reading(name: str, unit: str, address: int = 0x00BA, group: str = "pv") -> Reading:
    return Reading(name=name, value=1.0, unit=unit, address=address, group=group)


def test_slugify_keeps_simple_names() -> None:
    assert slugify("PV1 Power") == "pv1_power"


def test_slugify_collapses_separators() -> None:
    assert slugify("Battery (SOC) %") == "battery_soc"
    assert slugify("  Grid L1--Voltage ") == "grid_l1_voltage"


def test_unit_suffix_known_units() -> None:
    assert unit_suffix("W") == "watts"
    assert unit_suffix("V") == "volts"
    assert unit_suffix("A") == "amperes"
    assert unit_suffix("kWh") == "kwh"
    assert unit_suffix("°C") == "celsius"
    assert unit_suffix("%") == "percent"
    assert unit_suffix("Hz") == "hertz"
    assert unit_suffix("") == ""


def test_unit_suffix_unknown_unit_is_slugified() -> None:
    assert unit_suffix("VAr") == "var"


def test_metric_name_includes_prefix_and_unit() -> None:
    assert metric_name(_reading("PV1 Power", "W")) == "deye_pv1_power_watts"


def test_metric_name_without_unit() -> None:
    assert metric_name(_reading("Device MPPTs", "")) == "deye_device_mppts"


def test_metric_name_for_temperature_and_percent() -> None:
    assert metric_name(_reading("DC Temperature", "°C")) == "deye_dc_temperature_celsius"
    assert metric_name(_reading("Battery", "%")) == "deye_battery_percent"
