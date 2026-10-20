"""Build the Grafana dashboard JSON from a compact panel description.

The dashboard is generated so panel ids, grid positions and datasource references stay
consistent. Commit the generated file; the test suite fails if it drifts.

Run with:  uv run python tools/build_dashboard.py
"""

from __future__ import annotations

import json
import pathlib

TARGET = (
    pathlib.Path(__file__).resolve().parent.parent / "Monitoring/grafana/dashboards/deye-6k.json"
)

DATASOURCE = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}

UNIT = {
    "watts": "watt",
    "volts": "volt",
    "amperes": "amp",
    "celsius": "celsius",
    "kwh": "kwatth",
    "percent": "percent",
    "hertz": "hertz",
}


def target(expr: str, legend: str, ref_id: str = "A", *, instant: bool = False) -> dict:
    return {
        "datasource": DATASOURCE,
        "expr": expr,
        "legendFormat": legend,
        "refId": ref_id,
        "instant": instant,
    }


def timeseries(
    title: str,
    targets: list[dict],
    grid: tuple[int, int, int, int],
    unit: str = "short",
    description: str = "",
) -> dict:
    x, y, w, h = grid
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {"fillOpacity": 8, "lineWidth": 2, "showPoints": "never"},
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": targets,
    }


def stat(
    title: str,
    targets: list[dict],
    grid: tuple[int, int, int, int],
    unit: str = "short",
    thresholds: list[dict] | None = None,
    description: str = "",
) -> dict:
    x, y, w, h = grid
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "thresholds": {"mode": "absolute", "steps": thresholds or []},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": "area",
            "justifyMode": "auto",
            "textMode": "auto",
        },
        "targets": targets,
    }


def table(title: str, grid: tuple[int, int, int, int], expr: str | None = None) -> dict:
    x, y, w, h = grid
    return {
        "type": "table",
        "title": title,
        "description": "Every exported gauge, for ad-hoc inspection.",
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"custom": {"align": "auto"}}, "overrides": []},
        "options": {"showHeader": True, "sortBy": [{"displayName": "Time", "desc": True}]},
        "targets": [
            {
                "datasource": DATASOURCE,
                "expr": expr or '{__name__=~"deye_.*", job="deye-modbus"}',
                "format": "table",
                "instant": True,
                "legendFormat": "__auto",
                "refId": "A",
            }
        ],
        "transformations": [
            {"id": "labelsToFields", "options": {}},
            {"id": "organize", "options": {"excludeByName": {"Time": True, "job": True}}},
        ],
    }


def build() -> dict:
    """Assemble the dashboard document."""
    panels = [
        stat(
            "PV power now",
            [target("sum(deye_pv1_power_watts)", "PV", "A", instant=True)],
            (0, 0, 6, 5),
            "watt",
            [{"color": "blue", "value": None}],
        ),
        stat(
            "Battery SOC",
            [target("deye_battery_percent", "SOC", "A", instant=True)],
            (6, 0, 6, 5),
            "percent",
            [
                {"color": "red", "value": None},
                {"color": "orange", "value": 20},
                {"color": "green", "value": 50},
            ],
        ),
        stat(
            "DC temperature",
            [target("deye_dc_temperature_celsius", "DC", "A", instant=True)],
            (12, 0, 3, 5),
            "celsius",
            [
                {"color": "green", "value": None},
                {"color": "orange", "value": 65},
                {"color": "red", "value": 75},
            ],
        ),
        stat(
            "AC temperature",
            [target("deye_ac_temperature_celsius", "AC", "A", instant=True)],
            (15, 0, 3, 5),
            "celsius",
            [
                {"color": "green", "value": None},
                {"color": "orange", "value": 65},
                {"color": "red", "value": 75},
            ],
        ),
        stat(
            "Data age",
            [target("deye_data_age_seconds", "age", "A", instant=True)],
            (18, 0, 3, 5),
            "s",
            [
                {"color": "green", "value": None},
                {"color": "orange", "value": 60},
                {"color": "red", "value": 180},
            ],
        ),
        stat(
            "Scrape success",
            [target("deye_scrape_success", "success", "A", instant=True)],
            (21, 0, 3, 5),
            "short",
            [{"color": "red", "value": None}, {"color": "green", "value": 1}],
        ),
        timeseries(
            "PV power",
            [
                target("deye_pv1_power_watts", "PV1", "A"),
                target("deye_pv2_power_watts", "PV2", "B"),
            ],
            (0, 5, 12, 8),
            "watt",
        ),
        timeseries(
            "Battery power",
            [target("deye_battery_power_watts", "battery", "A")],
            (12, 5, 12, 8),
            "watt",
            "Negative values charge the battery.",
        ),
        timeseries(
            "PV string",
            [
                target("deye_pv1_voltage_volts", "PV1 voltage", "A"),
                target("deye_pv1_current_amperes", "PV1 current", "B"),
            ],
            (0, 13, 12, 8),
            "short",
            "Mixed units on purpose: voltage and current share the string shape.",
        ),
        timeseries(
            "Battery voltage and current",
            [
                target("deye_battery_voltage_volts", "voltage", "A"),
                target("deye_battery_current_amperes", "current", "B"),
            ],
            (12, 13, 12, 8),
        ),
        timeseries(
            "Grid",
            [
                target("deye_grid_power_watts", "power", "A"),
                target("deye_grid_voltage_volts", "voltage", "B"),
                target("deye_grid_frequency_hertz", "frequency", "C"),
            ],
            (0, 21, 12, 8),
        ),
        timeseries(
            "Load",
            [
                target("deye_load_power_watts", "power", "A"),
                target("deye_load_l1_voltage_volts", "voltage", "B"),
            ],
            (12, 21, 12, 8),
        ),
        timeseries(
            "Inverter status",
            [
                target("deye_running_status", "{{status}}", "A"),
            ],
            (0, 29, 12, 6),
            "short",
            "One-hot series: exactly one status is 1 at any time.",
        ),
        stat(
            "Energy today",
            [
                target("deye_daily_pv_production_kwh", "PV", "A", instant=True),
                target("deye_daily_load_consumption_kwh", "load", "B", instant=True),
                target("deye_daily_energy_bought_kwh", "grid", "C", instant=True),
            ],
            (12, 29, 12, 6),
            "kwatth",
        ),
        table("All exported metrics", (0, 35, 24, 10)),
        table(
            "Time of Use schedule",
            (0, 45, 24, 10),
            expr='{__name__=~"deye_tou_.*", job="deye-modbus"}',
        ),
    ]

    for index, panel in enumerate(panels, start=1):
        panel["id"] = index

    return {
        "annotations": {"list": []},
        "editable": True,
        "graphTooltip": 1,
        "links": [],
        "panels": panels,
        "refresh": "30s",
        "schemaVersion": 39,
        "tags": ["deye", "inverter", "solar"],
        "templating": {
            "list": [
                {
                    "name": "DS_PROMETHEUS",
                    "label": "Prometheus",
                    "type": "datasource",
                    "query": "prometheus",
                    "current": {"text": "Prometheus", "value": "deye-prometheus"},
                    "hide": 0,
                    "refresh": 1,
                    "regex": "",
                },
                {
                    "name": "instance",
                    "label": "Instance",
                    "type": "query",
                    "datasource": DATASOURCE,
                    "query": 'label_values(up{job="deye-modbus"}, instance)',
                    "definition": 'label_values(up{job="deye-modbus"}, instance)',
                    "includeAll": True,
                    "multi": True,
                    "refresh": 2,
                },
            ]
        },
        "time": {"from": "now-24h", "to": "now"},
        "timezone": "browser",
        "title": "Deye 6K",
        "uid": "deye-6k",
        "version": 1,
        "weekStart": "",
    }


def main() -> None:
    """Write the dashboard JSON with a trailing newline."""
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {TARGET.name}")


if __name__ == "__main__":
    main()
