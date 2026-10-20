# Changelog

## 2026-10-20

First working deployment on a Raspberry Pi in front of a Deye SUN-6K-SG05LP1.

- read-only Modbus RTU client (FC3 only) with CRC checks and retries
- generated register map from the upstream ESPHome package
- `dmon read|json|dump|watch|serve` CLI, Prometheus exporter on `:9101`
- Telegram notifier with cooldown and recovery messages
- Time-of-Use decoding: six slots, charge bitfield, HHMM, address-shift detection
- guarded writes: `dmon-write list|plan|set|verify`, readback, audit log, allow-list
- local JSON cache on tmpfs; collector and MQTT publisher run as separate units
- Home Assistant discovery over MQTT (137 entities verified live)
- `dmon top` console dashboard with screenshots
- Prometheus config, alert rules, Grafana dashboard and Alertmanager route in `Monitoring/`
- operations runbook, CI (ruff, pytest, promtool), progress log in `tasks/todo.md`

Known limits: single inverter, single phase; per-battery telemetry needs a second RS485 adapter and
the BMS protocol.
