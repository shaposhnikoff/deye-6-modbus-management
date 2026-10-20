"""Guarded write support: Modbus FC6/FC16 frames, an allow-list and readback checks.

This module is deliberately not imported by the monitoring service. It exists so a human can
change the Time-of-Use schedule with an explicit confirmation, and it refuses everything that is
not on the allow-list.

Safety rules:
- every write is validated against the target's range before a frame is built;
- every write is verified by reading the register back;
- targets whose behaviour is not confirmed on this inverter model are marked ``verified=False``
  and are hidden unless the operator asks for them explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from deye_monitoring.modbus import ModbusClient, crc16


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


@dataclass(frozen=True)
class WriteTarget:
    """One writable register with its encoding and safety limits."""

    key: str
    label: str
    address: int
    kind: TargetKind
    enabled: bool = True
    minimum: float = 0.0
    maximum: float = 65535.0
    scale: float = 1.0
    unit: str = ""

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
        if self.kind == TargetKind.ENABLE and raw not in (0, 1):
            raise WriteValueError(f"{self.key}: enable must be 0 or 1, got {value}")
        return raw

    def from_raw(self, raw: int) -> float:
        """Decode a raw register word into a human value."""
        return raw / self.scale if self.scale != 1 else float(raw)

    def describe(self, raw: int) -> str:
        """Format a raw value for humans, e.g. ``2100 -> 21:00``."""
        value = self.from_raw(raw)
        if self.kind == TargetKind.TIME:
            hours, minutes = divmod(int(value), 100)
            return f"{hours:02d}:{minutes:02d}"
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
        # Unconfirmed on SUN-6K-SG05LP1: the registers read 5/7 instead of 0/1,
        # so writing 0/1 could flip an unrelated setting.
        targets[f"tou.{slot}.charge_enable"] = WriteTarget(
            key=f"tou.{slot}.charge_enable",
            label=f"Time of Use {slot} charge enable",
            address=274 + base,
            kind=TargetKind.ENABLE,
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


@dataclass(frozen=True)
class PlannedWrite:
    """A validated change, ready to be applied or merely reported."""

    target: WriteTarget
    raw: int
    current: int | None

    def describe(self) -> str:
        """Human readable summary of the change."""
        before = self.target.describe(self.current) if self.current is not None else "unknown"
        return f"{self.target.key}: {before} -> {self.target.describe(self.raw)}"


def plan_change(target: WriteTarget, value: float, current: int | None) -> PlannedWrite:
    """Validate *value* against *target* and return a dry-run record."""
    return PlannedWrite(target=target, raw=target.to_raw(value), current=current)


def apply_change(client: ModbusClient, change: PlannedWrite) -> None:
    """Write one register and verify it by reading it back."""
    client.write_single_register(change.target.address, change.raw)
    actual = client.read_holding_registers(change.target.address, 1)[0]
    if actual != change.raw:
        raise WriteVerificationError(f"{change.target.key}: wrote {change.raw}, read back {actual}")
