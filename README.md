# DEYE Monitoring

Read-only Modbus RTU monitoring for a Deye hybrid inverter (validated on `SUN-6K`, single phase)
running on a Raspberry Pi over a Waveshare USB↔RS232/485 adapter.

The service only ever issues Modbus function code `03` (read holding registers). There are no
write paths in the code, so the inverter cannot be reconfigured by mistake.

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi (8 GB, Debian 13 / trixie) | `deye-monitoring`, `192.168.10.39` |
| Waveshare `USB TO RS232/485 (B)`, SKU 26547 | FTDI FT232RNL, isolated, auto direction control |
| Deye inverter | Modbus port is the RJ45 socket labelled **`Modbus`** |

Wiring (official Deye pinout, manual page 53):

| Deye Modbus RJ45 | Waveshare terminal |
|---|---|
| Pin 1 — `sunspe-485_B` | `B-` |
| Pin 2 — `sunspe-485_A` | `A+` |
| Pin 3 — `GND_sunspe-485` | `PE` / GND (optional, recommended for runs over ~5 m) |

Pins 4–8 stay unconnected. Use one twisted pair for A/B (pins 1 and 2 are one pair in T568B),
keep the 120 Ω termination jumper **off** for a short two-node line, and never take power from
the RJ45 port. If there is no reply, swap A and B first — RS485 polarity is not standardised
between vendors and swapping is harmless.

Serial settings: `9600 8N1`, slave address `1` (`Advanced Function → Paral. Set3 → Modbus SN`).

## Install on the Pi

```bash
# uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# stable device name: FTDI serial BG03RO6W -> /dev/modbus
sudo tee /etc/udev/rules.d/99-deye-modbus.rules >/dev/null <<'RULE'
SUBSYSTEM=="tty", KERNEL=="ttyUSB[0-9]*", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", ATTRS{serial}=="BG03RO6W", ENV{ID_MM_DEVICE_IGNORE}="1", SYMLINK+="modbus", GROUP="dialout", MODE="0660"
RULE
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=tty
```

The user must be in the `dialout` group to open the port without root.

## Usage

```bash
uv run dmon read          # human readable table, one snapshot
uv run dmon json          # one JSON line (ready for logs or an ingest pipeline)
uv run dmon dump          # raw holding registers, 8 words per line
uv run dmon watch         # poll forever, one JSON line per cycle
uv run dmon watch --interval 5 --human
```

Options common to every command: `--port` (default `/dev/modbus`), `--baud` (9600),
`--slave` (1), `--timeout` (1.0 s). They can also be set with `DEYE_PORT`, `DEYE_BAUD`,
`DEYE_SLAVE`.

Example JSON line:

```json
{"timestamp": "2026-10-20T14:58:03+00:00", "serial": "2601255637",
 "values": {"PV1 Power": 654, "PV1 Voltage": 130.9, "Battery": 89, "Grid Power": 18}}
```

## Run as a service

`/etc/systemd/system/deye-monitoring.service`:

```ini
[Unit]
Description=Deye inverter Modbus monitoring
After=network-online.target

[Service]
User=max
WorkingDirectory=/home/max/DEYE Monitoring
ExecStart=/home/max/.local/bin/uv run dmon watch --interval 10
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now deye-monitoring
journalctl -u deye-monitoring -f
```

## Register map

`src/deye_monitoring/registers.py` is generated — do not edit it by hand:

```bash
uv run python tools/generate_registers.py
```

The source definitions are vendored in `tools/upstream-map/` from
[scrhall/deye-sun6k-waveshare-esphome](https://github.com/scrhall/deye-sun6k-waveshare-esphome)
(Apache-2.0), which itself derives from `Lewa-Reka/esphome-deye-inverter`. Every sensor is
read-only: write entities are excluded upstream and the generator only emits numeric
`holding` registers.

Decoding rules per register: `value_type` (`U_WORD`, `S_WORD`, `U_DWORD`, `U_DWORD_R`),
`scale`, `offset` and optional bit extraction (`shift`, `mask`). Registers whose value is zero
are hidden from output to keep JSON lines short — see `snapshot.decode`.

## Development

```bash
uv sync                 # install pyserial + dev tools
uv run pytest           # unit tests, uses tests/fixtures/raw_registers.json
uv run ruff check .
uv run ruff format .
```

The fixture is a real snapshot captured from the inverter, used to pin decoding behaviour:
serial number, 32-bit word-swapped rated power, temperature offset, bit-field registers.

## Layout

```
src/deye_monitoring/
  modbus.py      # FC3 client, CRC-16, retries, exception frames
  registers.py   # generated register map (63 registers)
  snapshot.py    # block reads, decoding, serial number
  enums.py       # status enums (running status)
  cli.py         # read / json / dump / watch
tools/
  generate_registers.py  # regenerates registers.py from the vendored map
  upstream-map/*.yaml    # vendored ESPHome register definitions
tests/
```
