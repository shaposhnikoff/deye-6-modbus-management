# Runbook: changing one parameter on the inverter

> Русская версия: [RUNBOOK-WRITE.ru.md](RUNBOOK-WRITE.ru.md)

Short procedure for an operator at a shell on the Pi. Goal: change an allow-listed setting, prove
it was applied, and be able to undo it.

## What a "write" is

There is no script running on the inverter. Each value is sent as one Modbus RTU **FC16** frame:
slave `1`, register address, one 16-bit word, CRC. The only human command is:

```bash
dmon-write set <key>=<value> --confirm
```

The CLI checks the key against the allow-list, validates the range, writes the register, compares
the echo, reads the register back and appends a JSON line to `/var/log/deye/writes.log`. Without
`--confirm` it only prints the plan.

## What can be changed

Only Time-of-Use registers 250–279. `N` is the slot, 1–6.

| Key | Meaning | Format | Range |
|---|---|---|---|
| `tou.N.start` | slot start | HHMM, e.g. `2300` | 0000–2359, minutes < 60 |
| `tou.N.power` | slot output power | watts | 0–12000 |
| `tou.N.voltage` | battery voltage target | volts (0.01 V on the wire) | 40–60 |
| `tou.N.soc` | target state of charge | percent | 10–100 |
| `tou.N.grid_charge` | allow grid charging in this slot | `0` / `1` | — |
| `tou.N.gen_charge` | allow generator charging | `0` / `1` | — |
| `tou.N.charge_mask` | whole charge bitfield | 0–31 | **unverified**, needs `--include-unverified` |

Anything else is refused with exit code `2`. Bit keys use read-modify-write, so `gm/bu/ch` and the
other charge bits survive.

## Before you start

- mains is stable, the inverter is not in fault and not updating firmware;
- you know the exact target values and have written them down;
- the whole window will take under two minutes (Prometheus raises `DeyeDataStale` after two);
- `/var/log/deye` exists and belongs to `max` (one-time setup: `sudo install -d -o max -g max /var/log/deye`);
- nobody else is on the bus.

## Procedure

```bash
ssh max@192.168.10.39
cd /home/max/DEYEMonitoring
```

**1. Free the serial port.** The collector owns it exclusively, so every `dmon-write` command fails
while the service runs.

```bash
sudo systemctl stop deye-monitoring
```

**2. Read the current values.**

```bash
.venv/bin/dmon-write list
```

**3. Dry run.** Prints what would change and writes nothing.

```bash
.venv/bin/dmon-write plan tou.1.power=5500
```

Check the `before -> after` line. If it is not what you expect, stop here.

**4. Apply.**

```bash
.venv/bin/dmon-write set tou.1.power=5500 --confirm
```

Every key must print `ok ... verified by readback`. Exit code `0` means all of them passed.

**5. Verify read-only.**

```bash
.venv/bin/dmon-write verify tou.1.power=5500
```

Expect `1/1 match`. This is the command to repeat later to detect drift.

**6. Give the port back.** Do this even if step 4 failed.

```bash
sudo systemctl start deye-monitoring
curl -s localhost:9101/health
```

Expect `"ok": true`.

**7. Check the audit log.**

```bash
tail -n 3 /var/log/deye/writes.log
```

One line per key, with `from_raw`, `to_raw`, `confirmed: true` and the inverter serial.

**8. Check the device.** The inverter's own menu is authoritative. If the screen disagrees with the
readback, stop and investigate before trusting the CLI.

## Example: night-charge window (already applied)

Six slots, grid charge allowed only 23:00–07:00. Two start registers move because there was no
07:00 boundary, and four grid bits are cleared:

```bash
sudo systemctl stop deye-monitoring
.venv/bin/dmon-write plan \
  tou.6.start=2300 tou.2.start=700 \
  tou.1.grid_charge=1 tou.2.grid_charge=0 \
  tou.3.grid_charge=0 tou.4.grid_charge=0 \
  tou.5.grid_charge=0 tou.6.grid_charge=1
# looks right? repeat the same keys with `set ... --confirm`
.venv/bin/dmon-write verify \
  tou.6.start=2300 tou.2.start=700 \
  tou.1.grid_charge=1 tou.2.grid_charge=0 \
  tou.3.grid_charge=0 tou.4.grid_charge=0 \
  tou.5.grid_charge=0 tou.6.grid_charge=1
sudo systemctl start deye-monitoring
```

The full record, including rollback values, is in
[2026-10-21-tou-night-charge.md](plans/2026-10-21-tou-night-charge.md).

## Undo

The audit log stores the previous raw word in `from_raw`. Write the old value back:

```bash
sudo systemctl stop deye-monitoring
.venv/bin/dmon-write set tou.1.power=5000 --confirm   # value taken from from_raw
sudo systemctl start deye-monitoring
```

For a bit target, `from_raw` is the whole word, but `set` takes the bit value (`0`/`1`) — read the
old bit from `dmon-write list` or the inverter screen.

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `serial error ... busy` | collector still running | `sudo systemctl stop deye-monitoring`, retry |
| `no reply for write at 0x....` | inverter off/fault, or a firmware that ignores FC16 | confirm `dmon-write list` still reads; check power and A/B wiring |
| `inverter rejected read ... illegal data address` | wrong register base for this firmware | do not force; compare with the inverter screen |
| `echo mismatch` | line noise or a second master | `pgrep -af "bin/dmon"`, keep one owner, retry once |
| `FAILED ... wrote X, read back Y` | value not accepted | check range, then the screen |
| `warning: could not write audit log` | `/var/log/deye` missing | `sudo install -d -o max -g max /var/log/deye` |
| command finished but service is down | SSH dropped mid-window | reconnect, `sudo systemctl start deye-monitoring` |

Exit codes: `0` success, `1` transport/serial error, `2` refused (unknown key or out of range),
`3` verification failed.

## Rules

- One parameter group per command; time registers and their bits may go together.
- `set` without `--confirm` is always safe — use it as the last check.
- Writes are absolute: re-running the same command is safe and idempotent.
- Never call `dmon-write set --confirm` from cron, a script or Home Assistant. A human runs it.
- If a write is partially applied, re-run the same command and then `verify`.
