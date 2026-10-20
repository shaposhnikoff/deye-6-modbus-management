"""Validate the Prometheus/Grafana/Alertmanager configuration in Monitoring/.

These tests are the safety net for hand-edited dashboards: a metric rename in the exporter
must fail here instead of producing empty panels in production.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import pytest
import yaml

from deye_monitoring.exporter import (
    LAST_SUCCESS,
    RUNNING_STATUS_METRIC,
    SCRAPE_DURATION,
    SCRAPE_SUCCESS,
)
from deye_monitoring.metrics import metric_name
from deye_monitoring.registers import REGISTERS

ROOT = pathlib.Path(__file__).resolve().parent.parent
MONITORING = ROOT / "Monitoring"
DASHBOARD = MONITORING / "grafana/dashboards/deye-6k.json"

HEALTH_METRICS = {SCRAPE_SUCCESS, SCRAPE_DURATION, LAST_SUCCESS, "deye_data_age_seconds"}
KNOWN_METRICS = (
    {metric_name(register) for register in REGISTERS} | HEALTH_METRICS | {RUNNING_STATUS_METRIC}
)

METRIC_TOKEN = re.compile(r"\bdeye_[a-z0-9_]+\b")


@pytest.fixture(scope="module")
def dashboard() -> dict:
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def _walk_targets(panels: list[dict]) -> list[dict]:
    targets: list[dict] = []
    for panel in panels:
        targets.extend(panel.get("targets", []))
        for nested in panel.get("panels", []):
            targets.extend(nested.get("targets", []))
    return targets


def test_dashboard_file_exists_and_is_valid_json(dashboard: dict) -> None:
    assert DASHBOARD.exists()
    assert isinstance(dashboard["panels"], list)


def test_dashboard_identity(dashboard: dict) -> None:
    assert dashboard["uid"] == "deye-6k"
    assert dashboard["title"] == "Deye 6K"
    assert dashboard["refresh"] == "30s"
    assert dashboard["schemaVersion"] >= 39


def test_dashboard_has_a_datasource_variable(dashboard: dict) -> None:
    variables = {item["name"]: item for item in dashboard["templating"]["list"]}
    assert variables["DS_PROMETHEUS"]["type"] == "datasource"
    assert variables["DS_PROMETHEUS"]["query"] == "prometheus"


def test_every_target_uses_the_datasource_variable(dashboard: dict) -> None:
    for target in _walk_targets(dashboard["panels"]):
        datasource = target.get("datasource")
        assert datasource == {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}, target["expr"]


def test_every_expression_references_a_known_metric(dashboard: dict) -> None:
    referenced: set[str] = set()
    for target in _walk_targets(dashboard["panels"]):
        referenced.update(METRIC_TOKEN.findall(target["expr"]))
    unknown = referenced - KNOWN_METRICS
    assert not unknown, f"dashboard references unknown metrics: {sorted(unknown)}"
    assert referenced, "dashboard references no metrics at all"


def test_dashboard_covers_the_core_metrics(dashboard: dict) -> None:
    referenced = {
        name
        for target in _walk_targets(dashboard["panels"])
        for name in METRIC_TOKEN.findall(target["expr"])
    }
    required = {
        "deye_pv1_power_watts",
        "deye_battery_percent",
        "deye_battery_power_watts",
        "deye_grid_voltage_volts",
        "deye_load_power_watts",
        "deye_dc_temperature_celsius",
        "deye_scrape_success",
    }
    assert required <= referenced


def test_panel_ids_and_positions_are_sane(dashboard: dict) -> None:
    ids = [panel["id"] for panel in dashboard["panels"]]
    assert len(ids) == len(set(ids))
    for panel in dashboard["panels"]:
        grid = panel["gridPos"]
        assert 0 <= grid["x"] < 24
        assert grid["x"] + grid["w"] <= 24, panel["title"]
        assert grid["h"] > 0 and grid["w"] > 0


def test_committed_dashboard_matches_the_generator(dashboard: dict) -> None:
    spec = importlib.util.spec_from_file_location(
        "build_dashboard", ROOT / "tools/build_dashboard.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.build() == dashboard, "run: uv run python tools/build_dashboard.py"


def test_prometheus_config_is_valid() -> None:
    config = yaml.safe_load((MONITORING / "prometheus/prometheus.yml").read_text())
    jobs = {job["job_name"] for job in config["scrape_configs"]}
    assert "deye" in jobs
    assert "deye-alerts.yml" in config["rule_files"]
    target = config["scrape_configs"][0]["static_configs"][0]["targets"][0]
    assert target.endswith(":9101")


def test_scrape_snippet_is_reusable() -> None:
    snippet = yaml.safe_load((MONITORING / "prometheus/deye-scrape.yml").read_text())
    assert snippet[0]["job_name"] == "deye"
    assert snippet[0]["static_configs"][0]["targets"]


def test_alert_rules_reference_known_metrics() -> None:
    rules = yaml.safe_load((MONITORING / "prometheus/deye-alerts.yml").read_text())
    alerts = {rule["alert"]: rule for group in rules["groups"] for rule in group["rules"]}
    assert {"DeyeScrapeFailing", "DeyeDataStale", "DeyeOverTemperature", "DeyeFault"} <= set(alerts)
    for rule in alerts.values():
        unknown = set(METRIC_TOKEN.findall(rule["expr"])) - KNOWN_METRICS
        assert not unknown, f"{rule['alert']} references unknown metrics: {sorted(unknown)}"


def test_every_critical_alert_has_annotations() -> None:
    rules = yaml.safe_load((MONITORING / "prometheus/deye-alerts.yml").read_text())
    for group in rules["groups"]:
        for rule in group["rules"]:
            assert rule["annotations"]["summary"]
            assert rule["labels"]["severity"] in {"warning", "critical"}


def test_alertmanager_routes_to_telegram() -> None:
    config = yaml.safe_load((MONITORING / "alertmanager/alertmanager.yml").read_text())
    receivers = {receiver["name"]: receiver for receiver in config["receivers"]}
    assert config["route"]["receiver"] in receivers
    telegram = receivers["telegram"]["telegram_configs"][0]
    assert "TELEGRAM_BOT_TOKEN" in telegram["bot_token"]
    assert telegram["send_resolved"] is True


def test_grafana_provisioning_files_are_valid() -> None:
    datasources = yaml.safe_load(
        (MONITORING / "grafana/provisioning/datasources/prometheus.yml").read_text()
    )
    assert datasources["datasources"][0]["uid"] == "deye-prometheus"
    assert datasources["datasources"][0]["type"] == "prometheus"

    providers = yaml.safe_load(
        (MONITORING / "grafana/provisioning/dashboards/deye.yml").read_text()
    )
    provider = providers["providers"][0]
    assert provider["type"] == "file"
    assert provider["options"]["path"].endswith("/dashboards/deye")
