# TOU night-charge window (23:00–07:00)

**Goal:** stop the inverter from being allowed to charge the battery from the grid all day. After
this change the grid-charge bit is set only inside the cheap night window, 23:00–07:00, and cleared
for the rest of the day.

**Status:** applied 2026-10-21 18:17 EEST, readback `8/8 match`. The commands below are the first
confirmed writes on this inverter, so they are split into dry run, apply and read-only
verification.

**Attempt 2026-10-21:** the dry run matched the target exactly, then every write returned
`no echo` and `dmon-write verify` still showed the old schedule. Nothing changed on the inverter.

Root cause found with a raw probe once the mains was back: this firmware **ignores FC6** (write
single register) and answers with zero bytes, while **FC16** (write multiple registers) is
acknowledged with the expected header echo:

```text
FC3  0x00FF        -> 01 03 02 08 34 be 53   (21:00, read OK)
FC6  0x00FF=2100   -> <empty>
FC16 0x00FF=[2100] -> 01 10 00 ff 00 01 31 f9
```

`WriteClient.write_register` now sends a one-register FC16 frame, so the procedure below works as
written. The first attempt also exposed two operational gaps that are fixed: `/var/log/deye` is
created by the operator, and `dmon-write` now prints a clear message when the collector holds the
serial port instead of raising a traceback.

## Applied

A no-op write first proved the fixed path (`tou.6.start=2100`, `ok verified by readback`), then the
full set was applied in one maintenance window:

```text
write  tou.6.start: 21:00 -> 23:00   ok
write  tou.2.start: 05:00 -> 07:00   ok
write  tou.1.grid_charge: on -> on   ok
write  tou.2.grid_charge: on -> off  ok
write  tou.3.grid_charge: on -> off  ok
write  tou.4.grid_charge: on -> off  ok
write  tou.5.grid_charge: on -> off  ok
write  tou.6.grid_charge: on -> on   ok
8/8 match
```

The live `deye_tou_slot_info` afterwards: starts `01:00, 07:00, 09:00, 13:00, 17:00, 23:00`,
`grid=on` only for slots 1 and 6, `gen` unchanged, `gm=on, bu=off, ch=off` in every slot.

Audit `to_raw` words after the run: slot 1 `5`, slot 2 `4`, slots 3-5 `6`, slot 6 `7` — the GM,
generator and CH bits survived the read-modify-write. The first pass of the audit recorded the raw
bit value (`1`/`0`) instead of the whole word; the CLI now logs the written word, and an idempotent
re-run produced the correct lines. Treat the four `15:17:58` lines in `/var/log/deye/writes.log` as
bit values, not words.

The night window still needs to be observed for a week before any automation is allowed to call
`dmon-write`.

## Live state before the change

Read from the collector at `192.168.10.39:9101/metrics` (`deye_tou_slot_info`) on 2026-10-21:

| Slot | Start | Power | Voltage | SoC | Grid charge | Gen charge |
|---|---|---|---|---|---|---|
| 1 | 01:00 | 5000 W | 49.00 V | 95% | on | off |
| 2 | 05:00 | 6000 W | 49.00 V | 95% | on | on |
| 3 | 09:00 | 6000 W | 49.00 V | 95% | on | on |
| 4 | 13:00 | 6000 W | 49.00 V | 95% | on | on |
| 5 | 17:00 | 6000 W | 49.00 V | 95% | on | on |
| 6 | 21:00 | 6000 W | 49.00 V | 95% | on | on |

`gm=on`, `bu=off`, `ch=off` in every slot; Time-of-Use itself is enabled.

## Target state

The six slots are start markers: each slot applies from its start until the next one, and slot 6
wraps into slot 1. To get an exact 23:00–07:00 window there must be a boundary at both ends, and
today there is none at 07:00 — so **two** start registers move, not one:

| Slot | Start before | Start after | Grid charge after |
|---|---|---|---|
| 1 | 01:00 | 01:00 | on |
| 2 | 05:00 | **07:00** | **off** |
| 3 | 09:00 | 09:00 | **off** |
| 4 | 13:00 | 13:00 | **off** |
| 5 | 17:00 | 17:00 | **off** |
| 6 | 21:00 | **23:00** | on |

Resulting windows:

- 23:00–01:00 slot 6, grid charge on
- 01:00–07:00 slot 1, grid charge on
- 07:00–09:00 slot 2, off
- 09:00–13:00 slot 3, off
- 13:00–17:00 slot 4, off
- 17:00–23:00 slot 5, off (slot 5 now runs to 23:00 because slot 6 starts later)

Power, voltage, SoC target, `gen_charge` and the `gm/bu/ch` bits are deliberately left untouched.
Bit writes use read-modify-write, so the GM/BU/CH bits survive.

## Procedure on the Pi

Every `dmon-write` command opens `/dev/modbus`, and the collector holds that port exclusively.
Stop the collector first and keep the window short — `DeyeDataStale` fires after two minutes.

```bash
cd /home/max/DEYEMonitoring
sudo systemctl stop deye-monitoring.service
```

### 1. Read the current values

```bash
uv run dmon-write list
```

Expect `tou.2.start 05:00`, `tou.6.start 21:00`, and grid-charge on for all six slots.

### 2. Dry run

```bash
uv run dmon-write plan \
  tou.6.start=2300 \
  tou.2.start=700 \
  tou.1.grid_charge=1 \
  tou.2.grid_charge=0 \
  tou.3.grid_charge=0 \
  tou.4.grid_charge=0 \
  tou.5.grid_charge=0 \
  tou.6.grid_charge=1
```

Expect eight `plan` lines: `21:00 -> 23:00`, `05:00 -> 07:00`, one `on -> on`, four `on -> off`,
one `on -> on`, then `dry run: nothing was written`. Nothing has touched the inverter yet.

### 3. Apply

Same arguments plus `--confirm`:

```bash
uv run dmon-write set \
  tou.6.start=2300 \
  tou.2.start=700 \
  tou.1.grid_charge=1 \
  tou.2.grid_charge=0 \
  tou.3.grid_charge=0 \
  tou.4.grid_charge=0 \
  tou.5.grid_charge=0 \
  tou.6.grid_charge=1 \
  --confirm
```

Expect one `write` line and one `ok ... verified by readback` per key, exit code `0`, and eight
JSON lines appended to `/var/log/deye/writes.log`.

Writes are applied one register at a time in argument order. A transport error aborts the rest; a
readback mismatch is counted and the loop continues (exit `3`). Re-running the command is safe
because every value is absolute.

### 4. Verify without writing

```bash
uv run dmon-write verify \
  tou.6.start=2300 \
  tou.2.start=700 \
  tou.1.grid_charge=1 \
  tou.2.grid_charge=0 \
  tou.3.grid_charge=0 \
  tou.4.grid_charge=0 \
  tou.5.grid_charge=0 \
  tou.6.grid_charge=1
```

Expect `8/8 match`, exit code `0`.

### 5. Give the port back

```bash
sudo systemctl start deye-monitoring.service
curl -s localhost:9101/health
curl -s localhost:9101/metrics | grep deye_tou_slot_info
```

Expect `"ok": true` and slot starts `01:00, 07:00, 09:00, 13:00, 17:00, 23:00` with `grid=on`
only for slots 1 and 6.

### 6. Cross-check on the device

Open `Time Of Use` on the inverter screen: slot 2 must read 07:00, slot 6 must read 23:00, and the
grid-charge tick must be present only on slots 1 and 6. The screen is authoritative — if it
disagrees with the readback, stop and investigate before trusting the CLI.

## Rollback

The audit log holds the previous raw words (`from_raw`). To restore the old schedule:

```bash
sudo systemctl stop deye-monitoring.service
uv run dmon-write set \
  tou.2.start=500 \
  tou.6.start=2100 \
  tou.2.grid_charge=1 \
  tou.3.grid_charge=1 \
  tou.4.grid_charge=1 \
  tou.5.grid_charge=1 \
  --confirm
sudo systemctl start deye-monitoring.service
```

Slots 1 and 6 already had grid charge on, so they need no restore. As a last resort the inverter's
own `Time Of Use` menu always wins: setting the values there overrides whatever the CLI wrote.

## Acceptance

- [x] `dmon-write verify ...` prints `8/8 match` and exits `0`
- [x] `/var/log/deye/writes.log` has one confirmed line per changed key
- [x] `deye_tou_slot_info` shows the new starts and the grid bits above
- [ ] the inverter screen matches the same values (operator check)
- [x] collector is healthy again (`/health` → `"ok": true`)
