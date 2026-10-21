"""Guarded write support: Modbus FC16 frames, an allow-list, readback checks and an audit log.

Deliberately separate from the monitoring path: ``dmon serve`` never imports this module, and the
read-only ``ModbusClient`` gains no write methods. A human runs ``dmon-write`` explicitly.

Safety rules
------------
* every write is validated against the target's range before a frame is built;
* every write is read back and compared;
* bit targets (grid/gen charge) use read-modify-write, so unrelated bits are preserved;
* targets whose meaning is not confirmed on this model are ``enabled=False`` and hidden unless
  the operator passes ``--include-unverified``;
* a confirmed write appends a JSON line to an audit log.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import serial

from deye_monitoring.modbus import ModbusClient, build_read_frame, crc16


class RegisterNotAllowed(Exception):
    """Raised when a write targets a register that is not on the allow-list."""


class WriteValueError(ValueError):
    """Raised when a value is out of range or badly encoded for its target."""


class WriteVerificationError(RuntimeError):
    """Raised when a readback does not match the value that was written."""


class TargetKind(StrEnum):
    """How a value is encoded in the register."""

    TIME = "time"
    POWER = "power"
    VOLTAGE = "voltage"
    SOC = "soc"
    ENABLE = "enable"
    MASK = "mask"


@dataclass(frozen=True)
class WriteTarget:
    """One writable register with its encoding, safety limits and bit position."""

    key: str
    label: str
    address: int
    kind: TargetKind
    enabled: bool = True
    minimum: float = 0.0
    maximum: float = 65535.0
    scale: float = 1.0
    unit: str = ""
    bit: int | None = None

    def to_raw(self, value: float) -> int:
        """Encode a human value into the raw register word, validating the range."""
        if self.kind == TargetKind.TIME:
            raw = int(round(value))
            if not 0 <= raw <= 2359:
                raise WriteValueError(f"{self.key}: time must be 0000-2359, got {value}")
            if raw % 100 >= 60:
                raise WriteValueError(f"{self.key}: minutes must be 00-59, got {value}")
            return raw
        if not self.minimum <= value <= self.maximum:
            raise WriteValueError(
                f"{self.key}: value must be {self.minimum:g}-{self.maximum:g}, got {value}"
            )
        raw = int(round(value * self.scale))
        if self.kind in (TargetKind.ENABLE, TargetKind.MASK) and raw < 0:
            raise WriteValueError(f"{self.key}: value must not be negative")
        if self.kind == TargetKind.ENABLE and self.bit is None and raw not in (0, 1):
            raise WriteValueError(f"{self.key}: enable must be 0 or 1, got {value}")
        if self.bit is not None and raw not in (0, 1):
            raise WriteValueError(f"{self.key}: bit value must be 0 or 1, got {value}")
        return raw

    def from_raw(self, raw: int) -> float:
        """Decode a raw register word into a human value."""
        if self.bit is not None:
            return float((raw >> self.bit) & 1)
        return raw / self.scale if self.scale != 1 else float(raw)

    def describe_value(self, value: float) -> str:
        """Format a human value (not a raw word), e.g. ``1 -> on`` for a bit target."""
        if self.kind == TargetKind.TIME:
            hours, minutes = divmod(int(value), 100)
            return f"{hours:02d}:{minutes:02d}"
        if self.bit is not None or self.kind == TargetKind.ENABLE:
            return "on" if value else "off"
        if self.unit:
            return f"{value:g} {self.unit}"
        return f"{value:g}"

    def describe(self, raw: int) -> str:
        """Format a raw value for humans, e.g. ``2100 -> 21:00``."""
        value = self.from_raw(raw)
        if self.kind == TargetKind.TIME:
            hours, minutes = divmod(int(value), 100)
            return f"{hours:02d}:{minutes:02d}"
        if self.kind == TargetKind.ENABLE or (self.bit is not None):
            return "on" if value else "off"
        if self.unit:
            return f"{value:g} {self.unit}"
        return f"{value:g}"


def _tou_targets() -> dict[str, WriteTarget]:
    """Build the Time-of-Use allow-list (registers 250-279)."""
    targets: dict[str, WriteTarget] = {}
    for slot in range(1, 7):
        base = slot - 1
        targets[f"tou.{slot}.start"] = WriteTarget(
            key=f"tou.{slot}.start",
            label=f"Time of Use {slot} start",
            address=250 + base,
            kind=TargetKind.TIME,
        )
        targets[f"tou.{slot}.power"] = WriteTarget(
            key=f"tou.{slot}.power",
            label=f"Time of Use {slot} out power",
            address=256 + base,
            kind=TargetKind.POWER,
            minimum=0,
            maximum=12000,
            unit="W",
        )
        targets[f"tou.{slot}.voltage"] = WriteTarget(
            key=f"tou.{slot}.voltage",
            label=f"Time of Use {slot} voltage",
            address=262 + base,
            kind=TargetKind.VOLTAGE,
            minimum=40,
            maximum=60,
            scale=100,
            unit="V",
        )
        targets[f"tou.{slot}.soc"] = WriteTarget(
            key=f"tou.{slot}.soc",
            label=f"Time of Use {slot} target SoC",
            address=268 + base,
            kind=TargetKind.SOC,
            minimum=10,
            maximum=100,
            unit="%",
        )
        # Bit targets: read-modify-write keeps GM/BU/CH intact.
        targets[f"tou.{slot}.grid_charge"] = WriteTarget(
            key=f"tou.{slot}.grid_charge",
            label=f"Time of Use {slot} grid charge",
            address=274 + base,
            kind=TargetKind.ENABLE,
            bit=0,
        )
        targets[f"tou.{slot}.gen_charge"] = WriteTarget(
            key=f"tou.{slot}.gen_charge",
            label=f"Time of Use {slot} generator charge",
            address=274 + base,
            kind=TargetKind.ENABLE,
            bit=1,
        )
        # Whole bitfield stays opt-in: writing a raw mask can clobber GM/BU/CH.
        targets[f"tou.{slot}.charge_mask"] = WriteTarget(
            key=f"tou.{slot}.charge_mask",
            label=f"Time of Use {slot} charge bitfield (grid/gen/GM/BU/CH)",
            address=274 + base,
            kind=TargetKind.MASK,
            minimum=0,
            maximum=31,
            enabled=False,
        )
    return targets


TARGETS: dict[str, WriteTarget] = _tou_targets()


def resolve(key: str, *, include_unverified: bool = False) -> WriteTarget:
    """Look up a write target, raising when it is unknown or not confirmed."""
    target = TARGETS.get(key)
    if target is None:
        raise RegisterNotAllowed(f"{key!r} is not on the write allow-list")
    if not target.enabled and not include_unverified:
        raise RegisterNotAllowed(
            f"{key!r} is not verified on this inverter model; "
            "pass --include-unverified only after checking the register by hand"
        )
    return target


def build_write_frame(slave: int, address: int, value: int) -> bytes:
    """Build a function 06 (write single register) frame with CRC."""
    body = bytes([slave, 0x06, address >> 8, address & 0xFF, value >> 8, value & 0xFF])
    crc = crc16(body)
    return body + bytes([crc & 0xFF, crc >> 8])


def build_write_multiple_frame(slave: int, address: int, values: list[int]) -> bytes:
    """Build a function 16 (write multiple registers) frame with CRC."""
    count = len(values)
    payload = b"".join(value.to_bytes(2, "big") for value in values)
    body = (
        bytes([slave, 0x10, address >> 8, address & 0xFF, count >> 8, count & 0xFF, count * 2])
        + payload
    )
    crc = crc16(body)
    return body + bytes([crc & 0xFF, crc >> 8])


class WriteClient(ModbusClient):
    """Modbus client that can also write single registers.

    Lives here, not in ``modbus``, so the monitoring service has no write code path at all.
    """

    def write_registers(self, address: int, values: list[int]) -> None:
        """Write holding registers with FC16 and check the echoed header.

        The SUN-6K firmware used here silently ignores FC6 for the Time-of-Use block and only
        answers FC16, so every write goes through this function.
        """
        if not values:
            raise ValueError("values must not be empty")
        if self._serial is None:
            raise RuntimeError("port is not open, call connect() first")
        frame = build_write_multiple_frame(self.config.slave, address, values)
        header = frame[:6]  # slave, function, address, register count
        crc = crc16(header)
        expected = header + bytes([crc & 0xFF, crc >> 8])
        try:
            self._serial.reset_input_buffer()
            self._serial.write(frame)
            self._serial.flush()
            reply = self._serial.read(len(expected))
        except serial.SerialException as error:
            raise RuntimeError(f"serial I/O failed: {error}") from error
        if not reply:
            raise WriteVerificationError(f"no reply for write at 0x{address:04X}")
        if reply != expected:
            raise WriteVerificationError(
                f"echo mismatch at 0x{address:04X}: sent {frame.hex(' ')}, got {reply.hex(' ')}"
            )

    def write_register(self, address: int, value: int) -> None:
        """Write one holding register as a single-register FC16 frame."""
        self.write_registers(address, [value])


def build_read_frame_for(slave: int, address: int, count: int = 1) -> bytes:
    """Convenience wrapper kept for tests and external callers."""
    return build_read_frame(slave, address, count)


@dataclass(frozen=True)
class PlannedWrite:
    """A validated change, ready to be applied or merely reported."""

    target: WriteTarget
    value: float
    raw: int
    current: int | None

    def describe(self) -> str:
        """Human readable summary of the change."""
        before = self.target.describe(self.current) if self.current is not None else "unknown"
        return f"{self.target.key}: {before} -> {self.target.describe_value(self.value)}"


def plan_change(target: WriteTarget, value: float, current: int | None) -> PlannedWrite:
    """Validate *value* against *target* and return a dry-run record."""
    return PlannedWrite(target=target, value=value, raw=target.to_raw(value), current=current)


def apply_change(client: WriteClient, change: PlannedWrite) -> int:
    """Write one register (or one bit), verify it by readback, return the written word."""
    target = change.target
    if target.bit is not None:
        current = client.read_holding_registers(target.address, 1)[0]
        new_word = (current & ~(1 << target.bit)) | ((int(change.value) & 1) << target.bit)
        client.write_register(target.address, new_word)
        actual = client.read_holding_registers(target.address, 1)[0]
        if ((actual >> target.bit) & 1) != int(change.value):
            raise WriteVerificationError(
                f"{target.key}: wanted {int(change.value)}, "
                f"read back bit {target.bit} of 0x{actual:04X}"
            )
        return new_word

    client.write_register(target.address, change.raw)
    actual = client.read_holding_registers(target.address, 1)[0]
    if actual != change.raw:
        raise WriteVerificationError(f"{target.key}: wrote {change.raw}, read back {actual}")
    return change.raw


def matches(target: WriteTarget, expected_raw: int, current_raw: int | None) -> bool:
    """True when the live register already equals the expected encoded value."""
    if current_raw is None:
        return False
    if target.bit is not None:
        return ((current_raw >> target.bit) & 1) == expected_raw
    return current_raw == expected_raw


def audit(
    path: str,
    change: PlannedWrite,
    *,
    serial_number: str | None,
    confirmed: bool,
    written_raw: int | None = None,
) -> None:
    """Append one JSON line describing the intended or performed change.

    ``written_raw`` carries the whole word for bit targets, where ``change.raw`` is only 0 or 1.
    """
    record = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "serial": serial_number,
        "key": change.target.key,
        "address": f"0x{change.target.address:04X}",
        "from_raw": change.current,
        "to_raw": change.raw if written_raw is None else written_raw,
        "value": change.value,
        "confirmed": confirmed,
    }
    directory = os.path.dirname(path)
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as error:
        print(f"warning: could not write audit log {path}: {error}")
