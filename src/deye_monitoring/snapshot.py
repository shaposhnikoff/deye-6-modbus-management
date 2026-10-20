"""Decoded register snapshot: read raw words, turn them into named readings."""

from __future__ import annotations

from dataclasses import dataclass

from deye_monitoring.modbus import ModbusClient
from deye_monitoring.registers import REGISTERS, Register

# The inverter exposes its data in four contiguous blocks of 125 holding registers.
BLOCKS: tuple[tuple[int, int], ...] = (
    (0x0000, 0x7D),
    (0x007D, 0x7D),
    (0x00FA, 0x7D),
    (0x0177, 0x7D),
)

SERIAL_START = 0x0003
SERIAL_WORDS = 5


@dataclass(frozen=True)
class Reading:
    """One decoded value from the inverter."""

    name: str
    value: float
    unit: str
    address: int
    group: str


def read_registers(
    client: ModbusClient, blocks: tuple[tuple[int, int], ...] = BLOCKS
) -> dict[int, int]:
    """Read all register blocks and return a flat mapping of address -> raw word."""
    registers: dict[int, int] = {}
    for start, count in blocks:
        words = client.read_holding_registers(start, count)
        for offset, word in enumerate(words):
            registers[start + offset] = word
    return registers


def serial_number(registers: dict[int, int]) -> str | None:
    """Decode the 10-character serial number stored in registers 0x0003-0x0007."""
    chars = []
    for offset in range(SERIAL_WORDS):
        word = registers.get(SERIAL_START + offset)
        if word is None:
            return None
        chars.append(word.to_bytes(2, "big").decode("ascii", errors="replace"))
    serial = "".join(chars).strip()
    return serial or None


def _signed(word: int, bits: int) -> int:
    if word >= 1 << (bits - 1):
        return word - (1 << bits)
    return word


def _raw_value(registers: dict[int, int], register: Register) -> int | None:
    if register.value_type == "S_WORD" or register.value_type == "U_WORD":
        return registers.get(register.address)
    if register.value_type == "U_DWORD":
        high, low = registers.get(register.address), registers.get(register.address + 1)
        if high is None or low is None:
            return None
        return (high << 16) | low
    if register.value_type == "U_DWORD_R":
        low, high = registers.get(register.address), registers.get(register.address + 1)
        if high is None or low is None:
            return None
        return (high << 16) | low
    raise ValueError(f"unsupported value type {register.value_type!r} for {register.name!r}")


def decode_register(registers: dict[int, int], register: Register) -> Reading | None:
    """Decode a single register definition, or None when the data is missing."""
    raw = _raw_value(registers, register)
    if raw is None:
        return None

    if register.shift or register.mask != 0xFFFF:
        value = ((raw >> register.shift) & register.mask) * register.scale + register.offset
    else:
        bits = 32 if "DWORD" in register.value_type else 16
        value = (_signed(raw, bits) + register.offset) * register.scale

    return Reading(
        name=register.name,
        value=round(value, 3),
        unit=register.unit,
        address=register.address,
        group=register.group,
    )


def decode(registers: dict[int, int]) -> list[Reading]:
    """Decode every known register, skipping missing or zero-valued entries."""
    readings: list[Reading] = []
    for register in REGISTERS:
        reading = decode_register(registers, register)
        if reading is not None and reading.value != 0:
            readings.append(reading)
    return readings


def as_mapping(readings: list[Reading]) -> dict[str, float]:
    """Convert readings into a name -> value mapping for JSON output."""
    return {reading.name: reading.value for reading in readings}
