"""Tests for guarded writes: frames, validation, bit handling, audit log."""

from __future__ import annotations

import json

import pytest

from deye_monitoring.write import (
    TARGETS,
    RegisterNotAllowed,
    WriteValueError,
    WriteVerificationError,
    apply_change,
    audit,
    build_write_frame,
    build_write_multiple_frame,
    crc16,
    plan_change,
    resolve,
)


class FakeWriteClient:
    """In-memory stand-in for WriteClient."""

    def __init__(self, registers: dict[int, int] | None = None) -> None:
        self.registers = dict(registers or {})
        self.writes: list[tuple[int, int]] = []

    def read_holding_registers(self, start: int, count: int) -> list[int]:
        return [self.registers.get(start + index, 0) for index in range(count)]

    def write_register(self, address: int, value: int) -> None:
        self.writes.append((address, value))
        self.registers[address] = value


def test_write_frame_matches_known_vector() -> None:
    frame = build_write_frame(1, 0x00FA, 2100)
    assert frame.hex(" ") == "01 06 00 fa 08 34 af ec"
    assert crc16(frame[:-2]) == int.from_bytes(frame[-2:], "little")


def test_write_frame_high_byte_first() -> None:
    frame = build_write_frame(1, 0x0112, 5)
    assert frame[2:6] == bytes([0x01, 0x12, 0x00, 0x05])


def test_write_multiple_frame_structure() -> None:
    frame = build_write_multiple_frame(1, 0x0100, [5000, 6000])
    assert frame[1] == 0x10
    assert frame[4:6] == bytes([0x00, 0x02])  # register count
    assert frame[6] == 4  # byte count
    assert frame[7:11] == (5000).to_bytes(2, "big") + (6000).to_bytes(2, "big")
    assert crc16(frame[:-2]) == int.from_bytes(frame[-2:], "little")


def test_time_validation() -> None:
    target = resolve("tou.1.start")
    assert target.to_raw(2100) == 2100
    with pytest.raises(WriteValueError):
        target.to_raw(2400)
    with pytest.raises(WriteValueError):
        target.to_raw(1260)


def test_power_and_voltage_scaling() -> None:
    assert resolve("tou.1.power").to_raw(5500) == 5500
    with pytest.raises(WriteValueError):
        resolve("tou.1.power").to_raw(13000)
    assert resolve("tou.1.voltage").to_raw(49.0) == 4900
    assert resolve("tou.1.soc").to_raw(85) == 85


def test_unknown_target_is_refused() -> None:
    with pytest.raises(RegisterNotAllowed):
        resolve("tou.1.nonsense")


def test_raw_bitfield_requires_explicit_opt_in() -> None:
    with pytest.raises(RegisterNotAllowed, match="not verified"):
        resolve("tou.1.charge_mask")
    assert resolve("tou.1.charge_mask", include_unverified=True).address == 274


def test_every_tou_slot_has_the_expected_targets() -> None:
    for slot in range(1, 7):
        for suffix in ("start", "power", "voltage", "soc", "grid_charge", "gen_charge"):
            assert f"tou.{slot}.{suffix}" in TARGETS


def test_plan_change_reports_dry_run() -> None:
    change = plan_change(resolve("tou.1.start"), 2100, current=100)
    assert change.raw == 2100
    assert change.describe() == "tou.1.start: 01:00 -> 21:00"


def test_apply_plain_write_verifies() -> None:
    client = FakeWriteClient({250: 100})
    change = plan_change(resolve("tou.1.start"), 2100, current=100)
    apply_change(client, change)  # type: ignore[arg-type]
    assert client.writes == [(250, 2100)]


def test_apply_write_detects_mismatch() -> None:
    class StubbornClient(FakeWriteClient):
        def write_register(self, address: int, value: int) -> None:
            self.writes.append((address, value))  # pretends to write, keeps old value

    client = StubbornClient({250: 100})
    with pytest.raises(WriteVerificationError):
        apply_change(client, plan_change(resolve("tou.1.start"), 2100, 100))  # type: ignore[arg-type]


def test_bit_write_preserves_other_bits() -> None:
    # current 4 = 0b100 (GM mode only), switching grid charge on must keep bit2.
    client = FakeWriteClient({274: 4})
    apply_change(client, plan_change(resolve("tou.1.grid_charge"), 1, 4))  # type: ignore[arg-type]
    assert client.writes == [(274, 5)]


def test_bit_clear_preserves_other_bits() -> None:
    # current 7 = grid+gen+GM, clearing gen gives 5.
    client = FakeWriteClient({275: 7})
    apply_change(client, plan_change(resolve("tou.2.gen_charge"), 0, 7))  # type: ignore[arg-type]
    assert client.writes == [(275, 5)]


def test_bit_value_must_be_zero_or_one() -> None:
    with pytest.raises(WriteValueError):
        resolve("tou.1.grid_charge").to_raw(2)


def test_audit_appends_json_line(tmp_path) -> None:
    log = tmp_path / "writes.log"
    change = plan_change(resolve("tou.1.power"), 5500, current=5000)
    audit(str(log), change, serial_number="2601255637", confirmed=True)
    audit(str(log), change, serial_number="2601255637", confirmed=False)
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["key"] == "tou.1.power"
    assert lines[0]["address"] == "0x0100"
    assert lines[0]["confirmed"] is True
    assert lines[1]["confirmed"] is False
    assert lines[0]["serial"] == "2601255637"


def test_audit_creates_missing_directory(tmp_path) -> None:
    log = tmp_path / "nested" / "dir" / "writes.log"
    change = plan_change(resolve("tou.1.soc"), 90, current=85)
    audit(str(log), change, serial_number="x", confirmed=True)
    assert log.exists()


def test_plan_describes_bit_targets_correctly() -> None:
    # gen_charge is bit 1: describing the encoded value must not shift it again.
    change = plan_change(resolve("tou.3.gen_charge"), 1, current=5)
    assert change.describe() == "tou.3.gen_charge: off -> on"
    change = plan_change(resolve("tou.1.grid_charge"), 0, current=5)
    assert change.describe() == "tou.1.grid_charge: on -> off"
