# Operations runbook

Everything an operator needs after the first deployment: how to check, upgrade, roll back and
debug. Written for the actual layout on the Pi (`/home/max/DEYEMonitoring`) and the monitoring
host (`/home/max/infrastructure_as_docker/monitoring`).

## Services

| Unit | Host | Role |
|---|---|---|
| `deye-monitoring.service` | Pi 192.168.10.39 | owns `/dev/modbus`, writes the cache, serves `:9101` |
| `deye-mqtt.service` | Pi | reads the cache, publishes to Mosquitto |
| Prometheus / Grafana / Alertmanager | 10.68, Docker | scrape, dashboards, alerts |

```bash
systemctl status deye-monitoring deye-mqtt
journalctl -u deye-monitoring -f
curl -s localhost:9101/health
cat /run/deye/state.json | head -c 400
```

## Health signals

| Signal | Meaning | Action |
|---|---|---|
| `/health` → 503 | last Modbus poll failed | check RS485 wiring, then `dmon read` |
| `deye_scrape_success 0` | same, from Prometheus | see above |
| `deye_data_age_seconds` > 30 | collector stuck or dead | `systemctl status`, journal |
| `deye-mqtt` log `publishing offline` | cache stale/missing | fix the collector first |
| HA entities `unavailable` | availability topic offline | same |

## Upgrade

```bash
cd /home/max/DEYEMonitoring
git pull                     # or scp the tarball
uv sync --extra mqtt
sudo systemctl restart deye-monitoring deye-mqtt
curl -s localhost:9101/health
```

If a unit refused to start several times: `sudo systemctl reset-failed deye-monitoring`.

## Rollback

1. Code: `git checkout <previous-commit> && uv sync --extra mqtt && systemctl restart ...`
2. Configs on the monitoring host: every edited file has a sibling `*.bak-<timestamp>`;
   copy it back and reload Prometheus (`curl -X POST 192.168.10.68:9090/-/reload`).
3. Full config snapshot: `~/backups/deye-configs-<timestamp>.tar.gz` on the monitoring host.

## Serial port troubleshooting

Symptoms: `Permission denied`, `Resource temporarily unavailable`, `no reply`.

```bash
ls -l /dev/modbus                      # symlink must point at ttyUSB0
groups | grep dialout                  # user must be in dialout
pgrep -af "bin/dmon"                   # only ONE owner process may run
udevadm info -q property -n /dev/ttyUSB0 | grep ID_SERIAL
```

Two processes on the bus cause CRC errors, not just contention: the Modbus bus allows a single
master. If a stray process survived (for example an interrupted `timeout dmon serve`), kill it
before restarting the unit.

No reply from the inverter, in order of likelihood:

1. A/B swapped — swap the two signal wires (harmless);
2. inverter powered off or in a fault state;
3. wrong slave address (`Advanced Function → Paral. Set3 → Modbus SN`, default 1);
4. wrong baud rate (9600 8N1);
5. 120 Ω termination jumper left on for a short line.

## Cache and topic contract

| Path / topic | Producer | Consumer |
|---|---|---|
| `/run/deye/state.json` | collector, atomic replace | `dmon publish`, `dmon top`, `dmon-write` |
| `deye/state` (retained) | publisher | Home Assistant |
| `deye/availability` | publisher | Home Assistant |
| `homeassistant/sensor/deye_6k/*/config` | publisher | Home Assistant discovery |

`/run` is tmpfs: the cache disappears on reboot and is recreated on the first poll. That is
intended — it is a handoff, not storage.

## Home Assistant maintenance

After renaming or dropping sensors:

```bash
uv run python tools/cleanup_discovery.py --dry-run   # list stale retained configs
uv run python tools/cleanup_discovery.py             # delete them
```

Home Assistant removes the entities itself once the config topic is emptied. Entities that stay
`unavailable` can be deleted in the UI (Settings → Devices & Services → Entities).

## Backups

| What | Where |
|---|---|
| Pi project | git remote `shaposhnikoff/deye-6-modbus-management` |
| Pi secrets (`/etc/deye-monitoring.env`) | not in git; copy manually, `chmod 600` |
| Monitoring configs | `~/backups/deye-configs-*.tar.gz` + per-file `*.bak-*` |

## Known limits

- One inverter, slave 1, single phase, 2 MPPT. The register map still carries three-phase names
  that read zero; they are harmless.
- Per-battery telemetry is not available through the inverter — the BMS aggregates the bank.
- Writes are limited to the Time-of-Use block by design.
