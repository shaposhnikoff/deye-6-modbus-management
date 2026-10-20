"""Local JSON cache: the handoff between the serial owner and other consumers.

The collector writes one snapshot per poll to a tmpfs path (``/run/deye/state.json`` by
default), replacing it atomically. Consumers (MQTT publisher, CLI, future exporters) read the
file and decide freshness themselves, so none of them depends on the collector being reachable
over the network.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from deye_monitoring.exporter import Sample
from deye_monitoring.snapshot import Reading, as_mapping

LOGGER = logging.getLogger(__name__)

CACHE_VERSION = 1
DEFAULT_PATH = os.environ.get("DEYE_CACHE_PATH", "/run/deye/state.json")


@dataclass(frozen=True)
class CacheSnapshot:
    """One cached poll, as read back from disk."""

    unix: float
    serial: str | None
    online: bool
    interval: float
    values: dict[str, float]
    tou: dict | None = None
    error: str | None = None

    def age(self, now: float | None = None) -> float:
        """Seconds since the snapshot was written."""
        return max((now if now is not None else time.time()) - self.unix, 0.0)

    def is_fresh(self, max_age: float | None = None, now: float | None = None) -> bool:
        """True when the snapshot is recent enough to be trusted."""
        limit = max_age if max_age is not None else self.interval * 3
        return self.age(now) <= limit


def tou_from_readings(readings: tuple[Reading, ...] | list[Reading]) -> dict | None:
    """Rebuild the nested TOU block from the labelled readings."""
    info = [reading for reading in readings if reading.metric == "deye_tou_slot_info"]
    if not info:
        return None
    enabled = next(
        (reading.value for reading in readings if reading.metric == "deye_tou_enabled"), 0
    )
    days = {
        (reading.labels or {})["day"]: bool(reading.value)
        for reading in readings
        if reading.metric == "deye_tou_day"
    }
    slots = []
    for reading in sorted(info, key=lambda item: int((item.labels or {})["slot"])):
        labels = reading.labels or {}
        slots.append(
            {
                "slot": int(labels["slot"]),
                "start": labels.get("start"),
                "power_w": int(labels.get("power_w", 0)),
                "voltage_v": float(labels.get("voltage_v", 0)),
                "soc_pct": int(labels.get("soc_pct", 0)),
                "grid_charge": labels.get("grid") == "on",
                "gen_charge": labels.get("gen") == "on",
            }
        )
    return {"enabled": bool(enabled), "days": days, "slots": slots}


def write_snapshot(
    path: str,
    sample: Sample,
    *,
    interval: float = 10.0,
    now: float | None = None,
) -> None:
    """Write *sample* to *path* atomically; failures are logged, never raised."""
    payload = {
        "version": CACHE_VERSION,
        "unix": now if now is not None else time.time(),
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "serial": sample.serial,
        "online": sample.error is None,
        "interval": interval,
        "error": sample.error,
        "values": as_mapping(list(sample.readings)),
        "tou": tou_from_readings(sample.readings),
    }
    tmp = f"{path}.tmp"
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as error:
        LOGGER.warning("could not write cache %s: %s", path, error)


def read_snapshot(path: str = DEFAULT_PATH) -> CacheSnapshot | None:
    """Read the cache, returning None when it is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as error:
        LOGGER.debug("cannot read cache %s: %s", path, error)
        return None
    try:
        return CacheSnapshot(
            unix=float(payload["unix"]),
            serial=payload.get("serial"),
            online=bool(payload.get("online")),
            interval=float(payload.get("interval", 10.0)),
            values=dict(payload.get("values") or {}),
            tou=payload.get("tou"),
            error=payload.get("error"),
        )
    except (KeyError, TypeError, ValueError) as error:
        LOGGER.warning("cache %s has an unexpected shape: %s", path, error)
        return None
