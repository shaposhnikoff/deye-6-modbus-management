"""Tests for the Modbus RTU framing and reply handling."""

from __future__ import annotations

import pytest

from deye_monitoring.modbus import (
    ModbusClient,
    ModbusConfig,
    ModbusError,
    ModbusExceptionResponse,
    ModbusTimeout,
    build_read_frame,
    crc16,
)


def test_crc16_matches_real_inverter_frame() -> None:
    # Frame captured from the inverter: read 1 register at 0x0003 from slave 1.
    frame = bytes.fromhex("01 03 00 03 00 01 74 0a")
    assert crc16(frame[:-2]) == int.from_bytes(frame[-2:], "little")


def test_build_read_frame() -> None:
    assert build_read_frame(1, 0x0003, 1).hex(" ") == "01 03 00 03 00 01 74 0a"


def test_build_read_frame_takes_high_byte_first() -> None:
    assert build_read_frame(1, 0x0102, 2)[2:6] == bytes([0x01, 0x02, 0x00, 0x02])


class FakeSerial:
    """Just enough of pyserial's interface for the client under test."""

    def __init__(self, replies: list[bytes]) -> None:
        self.replies = list(replies)
        self.written = b""
        self.buffer = b""

    def reset_input_buffer(self) -> None:
        self.buffer = b""

    def write(self, data: bytes) -> int:
        self.written += data
        self.buffer = self.replies.pop(0) if self.replies else b""
        return len(data)

    def flush(self) -> None:
        return None

    def read(self, size: int) -> bytes:
        chunk, self.buffer = self.buffer[:size], self.buffer[size:]
        return chunk

    def close(self) -> None:
        return None


def _client_with(replies: list[bytes], retries: int = 0) -> ModbusClient:
    client = ModbusClient(ModbusConfig(port="fake", retries=retries))
    client._serial = FakeSerial(replies)  # noqa: SLF001 - test double
    return client


def _reply(slave: int, payload: bytes) -> bytes:
    header = bytes([slave, 0x03, len(payload)])
    crc = crc16(header + payload)
    return header + payload + bytes([crc & 0xFF, crc >> 8])


def test_read_holding_registers_returns_words() -> None:
    client = _client_with([_reply(1, bytes.fromhex("00 03 00 01"))])
    assert client.read_holding_registers(0x0000, 2) == [3, 1]


def test_exception_response_is_reported() -> None:
    header = bytes([1, 0x83, 0x02])
    crc = crc16(header)
    client = _client_with([header + bytes([crc & 0xFF, crc >> 8])])
    with pytest.raises(ModbusExceptionResponse, match="illegal data address"):
        client.read_holding_registers(0x0000, 1)


def test_timeout_after_retries() -> None:
    client = _client_with([b"", b""], retries=1)
    with pytest.raises(ModbusTimeout):
        client.read_holding_registers(0x0000, 1)


def test_crc_mismatch_is_rejected() -> None:
    frame = bytearray(_reply(1, bytes.fromhex("00 03")))
    frame[-1] ^= 0xFF
    client = _client_with([bytes(frame)])
    with pytest.raises(ModbusError, match="CRC"):
        client.read_holding_registers(0x0000, 1)


def test_count_must_be_within_modbus_limit() -> None:
    client = _client_with([])
    with pytest.raises(ValueError):
        client.read_holding_registers(0, 126)
