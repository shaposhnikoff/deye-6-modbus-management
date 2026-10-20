"""``dmon top`` — a live console dashboard (htop style) for the inverter.

Reads the local JSON cache, so it never competes with the collector for the serial port and can be
opened from any SSH session. ``--once`` prints a single frame as plain text, which is also what the
tests exercise.
"""

from __future__ import annotations

import curses
import time
from dataclasses import dataclass

from deye_monitoring.cache import CacheSnapshot, read_snapshot

OK = "ok"
WARN = "warn"
CRIT = "crit"

BATTERY_LOW = 25.0
BATTERY_CRITICAL = 15.0
TEMP_WARN = 60.0
TEMP_CRIT = 70.0
FREQ_MIN = 49.0
FREQ_MAX = 51.0


@dataclass(frozen=True)
class Row:
    """One line of the dashboard."""

    label: str
    text: str
    status: str = OK
    section: str = ""


def bar(value: float, low: float, high: float, width: int = 18) -> str:
    """Render a simple ASCII gauge, clamped to [low, high]."""
    if high <= low:
        return " " * width
    filled = int(round((value - low) / (high - low) * width))
    filled = max(0, min(width, filled))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def battery_status(soc: float) -> str:
    if soc <= BATTERY_CRITICAL:
        return CRIT
    if soc <= BATTERY_LOW:
        return WARN
    return OK


def temp_status(value: float) -> str:
    if value >= TEMP_CRIT:
        return CRIT
    if value >= TEMP_WARN:
        return WARN
    return OK


def freq_status(value: float) -> str:
    return OK if FREQ_MIN <= value <= FREQ_MAX else WARN


def _fmt(value: float, unit: str, digits: int = 1) -> str:
    if unit == "W" and abs(value) >= 1000:
        return f"{value / 1000:.2f} kW"
    if digits == 0 or float(value).is_integer():
        return f"{value:.0f} {unit}".strip()
    return f"{value:.{digits}f} {unit}".strip()


def format_age(seconds: float) -> str:
    """Human readable age, e.g. ``3s`` or ``2m14s``."""
    total = int(max(seconds, 0))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def build_overview(values: dict[str, float]) -> list[Row]:
    """Turn a value mapping into the overview rows."""
    rows: list[Row] = []

    def add(label: str, key: str, unit: str, status: str = OK, digits: int = 1) -> None:
        if key in values:
            rows.append(Row(label, _fmt(values[key], unit, digits), status, ""))

    rows.append(Row("", "", OK, "PV"))
    if "PV1 Power" in values:
        power = values["PV1 Power"] + values.get("PV2 Power", 0.0)
        rows.append(Row("PV power", _fmt(power, "W", 0), OK))
    add("PV1 voltage", "PV1 Voltage", "V")
    add("PV1 current", "PV1 Current", "A", digits=2)
    add("PV2 voltage", "PV2 Voltage", "V")

    rows.append(Row("", "", OK, "BATTERY"))
    if "Battery" in values:
        soc = values["Battery"]
        rows.append(
            Row(
                "State of charge",
                f"{soc:>5.0f} %  {bar(soc, 0, 100)}",
                battery_status(soc),
            )
        )
    add("Voltage", "Battery Voltage", "V", digits=2)
    add("Current", "Battery Current", "A", digits=2)
    add("Power", "Battery Power", "W", digits=0)
    if "Battery Temperature" in values:
        temp = values["Battery Temperature"]
        rows.append(Row("Temperature", _fmt(temp, "°C"), temp_status(temp)))

    rows.append(Row("", "", OK, "GRID"))
    add("Voltage", "Grid Voltage", "V")
    if "Grid Frequency" in values:
        freq = values["Grid Frequency"]
        rows.append(Row("Frequency", f"{freq:.2f} Hz", freq_status(freq)))
    add("Power", "Grid Power", "W", digits=0)
    add("Bought today", "Daily Energy Bought", "kWh", digits=2)

    rows.append(Row("", "", OK, "LOAD"))
    add("Power", "Load Power", "W", digits=0)
    add("Voltage", "Load L1 Voltage", "V")
    add("Consumed today", "Daily Load Consumption", "kWh", digits=2)

    rows.append(Row("", "", OK, "INVERTER"))
    if "DC Temperature" in values:
        temp = values["DC Temperature"]
        rows.append(Row("DC temperature", _fmt(temp, "°C"), temp_status(temp)))
    if "AC Temperature" in values:
        temp = values["AC Temperature"]
        rows.append(Row("AC temperature", _fmt(temp, "°C"), temp_status(temp)))
    add("Output voltage", "Output L1 Voltage", "V")
    add("PV today", "Daily PV Production", "kWh", digits=2)
    add("PV total", "Total PV Production", "kWh", digits=1)
    return rows


def build_tou(snapshot: CacheSnapshot) -> list[Row]:
    """Rows describing the Time-of-Use schedule."""
    rows: list[Row] = []
    tou = snapshot.tou or {}
    enabled = "ON" if tou.get("enabled") else "off"
    days = tou.get("days") or {}
    active_days = ",".join(name for name, on in days.items() if on) or "-"
    rows.append(Row("TOU enabled", enabled, OK if tou.get("enabled") else WARN))
    rows.append(Row("Days", active_days))
    rows.append(Row("", "", OK, "SLOTS"))
    for slot in tou.get("slots") or []:
        flags = []
        if slot.get("grid_charge"):
            flags.append("grid")
        if slot.get("gen_charge"):
            flags.append("gen")
        rows.append(
            Row(
                f"Slot {slot.get('slot')}  {slot.get('start')}",
                f"{slot.get('power_w')} W  {slot.get('voltage_v')} V  "
                f"{slot.get('soc_pct')} %  {'+'.join(flags) or 'no charge'}",
            )
        )
    return rows


def render_text(snapshot: CacheSnapshot | None, path: str, now: float | None = None) -> str:
    """Plain-text frame used by ``--once`` and tests."""
    if snapshot is None:
        return f"waiting for cache {path} ..."
    age = snapshot.age(now)
    lines = [
        f"Deye SUN-6K  serial {snapshot.serial or '?'}  "
        f"{'online' if snapshot.online else 'OFFLINE'}  "
        f"age {format_age(age)}  interval {snapshot.interval:g}s",
        "",
    ]
    section = ""
    for row in build_overview(snapshot.values):
        if row.section:
            section = row.section
            lines.append(f"[{section}]")
            continue
        mark = {"ok": " ", "warn": "!", "crit": "X"}[row.status]
        lines.append(f" {mark} {row.label:<18} {row.text}")

    lines.append("")
    lines.append("[TIME OF USE]")
    for row in build_tou(snapshot):
        if row.section:
            continue
        mark = {"ok": " ", "warn": "!", "crit": "X"}[row.status]
        lines.append(f" {mark} {row.label:<18} {row.text}")
    return "\n".join(lines)


def _color(status: str) -> int:
    return {OK: curses.COLOR_GREEN, WARN: curses.COLOR_YELLOW, CRIT: curses.COLOR_RED}.get(
        status, curses.COLOR_WHITE
    )


def _draw(screen: curses.window, snapshot: CacheSnapshot | None, path: str, page: str) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    curses.init_pair(1, curses.COLOR_CYAN, curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_GREEN, curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK)
    curses.init_pair(4, curses.COLOR_RED, curses.COLOR_BLACK)
    pair = {"ok": 2, "warn": 3, "crit": 4}

    if snapshot is None:
        screen.addstr(1, 2, f"waiting for cache {path} ...")
    else:
        header = (
            f"Deye SUN-6K  serial {snapshot.serial or '?'}  "
            f"{'online' if snapshot.online else 'OFFLINE'}  "
            f"age {format_age(snapshot.age())}  interval {snapshot.interval:g}s  "
            f"page {page} (1/2)"
        )
        screen.addnstr(
            0, 0, header.ljust(width - 1), width - 1, curses.color_pair(1) | curses.A_BOLD
        )

        rows = build_overview(snapshot.values) if page == "1" else build_tou(snapshot)
        line = 2
        for row in rows:
            if line >= height - 2:
                break
            if row.section:
                if row.section == "SLOTS":
                    continue
                screen.addnstr(line, 1, f"[{row.section}]", width - 2, curses.A_BOLD)
            else:
                text = f" {row.label:<18} {row.text}"
                screen.addnstr(line, 0, text, width - 2, curses.color_pair(pair[row.status]))
            line += 1

    footer = " q quit   p pause   + / - interval   1 / 2 page   r redraw "
    screen.addnstr(height - 1, 0, footer.ljust(width - 1), width - 1, curses.A_REVERSE)
    screen.refresh()


def run(cache_path: str, interval: float) -> int:
    """Interactive loop."""
    page = "1"
    paused = False

    def loop(screen: curses.window) -> None:
        nonlocal page, paused, interval
        curses.curs_set(0)
        screen.nodelay(True)
        screen.timeout(250)
        while True:
            snapshot = read_snapshot(cache_path)
            _draw(screen, snapshot, cache_path, page)
            key = screen.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key == ord("p"):
                paused = not paused
            elif key in (ord("+"), ord("=")):
                interval = max(0.5, interval - 0.5)
            elif key in (ord("-"), ord("_")):
                interval = min(60.0, interval + 0.5)
            elif key == ord("1"):
                page = "1"
            elif key == ord("2"):
                page = "2"
            elif key in (ord("r"), ord("R")):
                continue
            if not paused:
                time.sleep(interval)

    curses.wrapper(loop)
    return 0
