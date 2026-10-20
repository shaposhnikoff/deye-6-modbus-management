"""Minimal, read-only Modbus RTU client for Deye hybrid inverters.

Only function code 0x03 (read holding registers) is implemented on purpose:
the monitoring service must never write to the inverter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import serial

EXCEPTION_CODES = {
    0x01: "illegal function",
    0x02: "illegal data address",
    0x03: "illegal data value",
    0x04: "slave device failure",
    0x06: "slave device busy",
}

MAX_REGISTERS_PER_READ = 125


class ModbusError(RuntimeError):
    """Base error for Modbus transport problems."""


class ModbusTimeout(ModbusError):
    """The inverter did not answer in time."""


class ModbusExceptionResponse(ModbusError):
    """The inverter answered with a Modbus exception frame."""


def crc16(data: bytes) -> int:
    """Return the Modbus RTU CRC-16 (polynomial 0xA001) for *data*."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build_read_frame(slave: int, start: int, count: int) -> bytes:
    """Build an FC3 request frame including the trailing CRC."""
    body = bytes([slave, 0x03, start >> 8, start & 0xFF, count >> 8, count & 0xFF])
    crc = crc16(body)
    return body + bytes([crc & 0xFF, crc >> 8])


@dataclass(frozen=True)
class ModbusConfig:
    """Serial line settings for the inverter's Modbus RTU bus."""

    port: str = "/dev/modbus"
    baudrate: int = 9600
    slave: int = 1
    timeout: float = 1.0
    retries: int = 2


class ModbusClient:
    """Blocking Modbus RTU client that only reads holding registers."""

    def __init__(self, config: ModbusConfig) -> None:
        self.config = config
        self._serial: serial.Serial | None = None

    def connect(self) -> None:
        """Open the serial port if it is not open yet."""
        if self._serial is not None:
            return
        self._serial = serial.Serial(
            port=self.config.port,
            baudrate=self.config.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.config.timeout,
            write_timeout=self.config.timeout,
            exclusive=True,
        )
        self._serial.reset_input_buffer()

    def close(self) -> None:
        """Close the serial port."""
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def __enter__(self) -> ModbusClient:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def read_holding_registers(self, start: int, count: int) -> list[int]:
        """Read *count* holding registers starting at *start* with retries."""
        if not 1 <= count <= MAX_REGISTERS_PER_READ:
            raise ValueError(f"count must be between 1 and {MAX_REGISTERS_PER_READ}")
        last_error: ModbusError | None = None
        for attempt in range(self.config.retries + 1):
            try:
                return self._read_once(start, count)
            except ModbusError as error:
                last_error = error
                if attempt < self.config.retries:
                    time.sleep(0.15 * (attempt + 1))
        assert last_error is not None
        raise last_error

    def _read_once(self, start: int, count: int) -> list[int]:
        if self._serial is None:
            raise ModbusError("port is not open, call connect() first")
        frame = build_read_frame(self.config.slave, start, count)
        try:
            self._serial.reset_input_buffer()
            self._serial.write(frame)
            self._serial.flush()
            header = self._serial.read(3)
        except serial.SerialException as error:
            raise ModbusError(f"serial I/O failed: {error}") from error

        if len(header) != 3:
            raise ModbusTimeout(f"no reply for {count} registers at 0x{start:04X}")

        slave, function, third = header
        if slave != self.config.slave:
            raise ModbusError(f"reply from slave {slave}, expected {self.config.slave}")
        if function == 0x83:
            code = third
            raise ModbusExceptionResponse(
                f"inverter rejected read at 0x{start:04X}: "
                f"{EXCEPTION_CODES.get(code, f'code 0x{code:02X}')}"
            )
        if function != 0x03:
            raise ModbusError(f"unexpected function code 0x{function:02X}")
        if third != count * 2:
            raise ModbusError(f"payload length {third} does not match count {count}")

        try:
            payload = self._serial.read(third + 2)
        except serial.SerialException as error:
            raise ModbusError(f"serial I/O failed: {error}") from error
        if len(payload) != third + 2:
            raise ModbusTimeout("reply truncated while reading payload")

        data, received_crc = payload[:-2], payload[-2:]
        expected_crc = crc16(header + data)
        if received_crc != bytes([expected_crc & 0xFF, expected_crc >> 8]):
            raise ModbusError("CRC mismatch in reply")

        return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]
