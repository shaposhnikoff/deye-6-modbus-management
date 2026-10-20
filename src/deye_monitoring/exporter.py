"""Render a snapshot in the Prometheus text exposition format."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from deye_monitoring.enums import RUNNING_STATUS, RUNNING_STATUS_ADDRESS
from deye_monitoring.metrics import metric_name
from deye_monitoring.snapshot import Reading

SCRAPE_SUCCESS = "deye_scrape_success"
SCRAPE_DURATION = "deye_scrape_duration_seconds"
LAST_SUCCESS = "deye_last_successful_scrape_timestamp_seconds"
RUNNING_STATUS_METRIC = "deye_running_status"


@dataclass(frozen=True)
class Sample:
    """One poll of the inverter, complete enough to render metrics from."""

    readings: tuple[Reading, ...] = field(default_factory=tuple)
    serial: str | None = None
    duration: float = 0.0
    last_success: float = 0.0
    error: str | None = None


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _status_value(sample: Sample) -> int | None:
    for reading in sample.readings:
        if reading.address == RUNNING_STATUS_ADDRESS:
            return int(reading.value)
    return None


def render(sample: Sample) -> str:
    """Render *sample* as a Prometheus text exposition body."""
    lines: list[str] = []
    seen: set[str] = set()
    serial = _escape_label(sample.serial or "unknown")

    if sample.error is None:
        for reading in sample.readings:
            if reading.address == RUNNING_STATUS_ADDRESS:
                continue
            name = metric_name(reading)
            if name not in seen:
                lines.append(f"# HELP {name} {reading.name}")
                lines.append(f"# TYPE {name} gauge")
                seen.add(name)
            labels = f'group="{_escape_label(reading.group)}",serial="{serial}"'
            lines.append(f"{name}{{{labels}}} {reading.value}")

    status = _status_value(sample)
    lines.append(f"# HELP {RUNNING_STATUS_METRIC} Inverter running status as a one-hot gauge")
    lines.append(f"# TYPE {RUNNING_STATUS_METRIC} gauge")
    for code, label in RUNNING_STATUS.items():
        active = 1 if (sample.error is None and status == code) else 0
        lines.append(f'{RUNNING_STATUS_METRIC}{{status="{label}",serial="{serial}"}} {active}')

    age = max(time.time() - sample.last_success, 0.0) if sample.last_success else 0.0
    lines.append(f"# HELP {SCRAPE_SUCCESS} 1 when the last Modbus poll succeeded")
    lines.append(f"# TYPE {SCRAPE_SUCCESS} gauge")
    lines.append(f"{SCRAPE_SUCCESS} {0 if sample.error else 1}")
    lines.append(f"# HELP {SCRAPE_DURATION} Duration of the last Modbus poll in seconds")
    lines.append(f"# TYPE {SCRAPE_DURATION} gauge")
    lines.append(f"{SCRAPE_DURATION} {sample.duration:.3f}")
    lines.append(f"# HELP {LAST_SUCCESS} Unix timestamp of the last successful poll")
    lines.append(f"# TYPE {LAST_SUCCESS} gauge")
    lines.append(f"{LAST_SUCCESS} {sample.last_success:.0f}")
    lines.append("# HELP deye_data_age_seconds Seconds since the last successful poll")
    lines.append("# TYPE deye_data_age_seconds gauge")
    lines.append(f"deye_data_age_seconds {age:.1f}")

    return "\n".join(lines) + "\n"


def write_to(path: str, body: str) -> None:
    """Write metrics atomically, for the node_exporter textfile collector."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.replace(tmp, path)
