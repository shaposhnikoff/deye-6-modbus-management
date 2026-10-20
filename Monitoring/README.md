# Monitoring configuration

Everything needed to get the exporter into Prometheus, Grafana and Telegram. Copy the parts you
need; nothing here is deployed automatically and nothing assumes a specific host layout.

```
Monitoring/
├── prometheus/
│   ├── prometheus.yml     # complete standalone config
│   ├── deye-scrape.yml    # snippet to paste into an existing config
│   └── deye-alerts.yml    # alerting + recording rules
├── grafana/
│   ├── dashboards/deye-6k.json                 # uid: deye-6k
│   └── provisioning/
│       ├── datasources/prometheus.yml          # uid: deye-prometheus
│       └── dashboards/deye.yml                 # file provider
└── alertmanager/
    └── alertmanager.yml   # Telegram receiver, needs envsubst
```

## Prometheus

Standalone:

```bash
cp prometheus/prometheus.yml prometheus/deye-alerts.yml /etc/prometheus/
promtool check config /etc/prometheus/prometheus.yml
promtool check rules /etc/prometheus/deye-alerts.yml
systemctl reload prometheus          # or: curl -X POST localhost:9090/-/reload
```

Existing server — append `deye-scrape.yml` to `scrape_configs` and add `deye-alerts.yml` to
`rule_files`. The exporter listens on `192.168.10.39:9101`.

Verify:

```bash
curl -s localhost:9090/api/v1/targets \
  | jq -r '.data.activeTargets[] | select(.labels.job=="deye") | "\(.health) \(.lastError)"'
```

Expected: `up`.

## Grafana

File provisioning (recommended — dashboard stays in git):

```bash
cp grafana/provisioning/datasources/prometheus.yml /etc/grafana/provisioning/datasources/
cp grafana/provisioning/dashboards/deye.yml         /etc/grafana/provisioning/dashboards/
mkdir -p /var/lib/grafana/dashboards/deye
cp grafana/dashboards/deye-6k.json                  /var/lib/grafana/dashboards/deye/
systemctl restart grafana-server
```

Adjust the datasource `url` first if Prometheus is not on `localhost:9090`. The dashboard uses a
`${DS_PROMETHEUS}` template variable, so the datasource can also be picked in the UI.

Manual import: Dashboards → New → Import → upload `deye-6k.json`.

Verify:

```bash
curl -s -u "$GRAFANA_USER:$GRAFANA_PASSWORD" \
  "http://<grafana>:3000/api/dashboards/uid/deye-6k" \
  | jq -r '.dashboard.title, (.dashboard.panels | length)'
```

Expected: `Deye 6K` and `15`.

## Alertmanager + Telegram

```bash
export TELEGRAM_BOT_TOKEN=123456:AA...   # from @BotFather
export TELEGRAM_CHAT_ID=123456789        # from getUpdates
envsubst < alertmanager/alertmanager.yml > /etc/alertmanager/alertmanager.yml
amtool check-config /etc/alertmanager/alertmanager.yml
systemctl restart alertmanager
amtool alert add DeyeTest severity=warning
```

Alert rules shipped: scrape failing, stale data, over-temperature, fault, alarm, exporter down.
`send_resolved: true` means recovery messages arrive automatically.

## Regenerating the dashboard

```bash
uv run python tools/build_dashboard.py
uv run pytest tests/test_dashboard.py
```

The test fails if a panel references a metric the exporter does not produce, so a renamed
register cannot silently break the dashboard.
