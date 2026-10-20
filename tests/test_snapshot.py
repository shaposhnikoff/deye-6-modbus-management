"""Tests for register decoding against a real captured snapshot."""

from __future__ import annotations

import json
import pathlib

import pytest

from deye_monitoring.enums import describe
from deye_monitoring.registers import REGISTERS, Register
from deye_monitoring.snapshot import as_mapping, decode, decode_register, serial_number

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "raw_registers.json"


@pytest.fixture(scope="module")
def registers() -> dict[int, int]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {int(address, 16): value for address, value in raw.items()}


def _by_name(registers: dict[int, int], name: str) -> float:
    values = as_mapping(decode(registers))
    assert name in values, f"{name} missing from decoded snapshot"
    return values[name]


def test_serial_number_is_decoded(registers: dict[int, int]) -> None:
    assert serial_number(registers) == "2601255637"


def test_rated_power_is_a_32bit_word_swapped_value(registers: dict[int, int]) -> None:
    assert _by_name(registers, "Device Rated Power") == 6000


def test_temperature_applies_negative_offset(registers: dict[int, int]) -> None:
    raw = registers[0x005A]
    assert raw == 1374
    assert _by_name(registers, "DC Temperature") == pytest.approx(37.4)


def test_pv_string_readings(registers: dict[int, int]) -> None:
    assert _by_name(registers, "PV1 Power") == pytest.approx(654)
    assert _by_name(registers, "PV1 Voltage") == pytest.approx(130.9)
    assert _by_name(registers, "PV1 Current") == pytest.approx(5.6)


def test_battery_state(registers: dict[int, int]) -> None:
    assert _by_name(registers, "Battery") == pytest.approx(89)
    assert _by_name(registers, "Battery Voltage") == pytest.approx(53.82)


def test_bit_field_registers(registers: dict[int, int]) -> None:
    # Register 0x0012 packs MPPTs in the high nibble and phases in the low nibble.
    assert registers[0x0012] == 0x0201
    readings = as_mapping(decode(registers))
    assert readings["Device MPPTs"] == 2
    assert readings["Device Phases"] == 1


def test_running_status_enum(registers: dict[int, int]) -> None:
    assert describe(0x003B, 2) == "normal"
    assert describe(0x003B, 99) is None
    assert describe(0xFFFF, 1) is None


def test_missing_registers_are_skipped() -> None:
    register = Register(
        name="PV1 Power",
        address=0x00BA,
        value_type="U_WORD",
        scale=1.0,
        offset=0.0,
        unit="W",
        group="pv",
    )
    assert decode_register({}, register) is None


def test_zero_values_are_hidden(registers: dict[int, int]) -> None:
    values = as_mapping(decode(registers))
    assert "Daily Battery Discharge" in values
    assert all(value != 0 for value in values.values())


def test_all_registers_have_unique_addresses_and_names() -> None:
    addresses = [(register.name, register.address) for register in REGISTERS]
    assert len(addresses) == len(set(addresses))


def test_running_status_is_a_numeric_register() -> None:
    # Upstream ships it as a text sensor; the exporter needs the raw word.
    by_address = {register.address: register for register in REGISTERS}
    assert by_address[0x003B].name == "Running Status"


def test_running_status_is_decoded_from_the_snapshot(registers: dict[int, int]) -> None:
    values = as_mapping(decode(registers))
    assert values["Running Status"] == 2
    assert describe(0x003B, values["Running Status"]) == "normal"
