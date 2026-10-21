# Progress log

Living checklist for the DEYE monitoring work. Keep it short: what is done, what is next, what is
blocked. Chat progress reports are written in Russian; this file stays in English like the code.

## Done

- [x] Read-only CLI over Modbus RTU (FC3 only, CRC, retries)
- [x] Generated register map from the upstream ESPHome package
- [x] Prometheus exporter (`dmon serve`, /metrics, /health) as a systemd unit
- [x] Telegram notifier with cooldown and recovery messages
- [x] Prometheus config, alert rules, Grafana dashboard, Alertmanager route (Monitoring/)
- [x] Time-of-Use decoding (slots, charge bitfield, HHMM, address-shift detection)
- [x] Guarded writes (`dmon-write list|plan|set`), readback, audit log
- [x] Local JSON cache + split services (collector / MQTT publisher)
- [x] Home Assistant discovery verified in the live instance (137 entities)
- [x] `dmon top` console dashboard with screenshots in docs/assets
- [x] Zero-valued registers kept in the cache so HA entities stay stable
- [x] Operations runbook (docs/OPS.md) and a guarded-write flow diagram
- [x] CI workflow (ruff, pytest, promtool) and CHANGELOG
- [x] Lovelace dashboard for the inverter (deploy/homeassistant/lovelace-deye.yaml)
- [x] Home Assistant Energy sources: Deye solar+battery, Shelly Pro 3EM grid

## Next

- [x] `dmon-write verify` — compare the live schedule with an expected one (read-only)
- [x] First confirmed TOU write: night charge window 23:00–07:00 (FC16, readback `8/8 match`)
- [ ] Operator checks the new schedule on the inverter screen
- [ ] Observe the night-charge window for a week before any automation calls `dmon-write`

## Blocked

- [ ] Per-battery telemetry: needs a second USB-RS485 adapter on the Pi and the BMS protocol for
      three Deye SE-G5.1 packs (manual describes a host-computer monitoring port)

## Notes

- Backups of every changed file on the home server live next to the original as `*.bak-<timestamp>`;
  full config snapshots go to `~/backups/`.
- Serial port has a single owner: the collector. Everything else reads `/run/deye/state.json`.
