"""Tests for the Time-of-Use decoder."""

from __future__ import annotations

import json
import pathlib

import pytest

from deye_monitoring import tou
from deye_monitoring.exporter import Sample, render
from deye_monitoring.snapshot import as_mapping, decode_all

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "raw_registers.json"


@pytest.fixture(scope="module")
def registers() -> dict[int, int]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {int(address, 16): value for address, value in raw.items()}


@pytest.fixture(scope="module")
def state(registers: dict[int, int]) -> tou.TouState:
    parsed = tou.parse(registers)
    assert parsed is not None
    return parsed


def test_base_register_is_detected_as_250(state: tou.TouState) -> None:
    # Register 249 reads 0 on this firmware; the real block starts at 250.
    assert state.base == 250


def test_mode_register_marks_schedule_enabled(state: tou.TouState) -> None:
    assert state.enabled is True


def test_all_weekdays_enabled(state: tou.TouState) -> None:
    assert set(state.days) == {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    assert all(state.days.values())


def test_six_slots_are_decoded(state: tou.TouState) -> None:
    assert len(state.slots) == 6
    assert [slot.slot for slot in state.slots] == [1, 2, 3, 4, 5, 6]


def test_slot_times_convert_hhmm_to_minutes(state: tou.TouState) -> None:
    assert [slot.start_hhmm for slot in state.slots] == [
        "01:00",
        "05:00",
        "09:00",
        "13:00",
        "17:00",
        "21:00",
    ]
    assert state.slots[0].start_minutes == 60
    assert state.slots[5].start_minutes == 21 * 60


def test_slot_power_and_voltage(state: tou.TouState) -> None:
    assert state.slots[0].power_w == 5000
    assert state.slots[1].power_w == 6000
    assert state.slots[0].voltage_v == pytest.approx(49.0)


def test_slot_capacity(state: tou.TouState) -> None:
    assert all(slot.capacity_pct == 85 for slot in state.slots)


def test_charge_bits_are_split(state: tou.TouState) -> None:
    first = state.slots[0]
    second = state.slots[1]
    third = state.slots[2]
    # registers read 5, 5, 7, 7, 7, 7
    assert (first.grid_charge, first.gen_charge, first.gm_mode) == (True, False, True)
    assert (second.grid_charge, second.gen_charge, second.gm_mode) == (True, False, True)
    assert (third.grid_charge, third.gen_charge, third.gm_mode) == (True, True, True)
    assert all(slot.bu_mode is False and slot.ch_mode is False for slot in state.slots)


def test_as_dict_is_json_ready(state: tou.TouState) -> None:
    payload = state.as_dict()
    assert payload["enabled"] is True
    assert payload["base_register"] == 250
    assert len(payload["slots"]) == 6
    assert payload["slots"][0]["start"] == "01:00"
    assert payload["slots"][0]["grid_charge"] is True


def test_detect_base_prefers_plausible_block(registers: dict[int, int]) -> None:
    assert tou.detect_base(registers) == 250
    assert tou.detect_base(registers, preferred=249) is None
    assert tou.detect_base({}) is None


def test_parse_returns_none_without_registers() -> None:
    assert tou.parse({}) is None


def test_readings_have_metric_names_and_labels(state: tou.TouState) -> None:
    readings = tou.readings(state)
    names = {reading.metric for reading in readings}
    assert names == {
        "deye_tou_enabled",
        "deye_tou_day",
        "deye_tou_slot_start_minutes",
        "deye_tou_slot_power_watts",
        "deye_tou_slot_voltage_volts",
        "deye_tou_slot_capacity_percent",
        "deye_tou_charge",
        "deye_tou_mode",
    }
    day = next(reading for reading in readings if reading.metric == "deye_tou_day")
    assert day.labels == {"day": "mon"}
    assert day.value == 1
    charge = [r for r in readings if r.metric == "deye_tou_charge"]
    assert len(charge) == 12  # six slots, grid and generator
    assert {r.labels["source"] for r in charge} == {"grid", "gen"}


def test_decode_all_includes_tou_values(registers: dict[int, int]) -> None:
    readings, state = decode_all(registers)
    assert state is not None
    values = as_mapping(readings)
    assert values["PV1 Power"] == pytest.approx(654)
    assert values["Time of Use enabled"] == 1


def test_exported_tou_metric_has_slot_label(state: tou.TouState) -> None:
    sample = Sample(
        readings=tuple(tou.readings(state)),
        serial="2601255637",
        duration=0.1,
        last_success=1_700_000_000.0,
    )
    text = render(sample)
    assert 'deye_tou_slot_power_watts{group="tou",serial="2601255637",slot="1"} 5000' in text
    assert 'deye_tou_day{group="tou",serial="2601255637",day="mon"} 1' in text
    assert 'deye_tou_charge{group="tou",serial="2601255637",slot="1",source="gen"} 0' in text
