# DEYE Monitoring Roadmap: Prometheus exporter, MQTT/HA, alerts, guarded writes

> **For the implementing agent:** follow this plan task by task, tests first. Every task ends with a green test run and a commit.

**Goal:** Turn the working read-only CLI into a supervised monitoring stack — Prometheus exporter on the Pi, Prometheus scrape config and alert rules, a provisioned Grafana dashboard, MQTT bridge into Home Assistant, Telegram delivery through Alertmanager, and a separately gated write module.

**Architecture:** One long-lived collector process owns the serial port and polls the inverter on a fixed interval. Scrapes, MQTT publishes and health checks all read the in-memory snapshot, so the RS485 line is never touched by two things at once. Prometheus scrapes the exporter, Grafana renders that data from a dashboard stored in git, and MQTT carries the same snapshot to Home Assistant. Writing to the inverter lives in its own module and CLI, is never imported by the service, requires an explicit `--confirm`, and is restricted to an allow-list of registers.

**Tech stack:** Python 3.13, uv, pyserial, stdlib `http.server` (exporter), `paho-mqtt` (optional extra), systemd, Prometheus + Alertmanager, Grafana (file provisioning), Mosquitto, Home Assistant MQTT discovery.

**Deployment constraint:** all Prometheus, Grafana and Alertmanager configuration lives in the `Monitoring/` directory of this repository. The operator copies it wherever it belongs; the plan assumes no writable config directory and no specific host layout. The Mosquitto host is unknown, so every MQTT and Telegram setting arrives through environment variables and the exporter must work without them.

**Already in place:** repository `shaposhnikoff/deye-6-modbus-management`, package `deye_monitoring` with FC3-only client, generated register map, CLI `dmon read|json|dump|watch`, 18 tests, udev rule giving `/dev/modbus`, working on the Pi at `/home/max/DEYE Monitoring`.

---

## Ground rules

- Monitoring stays read-only. `deye_monitoring.modbus` gains no write functions.
- Writes live in `deye_monitoring.write`, are imported only by `dmon-write`, and default to dry-run.
- The serial port has one owner: the collector thread. CLI commands keep their own short-lived connection (they are for humans, not for automation).
- Metric and topic names are derived from the register map, so adding a register never means editing the exporter by hand.
- Every phase keeps `uv run pytest` and `uv run ruff check .` green.

---

## Phase 1 — Prometheus exporter

**Outcome:** `dmon serve` exposes `http://192.168.10.39:9101/metrics` with ~63 gauges, scrape and staleness metrics, and `/health`.

### Task 1.1: Metric naming

**Files:**
- Create: `src/deye_monitoring/metrics.py`
- Test: `tests/test_metrics.py`

**Step 1: Write the failing test**

```python
from deye_monitoring.metrics import metric_name, unit_suffix
from deye_monitoring.snapshot import Reading


def test_unit_suffix_known_units():
    assert unit_suffix("W") == "watts"
    assert unit_suffix("kWh") == "kwh"
    assert unit_suffix("°C") == "celsius"
    assert unit_suffix("") == ""


def test_metric_name_is_prometheus_safe():
    reading = Reading(name="PV1 Power", value=654, unit="W", address=0x00BA, group="pv")
    assert metric_name(reading) == "deye_pv1_power_watts"


def test_metric_name_handles_parentheses_and_percent():
    reading = Reading(name="Battery (SOC)", value=89, unit="%", address=0x00B8, group="battery")
    assert metric_name(reading) == "deye_battery_soc_percent"
```

**Step 2: Run it, expect `ImportError`**

`uv run pytest tests/test_metrics.py -q`

**Step 3: Implement**

```python
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
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def unit_suffix(unit: str) -> str:
    return UNIT_SUFFIX.get(unit, slugify(unit))


def metric_name(reading: Reading) -> str:
    suffix = unit_suffix(reading.unit)
    base = f"deye_{slugify(reading.name)}"
    return f"{base}_{suffix}" if suffix else base
```

**Step 4: Run tests, expect PASS**

**Step 5: Commit** — `feat: add prometheus metric naming`

### Task 1.2: Text exposition rendering

**Files:**
- Create: `src/deye_monitoring/exporter.py`
- Test: `tests/test_exporter.py`

**Step 1: Failing test** — render a snapshot and assert:

```python
def test_render_contains_help_type_and_labels(sample):
    text = render(sample)
    assert "# TYPE deye_pv1_power_watts gauge" in text
    assert 'deye_pv1_power_watts{group="pv",serial="2601255637"} 654' in text


def test_render_reports_scrape_health(sample):
    assert "deye_scrape_success 1" in text
    assert "deye_last_successful_scrape_timestamp_seconds" in text


def test_render_marks_failed_scrape(sample_with_error):
    assert "deye_scrape_success 0" in text
    assert "deye_pv1_power_watts" not in render(sample_with_error)
```

**Step 2: Implement** — `Sample` dataclass (`readings`, `serial`, `scraped_at`, `duration`, `error`) and:

```python
def render(sample: Sample) -> str:
    lines: list[str] = []
    if sample.error is None:
        for reading in sample.readings:
            name = metric_name(reading)
            if name not in seen:
                lines += [f"# HELP {name} {reading.name}", f"# TYPE {name} gauge"]
                seen.add(name)
            lines.append(
                f'{name}{{group="{reading.group}",serial="{sample.serial}"}} {reading.value}'
            )
    lines.append("# TYPE deye_scrape_success gauge")
    lines.append(f"deye_scrape_success {0 if sample.error else 1}")
    lines.append("# TYPE deye_scrape_duration_seconds gauge")
    lines.append(f"deye_scrape_duration_seconds {sample.duration:.3f}")
    lines.append("# TYPE deye_last_successful_scrape_timestamp_seconds gauge")
    lines.append(f"deye_last_successful_scrape_timestamp_seconds {sample.last_success:.0f}")
    return "\n".join(lines) + "\n"
```

**Step 3: Run tests, expect PASS. Step 4: Commit** — `feat: render prometheus text exposition`

### Task 1.3: Single-owner collector

**Files:**
- Create: `src/deye_monitoring/collector.py`
- Test: `tests/test_collector.py`

**Step 1: Failing test** — inject a fake reader so no hardware is needed:

```python
def test_collector_caches_snapshot_between_scrapes(fake_reader):
    collector = Collector(fake_reader, interval=0.01)
    collector.poll_once()
    assert collector.latest().error is None
    assert fake_reader.calls == 1
    collector.latest()  # serving a cached snapshot must not poll
    assert fake_reader.calls == 1


def test_collector_keeps_last_good_values_on_error(fake_reader):
    collector = Collector(fake_reader, interval=0.01)
    collector.poll_once()
    fake_reader.fail = True
    collector.poll_once()
    assert collector.latest().error is not None
    assert collector.last_success > 0
```

**Step 2: Implement** — `Collector` takes a callable returning `(registers, readings, serial)`; `poll_once()` wraps it in try/except, stores `Sample`, records duration; `start()`/`stop()` drive a daemon thread with `threading.Event`.

**Step 3: Run tests, expect PASS. Step 4: Commit** — `feat: add single-owner collector thread`

### Task 1.4: HTTP endpoints

**Files:**
- Create: `src/deye_monitoring/web.py`
- Test: `tests/test_web.py` (start server on port 0, request with `urllib`)

**Step 1: Failing test**

```python
def test_metrics_endpoint_returns_exposition(server):
    body = urllib.request.urlopen(server.url("/metrics")).read().decode()
    assert "deye_scrape_success" in body


def test_health_endpoint_returns_json(server):
    payload = json.loads(urllib.request.urlopen(server.url("/health")).read())
    assert payload["ok"] is True and "age_seconds" in payload


def test_unknown_path_returns_404(server):
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(server.url("/nope"))
    assert error.value.code == 404
```

**Step 2: Implement** — `ThreadingHTTPServer`, handler with `/metrics`, `/health`, 404 otherwise, `daemon_threads = True`. `/health` returns `{"ok": latest.error is None, "age_seconds": ..., "serial": ..., "error": ...}` and status 200 or 503.

**Step 3: Run tests, expect PASS. Step 4: Commit** — `feat: expose metrics and health over http`

### Task 1.5: `dmon serve` command

**Files:**
- Modify: `src/deye_monitoring/cli.py`
- Test: `tests/test_cli.py` (parser-level test only, no socket)

**Step 1: Failing test** — `build_parser().parse_args(["serve", "--listen", "0.0.0.0:9101", "--interval", "10"])` yields those values; `--listen` defaults to `0.0.0.0:9101`, interval to `10.0`.

**Step 2: Implement** — subparser with `--listen`, `--interval`, `--device-timeout`; `cmd_serve` starts collector, starts HTTP server, blocks until `SIGINT`/`SIGTERM`, then `stop()`.

**Step 3: Run on the Pi**

```bash
cd "/home/max/DEYE Monitoring" && uv run dmon serve --listen 0.0.0.0:9101 &
curl -s localhost:9101/metrics | head -20
curl -s localhost:9101/health
```

Expected: `deye_pv1_power_watts{group="pv",serial="2601255637"} <value>`, `"ok": true`.

**Step 4: Commit** — `feat: add serve command with exporter and health endpoints`

### Task 1.6: Smoke-check the exporter from Prometheus

**Files:**
- Modify: `README.md` (mention the scrape target; the full Prometheus config lands in Task 4.1)

**Step 1: Manual scrape check from the Prometheus host**

```bash
curl -s http://192.168.10.39:9101/metrics | grep -c '^deye_'
```

Expected: 60+ metric lines.

**Step 2: Add the target to Prometheus** — full configuration, rules and validation are Task 4.1 so there is exactly one authoritative copy of `deye-scrape.yml`.

**Step 3: Commit** — `docs: document the exporter scrape target`

---

## Phase 2 — MQTT bridge for Home Assistant

**Outcome:** the same snapshot lands in Home Assistant via MQTT discovery, with no YAML in HA.

### Task 2.1: Optional dependency and publisher

**Files:**
- Modify: `pyproject.toml` (`[project.optional-dependencies] mqtt = ["paho-mqtt>=2.1"]`)
- Create: `src/deye_monitoring/mqtt.py`
- Test: `tests/test_mqtt.py` (fake client object, no broker)

**Step 1: Failing test**

```python
def test_state_payload_matches_snapshot(fake_client, sample):
    publish_state(fake_client, "deye/state", sample)
    topic, payload, retain = fake_client.published[0]
    assert topic == "deye/state" and retain is True
    assert json.loads(payload)["values"]["PV1 Power"] == 654


def test_discovery_config_has_unit_and_state_topic(fake_client, sample):
    publish_discovery(fake_client, prefix="homeassistant", node="deye_6k", sample=sample)
    configs = [json.loads(p[1]) for p in fake_client.published]
    pv = next(c for c in configs if c["name"] == "PV1 Power")
    assert pv["unit_of_measurement"] == "W"
    assert pv["state_topic"] == "deye/state"
    assert pv["value_template"] == "{{ value_json.values['PV1 Power'] }}"
    assert pv["unique_id"].startswith("deye_6k_")
```

**Step 2: Implement** — `publish_state` (retained JSON, `availability_topic`), `publish_discovery` (one retained config per register, `device` block grouping all entities), `availability` = `online`/`offline`, LWT via `will_set`.

**Step 3: Commit** — `feat: publish snapshot and discovery to mqtt`

### Task 2.2: `dmon mqtt` command

**Files:** `src/deye_monitoring/cli.py`, `tests/test_cli.py`

Flags: `--host` (default `localhost`), `--port 1883`, `--username/--password` (env `DEYE_MQTT_USER/PASSWORD`), `--prefix homeassistant`, `--node deye_6k`, `--interval 10`. Reuse `Collector`, publish after each poll, publish `offline` on exit.

**Verify on the Pi** (only if Mosquitto is reachable): `mosquitto_sub -h <broker> -t 'deye/#' -v` shows state updates every interval, and HA shows the device with ~60 entities.

**Commit** — `feat: add mqtt command with home assistant discovery`

---

## Phase 3 — Supervision and deployment

**Outcome:** survives reboot, restarts on failure, logs to journald, no manual SSH.

### Task 3.1: systemd unit

**Files:** Create `deploy/systemd/deye-monitoring.service`, `deploy/systemd/deye-monitoring.env.example`

```ini
[Unit]
Description=Deye inverter monitoring (Prometheus exporter)
After=network-online.target dev-modbus.device
Wants=network-online.target

[Service]
Type=simple
User=max
WorkingDirectory=/home/max/DEYE Monitoring
EnvironmentFile=-/etc/deye-monitoring.env
ExecStart=/home/max/.local/bin/uv run dmon serve --listen 0.0.0.0:9101 --interval 10
Restart=always
RestartSec=15
MemoryMax=256M

[Install]
WantedBy=multi-user.target
```

**Verify:** `sudo systemctl enable --now deye-monitoring`, `systemctl status`, `journalctl -u deye-monitoring -n 20`, reboot the Pi, `curl localhost:9101/health`.

**Commit** — `feat: add systemd unit for the exporter`

### Task 3.2: Ops runbook

**Files:** Create `docs/OPS.md` — install from scratch (uv, udev, systemd), wiring pinout, troubleshooting table (no reply → swap A/B → check baud/slave → check power), log locations, how to rotate/replace the adapter (update udev serial), upgrade procedure (`git pull`, `uv sync`, restart), backup of nothing-but-config.

**Commit** — `docs: add operations runbook`

### Task 3.3: Diagrams

**Files:** Create `docs/diagrams/service-topology.md` (flowchart: inverter → adapter → collector → exporter/MQTT → Prometheus/HA → Alertmanager/Telegram) and extend `docs/diagrams/modbus-read-flow.md` with the collector cache step.

**Commit** — `docs: add service topology diagram`

---

## Phase 4 — Grafana dashboard and provisioning

**Outcome:** a dashboard kept in git, provisioned into Grafana, reading the exporter through Prometheus. Replacing the Pi must not mean rebuilding panels by hand.

### Task 4.1: Prometheus configuration

**Files:**
- Create: `Monitoring/prometheus/prometheus.yml` (complete, runnable config)
- Create: `Monitoring/prometheus/deye-scrape.yml` (snippet for an existing server)
- Create: `docs/PROMETHEUS.md` (where the config lives, how to reload, retention note)

```yaml
# Monitoring/prometheus/prometheus.yml
global:
  scrape_interval: 30s
  evaluation_interval: 30s
  external_labels:
    site: home

rule_files:
  - deye-alerts.yml

scrape_configs:
  - job_name: deye
    scrape_timeout: 10s
    static_configs:
      - targets: ["192.168.10.39:9101"]
        labels:
          inverter: sun6k
          model: SUN-6K-SG05LP1
```

**Step 1: Validate before touching a running server**

```bash
promtool check config Monitoring/prometheus/prometheus.yml
promtool check rules Monitoring/prometheus/deye-alerts.yml
```

Expected: `SUCCESS: ... is valid`.

**Step 2: Verify the target is UP after reload**

```bash
curl -s localhost:9090/api/v1/targets | jq -r '.data.activeTargets[] | select(.labels.job=="deye") | "\(.health) \(.lastError)"'
```

Expected: `up`.

**Step 3: Commit** — `feat: add prometheus configuration for the exporter`

### Task 4.2: Dashboard JSON

**Files:**
- Create: `Monitoring/grafana/dashboards/deye-6k.json`
- Create: `tests/test_dashboard.py`

Dashboard contract: `uid: deye-6k`, title `Deye 6K`, `schemaVersion` matching the target Grafana, refresh `30s`, default range `now-24h`, datasource variable `${DS_PROMETHEUS}`, and an `$instance` variable from `label_values(up{job="deye"}, instance)`.

Panels (all `expr` must reference metrics produced by the exporter):

| Panel | Type | Expression |
|---|---|---|
| PV power | timeseries | `deye_pv1_power_watts` (+ `deye_pv2_power_watts` when non-zero) |
| PV string | timeseries | `deye_pv1_voltage_volts`, `deye_pv1_current_amperes` |
| Battery SOC | gauge + timeseries | `deye_battery_percent` with thresholds 20/50 |
| Battery power | timeseries | `deye_battery_power_watts` (negative = charging) |
| Battery voltage | timeseries | `deye_battery_voltage_volts` |
| Grid | timeseries | `deye_grid_power_watts`, `deye_grid_voltage_volts`, `deye_grid_frequency_hertz` |
| Load | timeseries | `deye_load_power_watts`, `deye_load_l1_voltage_volts` |
| Temperatures | stat + timeseries | `deye_dc_temperature_celsius`, `deye_ac_temperature_celsius`, thresholds 65/75 |
| Energy today | stat | `deye_daily_pv_production_kwh`, `deye_daily_load_consumption_kwh`, `deye_daily_energy_bought_kwh` |
| Inverter status | stat with value mappings | `deye_running_status{status="normal"}` etc. |
| Scrape health | stat | `deye_scrape_success`, `time() - deye_last_successful_scrape_timestamp_seconds` |
| All metrics | table | `{__name__=~"deye_.*"}` |

**Step 1: Write the failing test** — the dashboard must not reference metrics that the code does not export:

```python
def test_every_dashboard_expression_uses_a_known_metric():
    dashboard = json.loads(DASHBOARD.read_text())
    expressions = extract_expressions(dashboard)  # walk panels + targets
    known = {metric_name(r) for r in REGISTERS} | {
        "deye_scrape_success",
        "deye_last_successful_scrape_timestamp_seconds",
        "deye_scrape_duration_seconds",
        "deye_running_status",
    }
    referenced = {name for expr in expressions for name in re.findall(r"deye_[a-z0-9_]+", expr)}
    assert referenced <= known, f"unknown metrics in dashboard: {referenced - known}"


def test_dashboard_has_uid_title_and_refresh(): ...


def test_dashboard_panels_have_unique_ids_and_grid_positions(): ...
```

**Step 2: Run it, expect FAIL** (dashboard file missing).

**Step 3: Build the dashboard** — author JSON in the Grafana UI model (or by hand), then export. Keep `__inputs` empty because the datasource is a template variable, so import via provisioning needs no manual mapping.

**Step 4: Run tests, expect PASS. Step 5: Commit** — `feat: add grafana dashboard for exporter metrics`

### Task 4.3: Provisioning and verification

**Files:**
- Create: `Monitoring/grafana/provisioning/datasources/prometheus.yml`
- Create: `Monitoring/grafana/provisioning/dashboards/deye.yml`
- Create: `docs/GRAFANA.md`

```yaml
# datasources/prometheus.yml
apiVersion: 1
datasources:
  - name: Prometheus
    uid: deye-prometheus
    type: prometheus
    access: proxy
    url: http://localhost:9090
    isDefault: true
    jsonData:
      timeInterval: 30s
```

```yaml
# dashboards/deye.yml
apiVersion: 1
providers:
  - name: deye
    orgId: 1
    folder: Inverter
    type: file
    disableDeletion: false
    updateIntervalSeconds: 30
    allowUiUpdates: true
    options:
      path: /var/lib/grafana/dashboards/deye
```

**Step 1: Deploy** — copy `deye-6k.json` into the provider path, restart Grafana.

**Step 2: Verify through the API**

```bash
curl -s -u "$GRAFANA_USER:$GRAFANA_PASSWORD" \
  "http://<grafana>:3000/api/dashboards/uid/deye-6k" | jq -r '.dashboard.title, (.dashboard.panels | length)'
```

Expected: `Deye 6K` and the expected panel count.

**Step 3: Verify data** — open the dashboard, confirm every panel shows data, no `No data` and no `datasource not found`.

**Step 4: Commit** — `feat: add grafana provisioning for the deye dashboard`

---

## Phase 5 — Alerts: Prometheus → Alertmanager → Telegram

**Outcome:** Telegram messages for inverter faults, overheating, and missing data.

### Task 5.1: Recording and alerting rules

**Files:** Create `Monitoring/prometheus/deye-alerts.yml`

```yaml
groups:
  - name: deye
    rules:
      - alert: DeyeScrapeFailing
        expr: deye_scrape_success == 0
        for: 3m
        labels: { severity: critical }
        annotations:
          summary: "Inverter Modbus reads are failing"
          description: "No successful read for 3 minutes on {{ $labels.instance }}"

      - alert: DeyeDataStale
        expr: time() - deye_last_successful_scrape_timestamp_seconds > 120
        for: 2m
        labels: { severity: warning }

      - alert: DeyeOverTemperature
        expr: deye_dc_temperature_celsius > 65 and deye_ac_temperature_celsius > 65
        for: 5m
        labels: { severity: warning }

      - alert: DeyeFaultStatus
        expr: deye_running_status{status="fault"} == 1
        for: 1m
        labels: { severity: critical }
```

Note: `Running Status` is currently an enum, not a gauge — Task 5.2 adds `deye_running_status{status="..."}` as a one-hot metric so rules stay simple.

### Task 5.2: Status as labelled metric

**Files:** `src/deye_monitoring/exporter.py`, `tests/test_exporter.py`

Test: a snapshot with running status `2` renders `deye_running_status{status="normal"} 1` and zeros for the other four labels. Implement by mapping the register value through `enums.RUNNING_STATUS`.

**Commit** — `feat: export running status as one-hot metric`

### Task 5.3: Alertmanager Telegram receiver

**Files:** Create `Monitoring/alertmanager/deye-route.yml` (route + `telegram_configs` snippet), `docs/ALERTS.md` (bot creation via `@BotFather`, chat id via `getUpdates`, how to test with `amtool alert add`).

**Verify:** `amtool alert add DeyeTest severity=warning` → Telegram message arrives.

**Commit** — `docs: add alertmanager telegram routing`

---

### Task 5.4: Built-in Telegram notifier

**Files:**
- Create: `src/deye_monitoring/notify.py`
- Test: `tests/test_notify.py`
- Modify: `src/deye_monitoring/cli.py` (`serve --notify`, disabled by default)

Alertmanager is optional: this notifier makes a bare Pi enough to get messages. Configuration comes from the environment, nothing is stored in git:

| Variable | Purpose |
|---|---|
| `DEYE_TELEGRAM_TOKEN` | bot token from `@BotFather`; when unset, notifications are disabled and the exporter logs it once |
| `DEYE_TELEGRAM_CHAT_ID` | target chat or channel id |
| `DEYE_TEMP_WARNING_C` | DC/AC temperature threshold for the warning (default 65) |
| `DEYE_STALE_SECONDS` | data age that counts as stale (default 120) |
| `DEYE_NOTIFY_COOLDOWN` | minimum seconds between repeats of the same alert (default 1800) |

Rules: `scrape failing`, `data stale`, `over temperature`, `running status alarm/fault`, and a `recovered` message when a previously firing alert clears. State is kept in memory with a cooldown so a flapping line cannot spam the chat.

**Step 1: Failing test** — a fake transport records requests:

```python
def test_disabled_without_token(fake_transport):
    notifier = Notifier(token=None, chat_id=None, transport=fake_transport)
    notifier.raise_alert("stale", "no data")
    assert fake_transport.requests == []


def test_message_contains_alert_and_serial(fake_transport):
    notifier = Notifier(token="t", chat_id="1", transport=fake_transport)
    notifier.raise_alert("over_temperature", "DC 68.1 C")
    body = fake_transport.requests[0]["data"]["text"]
    assert "over_temperature" in body and "2601255637" in body


def test_cooldown_suppresses_repeats(fake_transport, clock): ...
def test_recovery_message_is_sent_once(fake_transport): ...
```

**Step 2: Implement** — `Notifier` builds `https://api.telegram.org/bot<token>/sendMessage` requests with `urllib.request`, `parse_mode=HTML`, escapes user text, never raises into the poll loop, and logs failures.

**Step 3: Wire into `serve`** — evaluate rules after each poll, send transitions only.

**Step 4: Verify without a real token** — `uv run dmon serve --notify` prints `telegram disabled: DEYE_TELEGRAM_TOKEN is not set`; unit tests cover message building.

**Step 5: Commit** — `feat: add optional telegram notifier`

---

## Phase 6 — Guarded writes (opt-in, separate from monitoring)

**Outcome:** a human can change TOU/limits via CLI, with dry-run, allow-list and audit log. The monitoring service never imports this module.

### Task 6.1: Write primitives

**Files:** Create `src/deye_monitoring/write.py`, `tests/test_write.py`

- `build_write_frame(slave, address, value)` (FC6) and `build_write_multiple_frame` (FC16), CRC-checked, tested against known vectors.
- `ALLOWED_REGISTERS: dict[str, RegisterSpec]` — start with the Time-of-Use schedule (`tou_*`: time slots, grid-charge flag, power per slot), then `max_charge_current` and `max_discharge_current`. Anything else raises `RegisterNotAllowed`.
- TOU is the first and most important write target. Writes are **never executed against the live inverter during development**: tests use a fake transport, and the operator verifies on the device separately.
- Echo verification: after writing, read back and compare; mismatch raises `WriteVerificationError`.

### Task 6.2: `dmon-write` CLI

**Files:** Create `src/deye_monitoring/write_cli.py`; entry point `dmon-write` in `pyproject.toml`.

Behaviour: `dmon-write set max_charge_current 50` prints the planned change and **does nothing** without `--confirm`. With `--confirm` it writes, reads back, and appends a JSON line to `/var/log/deye/writes.log` (path configurable). `--list` shows the allow-list and current values.

**Tests:** dry-run makes zero writes; `--confirm` writes and verifies; disallowed register exits 2; audit line is appended.

### Task 6.3: Safety documentation

**Files:** `docs/WRITES.md` — why writes are separate, what each allowed register does, recovery from a bad write (screen on the inverter resets the value), and the rule that automation must not call `dmon-write` until a manual change has been observed for a week.

**Commit** — `feat: add guarded inverter write cli`

---

## Acceptance criteria

| Phase | Done when |
|---|---|
| 1 | `curl 192.168.10.39:9101/metrics` returns 60+ gauges, `deye_scrape_success 1`, `/health` is 200; Prometheus shows the target `UP` |
| 2 | HA shows a `Deye 6K` device with PV/battery/grid/load/temperature entities updating every 10 s |
| 3 | Pi reboots and the exporter comes back without SSH; `journalctl` shows no tracebacks for 24 h |
| 4 | `promtool check config` passes, the target is `UP`, the provisioned dashboard opens with uid `deye-6k`, every panel has data, and `uv run pytest tests/test_dashboard.py` proves no panel references a non-existent metric |
| 5 | Killing the adapter (`systemctl stop`) produces a Telegram alert within 5 minutes and a recovery message after restart |
| 6 | `dmon-write --list` prints the allow-list; a dry-run changes nothing; a confirmed write reads back identical |

## Out of scope

- Automatic control loops (charging from forecast, dynamic export limits).
- Multi-inverter support (single slave `1` for now; the register map is per-model).
- Historical storage beyond Prometheus retention.
- Alerting on individual alarm bits (device alarm/fault bitfields) — start with status, temperature and scrape health.

## Open questions

1. Prometheus retention and scrape interval on the existing server (default plan: 30 s scrape, 15 d retention).
2. Where Grafana and Prometheus run: same host, and what UID/URL the datasource should use. The plan assumes `http://localhost:9090` with uid `deye-prometheus`; adjust if they are remote.
3. Mosquitto host/port and whether anonymous access is allowed from VLAN 210 — unknown, so the MQTT bridge is driven entirely by `--host/--port` or environment variables and stays idle when unconfigured.
4. Telegram bot: existing bot for other alerts, or a dedicated one for the inverter? The built-in notifier only needs `DEYE_TELEGRAM_TOKEN` and `DEYE_TELEGRAM_CHAT_ID`.
5. Which write registers matter first: TOU schedule, grid-charge toggle, or charge/discharge current limits?

