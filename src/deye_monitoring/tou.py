"""Decode the Time-of-Use schedule from a raw register snapshot.

Register layout follows the Deye hybrid protocol (see
https://github.com/RichardAntalik/deye-modbus-reference, section "Hybrid Inverter Registers",
addresses 248-279):

=====================  ===========================================
248                    Time of Use Selling Enabled
                       bit0 = enabled, bits 1-7 = Monday..Sunday, bit8 = Mode 3
249/250-254/255        Slot 1-6 start times (HHMM)
255/256-260/261        Slot 1-6 output power (W)
261/262-266/267        Slot 1-6 battery voltage (0.01 V)
267/268-272/273        Slot 1-6 target capacity (%)
273/274-278/279        Slot 1-6 charge enable bitfield
                       bit0 grid charge, bit1 generator charge,
                       bit2 GM mode, bit3 BU mode, bit4 CH mode
=====================  ===========================================

Firmware on this inverter starts the slot block at 250 instead of the documented 249 (register
249 reads 0). The base is detected from the plausibility of the values, and can be pinned
explicitly when the detection is ever ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass

from deye_monitoring.snapshot import Reading

MODE_ADDRESS = 248
SLOT_COUNT = 6

POWER_OFFSET = 6
VOLTAGE_OFFSET = 12
CAPACITY_OFFSET = 18
CHARGE_OFFSET = 24

CANDIDATE_BASES = (250, 249)
MIN_VOLTAGE_RAW = 3000  # 30.00 V, below this the register cannot be a battery voltage
MAX_VOLTAGE_RAW = 6500  # 65.00 V
MAX_POWER_W = 12000
MAX_CAPACITY = 100

DAYS: tuple[tuple[str, int], ...] = (
    ("mon", 1),
    ("tue", 2),
    ("wed", 3),
    ("thu", 4),
    ("fri", 5),
    ("sat", 6),
    ("sun", 7),
)

CHARGE_BITS: tuple[tuple[str, int], ...] = (
    ("grid", 0),
    ("gen", 1),
    ("gm", 2),
    ("bu", 3),
    ("ch", 4),
)


@dataclass(frozen=True)
class TouSlot:
    """One of the six Time-of-Use slots."""

    slot: int
    start_minutes: int
    power_w: int
    voltage_v: float
    capacity_pct: int
    grid_charge: bool
    gen_charge: bool
    gm_mode: bool
    bu_mode: bool
    ch_mode: bool

    @property
    def start_hhmm(self) -> str:
        """Start time formatted as HH:MM."""
        return f"{self.start_minutes // 60:02d}:{self.start_minutes % 60:02d}"


@dataclass(frozen=True)
class TouState:
    """Whole Time-of-Use configuration."""

    enabled: bool
    days: dict[str, bool]
    slots: tuple[TouSlot, ...]
    base: int

    def as_dict(self) -> dict[str, object]:
        """Nested representation for JSON output."""
        return {
            "enabled": self.enabled,
            "days": self.days,
            "base_register": self.base,
            "slots": [
                {
                    "slot": slot.slot,
                    "start": slot.start_hhmm,
                    "start_minutes": slot.start_minutes,
                    "power_w": slot.power_w,
                    "voltage_v": slot.voltage_v,
                    "capacity_pct": slot.capacity_pct,
                    "grid_charge": slot.grid_charge,
                    "gen_charge": slot.gen_charge,
                    "gm_mode": slot.gm_mode,
                    "bu_mode": slot.bu_mode,
                    "ch_mode": slot.ch_mode,
                }
                for slot in self.slots
            ],
        }


def _hhmm_to_minutes(value: int) -> int:
    return (value // 100) * 60 + (value % 100)


def _plausible(registers: dict[int, int], base: int) -> bool:
    """Check that a candidate base register really holds a slot block."""
    times = [registers.get(base + index) for index in range(SLOT_COUNT)]
    powers = [registers.get(base + POWER_OFFSET + index) for index in range(SLOT_COUNT)]
    voltages = [registers.get(base + VOLTAGE_OFFSET + index) for index in range(SLOT_COUNT)]
    capacities = [registers.get(base + CAPACITY_OFFSET + index) for index in range(SLOT_COUNT)]
    if any(value is None for value in times + powers + voltages + capacities):
        return False
    return (
        all(0 <= value <= 2359 and value % 100 < 60 for value in times)
        and all(0 <= value <= MAX_POWER_W for value in powers)
        and all(MIN_VOLTAGE_RAW <= value <= MAX_VOLTAGE_RAW for value in voltages)
        and all(0 <= value <= MAX_CAPACITY for value in capacities)
    )


def detect_base(registers: dict[int, int], preferred: int | None = None) -> int | None:
    """Return the register address of slot 1, or None when the block is absent."""
    if preferred is not None:
        return preferred if _plausible(registers, preferred) else None
    for base in CANDIDATE_BASES:
        if _plausible(registers, base):
            return base
    return None


def parse(registers: dict[int, int], base: int | None = None) -> TouState | None:
    """Decode the Time-of-Use block, or None when it is not present."""
    resolved = detect_base(registers, preferred=base)
    if resolved is None:
        return None

    mode = registers.get(MODE_ADDRESS, 0)
    slots: list[TouSlot] = []
    for index in range(SLOT_COUNT):
        charge = registers[resolved + CHARGE_OFFSET + index]
        slots.append(
            TouSlot(
                slot=index + 1,
                start_minutes=_hhmm_to_minutes(registers[resolved + index]),
                power_w=registers[resolved + POWER_OFFSET + index],
                voltage_v=registers[resolved + VOLTAGE_OFFSET + index] / 100,
                capacity_pct=registers[resolved + CAPACITY_OFFSET + index],
                grid_charge=bool(charge & (1 << 0)),
                gen_charge=bool(charge & (1 << 1)),
                gm_mode=bool(charge & (1 << 2)),
                bu_mode=bool(charge & (1 << 3)),
                ch_mode=bool(charge & (1 << 4)),
            )
        )

    return TouState(
        enabled=bool(mode & (1 << 0)),
        days={name: bool(mode & (1 << bit)) for name, bit in DAYS},
        slots=tuple(slots),
        base=resolved,
    )


def readings(state: TouState) -> list[Reading]:
    """Turn a decoded schedule into labelled readings for the exporter."""

    def reading(
        name: str, metric: str, value: float, unit: str, address: int, **labels: str
    ) -> Reading:
        return Reading(
            name=name,
            value=value,
            unit=unit,
            address=address,
            group="tou",
            labels=labels,
            metric=metric,
        )

    result = [
        reading(
            "Time of Use enabled",
            "deye_tou_enabled",
            1 if state.enabled else 0,
            "",
            MODE_ADDRESS,
        )
    ]
    for name, _bit in DAYS:
        result.append(
            reading(
                f"Time of Use day {name}",
                "deye_tou_day",
                1 if state.days[name] else 0,
                "",
                MODE_ADDRESS,
                day=name,
            )
        )

    for slot in state.slots:
        slot_label = str(slot.slot)
        address = state.base + slot.slot - 1
        result.extend(
            [
                reading(
                    f"Time of Use slot {slot.slot} start",
                    "deye_tou_slot_start_minutes",
                    slot.start_minutes,
                    "min",
                    address,
                    slot=slot_label,
                ),
                reading(
                    f"Time of Use slot {slot.slot} power",
                    "deye_tou_slot_power_watts",
                    slot.power_w,
                    "W",
                    address + POWER_OFFSET,
                    slot=slot_label,
                ),
                reading(
                    f"Time of Use slot {slot.slot} voltage",
                    "deye_tou_slot_voltage_volts",
                    slot.voltage_v,
                    "V",
                    address + VOLTAGE_OFFSET,
                    slot=slot_label,
                ),
                reading(
                    f"Time of Use slot {slot.slot} capacity",
                    "deye_tou_slot_capacity_percent",
                    slot.capacity_pct,
                    "%",
                    address + CAPACITY_OFFSET,
                    slot=slot_label,
                ),
                reading(
                    f"Time of Use slot {slot.slot} grid charge",
                    "deye_tou_charge",
                    1 if slot.grid_charge else 0,
                    "",
                    address + CHARGE_OFFSET,
                    slot=slot_label,
                    source="grid",
                ),
                reading(
                    f"Time of Use slot {slot.slot} generator charge",
                    "deye_tou_charge",
                    1 if slot.gen_charge else 0,
                    "",
                    address + CHARGE_OFFSET,
                    slot=slot_label,
                    source="gen",
                ),
            ]
        )
        for mode_name, enabled in (
            ("gm", slot.gm_mode),
            ("bu", slot.bu_mode),
            ("ch", slot.ch_mode),
        ):
            result.append(
                reading(
                    f"Time of Use slot {slot.slot} {mode_name.upper()} mode",
                    "deye_tou_mode",
                    1 if enabled else 0,
                    "",
                    address + CHARGE_OFFSET,
                    slot=slot_label,
                    mode=mode_name,
                )
            )
    return result
