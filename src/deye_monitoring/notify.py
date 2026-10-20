"""Optional Telegram notifications.

Works standalone: set ``DEYE_TELEGRAM_TOKEN`` and ``DEYE_TELEGRAM_CHAT_ID`` and the exporter
reports problems itself, without Prometheus or Alertmanager. Sending is best-effort: a failing
bot API must never interrupt the Modbus poll loop.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from deye_monitoring.enums import RUNNING_STATUS, RUNNING_STATUS_ADDRESS
from deye_monitoring.exporter import Sample

LOGGER = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
Transport = Callable[[str, dict], None]


def urllib_transport(url: str, payload: dict) -> None:
    """POST *payload* as JSON to *url* (the default transport)."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


@dataclass(frozen=True)
class NotifyConfig:
    """Telegram and threshold settings, all overridable through the environment."""

    token: str | None = None
    chat_id: str | None = None
    temp_warning_c: float = 65.0
    stale_seconds: float = 120.0
    cooldown: float = 1800.0

    @property
    def enabled(self) -> bool:
        """True when both a token and a chat id are configured."""
        return bool(self.token and self.chat_id)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Notifier:
    """Turn inverter state into Telegram messages, with cooldown and recovery notices."""

    def __init__(
        self,
        config: NotifyConfig,
        transport: Transport = urllib_transport,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._transport = transport
        self._clock = clock
        self._firing: dict[str, str] = {}
        self._last_sent: dict[str, float] = {}

    def handle(self, sample: Sample) -> None:
        """Evaluate a snapshot and emit any needed messages."""
        if not self.config.enabled:
            return

        current = self._active_alerts(sample)
        now = self._clock()

        for key, text in current.items():
            last = self._last_sent.get(key)
            if last is None or now - last >= self.config.cooldown:
                self._send(self._format(sample, text, resolved=False))
                self._last_sent[key] = now

        for key, text in self._firing.items():
            if key not in current:
                self._send(self._format(sample, text, resolved=True))

        self._firing = current

    def _active_alerts(self, sample: Sample) -> dict[str, str]:
        alerts: dict[str, str] = {}
        serial = sample.serial or "unknown"
        if sample.error:
            alerts["scrape_failing"] = f"Modbus reads failing: {sample.error}"
        age = time.time() - sample.last_success if sample.last_success else None
        if age is not None and age > self.config.stale_seconds:
            alerts["stale"] = (
                f"No fresh data for {age:.0f} s (limit {self.config.stale_seconds:.0f} s)"
            )
        for reading in sample.readings:
            if reading.address == RUNNING_STATUS_ADDRESS:
                status = int(reading.value)
                if status == 4:
                    alerts["fault"] = "Inverter reports fault"
                elif status == 3:
                    alerts["alarm"] = "Inverter reports alarm"
            if (
                reading.name in {"DC Temperature", "AC Temperature"}
                and reading.value > self.config.temp_warning_c
            ):
                alerts[f"temp:{reading.name}"] = (
                    f"{reading.name} {reading.value:.1f} C "
                    f"(limit {self.config.temp_warning_c:.0f} C)"
                )
        if not alerts:
            LOGGER.debug("no active alerts for %s", serial)
        return alerts

    def _format(self, sample: Sample, text: str, *, resolved: bool) -> str:
        header = "RECOVERED" if resolved else "ALERT"
        serial = _escape(sample.serial or "unknown")
        body = _escape(text)
        return f"<b>{header}</b> Deye SUN-6K\nSerial: <code>{serial}</code>\n{body}"

    def _send(self, text: str) -> None:
        url = API_URL.format(token=self.config.token)
        payload = {
            "chat_id": self.config.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            self._transport(url, payload)
        except Exception as error:  # noqa: BLE001 - notifications must never break polling
            LOGGER.warning("telegram send failed: %s", error)
        else:
            LOGGER.info("telegram message sent: %s", text.splitlines()[0])


def describe_status(value: int | None) -> str:
    """Return the human readable name for a running status code."""
    return RUNNING_STATUS.get(value, "unknown") if value is not None else "unknown"
