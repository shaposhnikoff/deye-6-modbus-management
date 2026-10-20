"""Prometheus metric naming derived from the register map.

Names are generated, never hand-maintained: adding a register to the map
automatically produces a correctly named metric.
"""

from __future__ import annotations

import re

from deye_monitoring.snapshot import Reading

UNIT_SUFFIX = {
    "W": "watts",
    "V": "volts",
    "A": "amperes",
    "kWh": "kwh",
    "°C": "celsius",
    "%": "percent",
    "Hz": "hertz",
    "": "",
}


def slugify(text: str) -> str:
    """Turn arbitrary text into a Prometheus-safe name fragment."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def unit_suffix(unit: str) -> str:
    """Return the metric name suffix for a unit, e.g. ``W`` -> ``watts``."""
    return UNIT_SUFFIX.get(unit, slugify(unit))


def metric_name(reading: Reading) -> str:
    """Return the full metric name for a reading, including the unit suffix."""
    base = f"deye_{slugify(reading.name)}"
    suffix = unit_suffix(reading.unit)
    return f"{base}_{suffix}" if suffix else base
