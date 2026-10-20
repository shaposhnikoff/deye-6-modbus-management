# Service topology

Two services on the Pi share one RS485 line through a local JSON cache. Only the collector opens
`/dev/modbus`; everything else reads either the cache file or the HTTP endpoint.

```mermaid
flowchart LR
    subgraph Inverter["Inverter side"]
        INV[Deye SUN-6K]
        ADAPT[Waveshare RS485 adapter]
    end

    subgraph Pi["Raspberry Pi 192.168.10.39"]
        COL["deye-monitoring.service<br/>dmon serve"]
        CACHE[(/run/deye/state.json<br/>tmpfs, atomic replace)]
        HTTP["/metrics + /health<br/>:9101"]
        MQ["deye-mqtt.service<br/>dmon publish"]
        TEL["telegram notifier<br/>inside collector"]
    end

    subgraph Net["Home network"]
        PROM[Prometheus 10.68:9090]
        GRAF[Grafana 10.68:3000]
        BROKER[Mosquitto 10.68:1883]
        HA[Home Assistant]
        TG[Telegram API]
    end

    INV <-->|RS485 A/B| ADAPT
    ADAPT -->|USB tty| COL
    COL -->|write every 10s| CACHE
    COL --> HTTP
    COL --> TEL
    CACHE -->|read every 10s| MQ
    HTTP --> PROM
    PROM --> GRAF
    MQ --> BROKER
    BROKER --> HA
    TEL --> TG
```

Why the cache exists: the Modbus bus allows one master. Two processes reading the inverter would
collide, so the collector owns the port and publishes a snapshot instead of sharing the device.

```mermaid
stateDiagram-v2
    [*] --> Collecting
    Collecting --> CacheWrite: poll succeeded
    CacheWrite --> Collecting: continue
    Collecting --> CacheWriteStale: poll failed
    CacheWriteStale --> Collecting: keep previous values, online=false
    Collecting --> Stopping: SIGTERM
    Stopping --> [*]
```

Consumer behaviour when the cache is missing or old (`age > 3 x interval`):

```mermaid
flowchart TD
    A[dmon publish tick] --> B{cache file readable}
    B -- no --> C[publish availability offline]
    B -- yes --> D{age <= 3 x interval}
    D -- no --> C
    D -- yes --> E[publish state JSON]
    E --> F{discovery already published}
    F -- no --> G[publish retained discovery configs]
    F -- yes --> H[done]
    G --> H
```
