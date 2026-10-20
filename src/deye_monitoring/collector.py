"""Single owner of the serial line: polls the inverter and caches the snapshot."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from deye_monitoring.exporter import Sample
from deye_monitoring.modbus import ModbusConfig
from deye_monitoring.snapshot import Reading, decode_all, read_registers, serial_number

LOGGER = logging.getLogger(__name__)

Reader = Callable[[], tuple[list[Reading], str | None]]


def modbus_reader(config: ModbusConfig) -> Reader:
    """Build a reader that opens the port per poll.

    Opening per poll keeps recovery simple: a replugged adapter or a transient
    serial error never leaves a broken handle behind.
    """

    def read() -> tuple[list[Reading], str | None]:
        from deye_monitoring.modbus import ModbusClient

        with ModbusClient(config) as client:
            registers = read_registers(client)
        readings, _tou_state = decode_all(registers)
        return readings, serial_number(registers)

    return read


class Collector:
    """Poll the inverter in a background thread and serve the last snapshot."""

    def __init__(
        self,
        reader: Reader,
        interval: float = 10.0,
        on_sample: Callable[[Sample], None] | None = None,
    ) -> None:
        self._reader = reader
        self.interval = interval
        self._on_sample = on_sample
        self._sample = Sample()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def latest(self) -> Sample:
        """Return the cached snapshot without touching the serial port."""
        with self._lock:
            return self._sample

    def poll_once(self) -> Sample:
        """Read the inverter once, cache the result and notify the listener."""
        started = time.monotonic()
        try:
            readings, serial = self._reader()
        except Exception as error:  # noqa: BLE001 - the poll loop must survive anything
            LOGGER.warning("modbus poll failed: %s", error)
            with self._lock:
                previous = self._sample
                self._sample = Sample(
                    readings=previous.readings,
                    serial=previous.serial,
                    duration=time.monotonic() - started,
                    last_success=previous.last_success,
                    error=str(error),
                )
                sample = self._sample
        else:
            duration = time.monotonic() - started
            with self._lock:
                self._sample = Sample(
                    readings=tuple(readings),
                    serial=serial,
                    duration=duration,
                    last_success=time.time(),
                    error=None,
                )
                sample = self._sample

        if self._on_sample is not None:
            try:
                self._on_sample(sample)
            except Exception:  # noqa: BLE001 - a listener bug must not stop polling
                LOGGER.exception("sample listener failed")
        return sample

    def start(self) -> None:
        """Start the polling thread (idempotent)."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="deye-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the polling thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            self.poll_once()
            elapsed = time.monotonic() - started
            self._stop.wait(max(self.interval - elapsed, 0.0))
