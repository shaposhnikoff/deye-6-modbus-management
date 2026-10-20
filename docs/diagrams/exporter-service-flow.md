# Exporter service flow

Runtime shape of `dmon serve`: one thread owns the RS485 line, the HTTP layer only reads the
cached snapshot. This is what prevents a Prometheus scrape from colliding with a Modbus poll on
a single-master bus.

```mermaid
flowchart TD
    subgraph Process["dmon serve process"]
        A[Collector thread] -->|every interval 10s| B[modbus_reader]
        B --> C[ModbusClient FC3]
        C -->|4 blocks x 125 regs| D[(Serial /dev/modbus)]
        C --> E{reply valid}
        E -- no --> F[Sample with error, keep previous values]
        E -- yes --> G[decode readings + serial]
        F --> H[Cached Sample under lock]
        G --> H
        H --> I[HTTP handler /metrics]
        H --> J[HTTP handler /health]
    end

    K[Prometheus scrape 30s] --> I
    L[Human or uptime probe] --> J
    I --> M[Text exposition with deye_* gauges]
    J --> N[JSON ok true, age_seconds, serial]
    M --> O[Grafana dashboard]
    M --> P[Alert rules]
```

Shutdown path, so no orphaned process keeps the serial port locked:

```mermaid
stateDiagram-v2
    [*] --> Starting
    Starting --> Polling: collector.start
    Polling --> Polling: poll every interval
    Polling --> Stopping: SIGTERM or SIGINT
    Stopping --> Closed: server.shutdown then collector.stop
    Closed --> [*]
```
