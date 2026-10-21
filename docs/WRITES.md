# Guarded writes (Time-of-Use)

The monitoring service is read-only. Writing lives in `deye_monitoring.write` and is exposed only
through the separate `dmon-write` command, which a human runs on purpose. `dmon serve` never
imports the write module, and the read-only `ModbusClient` gains no write methods — the write
client is a subclass that exists only in that module.

Step-by-step operator procedure, including the maintenance window and rollback:
[RUNBOOK-WRITE.md](RUNBOOK-WRITE.md).

## Allow-list

Only Time-of-Use registers are writable. Anything else is refused with exit code `2`.

| Key | Register | Format | Range |
|---|---|---|---|
| `tou.N.start` | 250–255 | HHMM (`2100` = 21:00) | 0000–2359, minutes < 60 |
| `tou.N.power` | 256–261 | watts | 0–12000 |
| `tou.N.voltage` | 262–267 | volts (stored as 0.01 V) | 40–60 |
| `tou.N.soc` | 268–273 | percent | 10–100 |
| `tou.N.grid_charge` | 274–279, bit 0 | `0`/`1` | — |
| `tou.N.gen_charge` | 274–279, bit 1 | `0`/`1` | — |
| `tou.N.charge_mask` | 274–279 whole word | 0–31 | **unverified**, needs `--include-unverified` |

`N` is the slot number, 1–6.

Bit targets use **read-modify-write**: switching grid charge off keeps the GM/BU/CH bits intact.
Writing the whole bitfield is opt-in because a raw mask can silently clear those bits — the
meaning of bits 2–4 is documented upstream but not confirmed on this firmware.

## Commands

```bash
dmon-write list                       # allow-list with current values (read-only)
dmon-write plan tou.1.power=5500      # validate, print the change, write nothing
dmon-write set tou.1.power=5500       # dry run as well (no --confirm)
dmon-write set tou.1.power=5500 --confirm
dmon-write verify tou.1.power=5500 tou.1.start=100   # read-only drift check
```

`list`, `plan` and `verify` never change anything, but they still open the serial port, so they
need it free (see below).

`verify` is the check to use after a manual change on the inverter screen, or before trusting an
automation: it compares the live schedule with the expected values and exits `1` on drift.

## Serial port ownership

The collector owns `/dev/modbus` and opens it with `exclusive=True`; every `dmon-write` command
tries to take the same lock. While `deye-monitoring.service` runs, `list`, `plan`, `set` and
`verify` all fail at `connect()` with a busy port instead of fighting for the bus.

Stop the collector, do the work, start it again:

```bash
sudo systemctl stop deye-monitoring.service
# ... dmon-write list / plan / set / verify ...
sudo systemctl start deye-monitoring.service
```

Prometheus marks the target down within a few minutes and `DeyeDataStale` fires after two, so keep
a planned change inside a two-minute window. `deye-mqtt.service` does not own the serial port and
can keep running.

The audit log directory is not created by the service account. Create it once:

```bash
sudo install -d -o max -g max /var/log/deye
```

Without it every write still happens, but the audit line is lost and the CLI only prints a
permission warning.

## Safety behaviour

1. **Validation before framing** — a bad value never reaches the serial port.
2. **Readback** — after FC16 the register is read again and compared; a mismatch raises
   `WriteVerificationError` (exit code `3`).
3. **Echo check** — FC16 echoes the request header (slave, function, address, count) plus CRC;
   anything else is a verification failure.

The SUN-6K firmware on this inverter **silently ignores FC6** (write single register): the reply
is empty while FC3 reads keep working. Every write is therefore sent as a one-register FC16 frame,
which the inverter acknowledges with the eight-byte header echo.
4. **Audit log** — every confirmed or failed attempt appends one JSON line to
   `/var/log/deye/writes.log` (override with `--log` or `DEYE_WRITE_LOG`):

```json
{"timestamp": "2026-10-20T15:12:44+00:00", "serial": "2601255637", "key": "tou.1.power",
 "address": "0x0100", "from_raw": 5000, "to_raw": 5500, "value": 5500.0, "confirmed": true}
```

Exit codes: `0` success, `1` Modbus/transport error, `2` refused (unknown key, out of range),
`3` verification failed.

## Partial application

Changes are applied one register at a time, in the order given on the command line. If the third
of five writes fails, the first two are already applied — the audit log shows exactly which ones
were confirmed. Re-running the same command is safe: values are absolute, not relative.

## Recovery

- Undo by writing the old value from the audit log (`from_raw`).
- If the inverter accepts the write but the screen shows a different schedule, check that the
  register base really is 250 on this firmware (see `deye_modbus` decoding: it auto-detects 250
  vs the documented 249).
- When in doubt, the inverter's own Time-of-Use menu is authoritative; match it against
  `deye_tou_slot_info` in Grafana.

## Not covered yet

- Charge/discharge current limits, `Grid Charge Enable` (233), `Solar Sell` (247) and other
  configuration registers — no allow-list entries, so they cannot be written.
- Automatic control (price-based scheduling). The first manual TOU write was verified on
  2026-10-21; still, nothing should call `dmon-write set --confirm` from a script until the night
  window has been observed for a week.
