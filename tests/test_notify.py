"""Tests for the optional Telegram notifier."""

from __future__ import annotations

import time

from deye_monitoring.exporter import Sample
from deye_monitoring.notify import Notifier, NotifyConfig, describe_status
from deye_monitoring.snapshot import Reading

SERIAL = "2601255637"


class FakeTransport:
    """Records outgoing messages instead of calling the Telegram API."""

    def __init__(self, fail: bool = False) -> None:
        self.requests: list[dict] = []
        self.fail = fail

    def __call__(self, url: str, payload: dict) -> None:
        if self.fail:
            raise OSError("network is down")
        self.requests.append({"url": url, "payload": payload})


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _sample(
    *,
    status: int = 2,
    dc_temp: float = 38.0,
    error: str | None = None,
    age: float = 1.0,
) -> Sample:
    readings = [
        Reading("Running Status", status, "", 0x003B, "device"),
        Reading("DC Temperature", dc_temp, "°C", 0x005A, "inverter"),
    ]
    return Sample(
        readings=tuple(readings),
        serial=SERIAL,
        duration=0.2,
        last_success=time.time() - age,
        error=error,
    )


def _notifier(transport: FakeTransport, clock: FakeClock, **overrides: object) -> Notifier:
    config = NotifyConfig(token="token", chat_id="42", **overrides)  # type: ignore[arg-type]
    return Notifier(config, transport=transport, clock=clock)


def test_disabled_without_token() -> None:
    transport = FakeTransport()
    notifier = Notifier(NotifyConfig(token=None, chat_id=None), transport=transport)
    notifier.handle(_sample(status=4))
    assert transport.requests == []


def test_disabled_without_chat_id() -> None:
    transport = FakeTransport()
    notifier = Notifier(NotifyConfig(token="token", chat_id=None), transport=transport)
    notifier.handle(_sample(status=4))
    assert transport.requests == []


def test_healthy_sample_sends_nothing() -> None:
    transport = FakeTransport()
    _notifier(transport, FakeClock()).handle(_sample())
    assert transport.requests == []


def test_fault_sends_alert_with_serial() -> None:
    transport = FakeTransport()
    _notifier(transport, FakeClock()).handle(_sample(status=4))
    payload = transport.requests[0]["payload"]
    assert "ALERT" in payload["text"]
    assert SERIAL in payload["text"]
    assert "fault" in payload["text"].lower()
    assert payload["chat_id"] == "42"
    assert payload["parse_mode"] == "HTML"


def test_alarm_is_reported_separately_from_fault() -> None:
    transport = FakeTransport()
    _notifier(transport, FakeClock()).handle(_sample(status=3))
    assert "alarm" in transport.requests[0]["payload"]["text"].lower()


def test_over_temperature_alert() -> None:
    transport = FakeTransport()
    _notifier(transport, FakeClock(), temp_warning_c=60).handle(_sample(dc_temp=68.4))
    text = transport.requests[0]["payload"]["text"]
    assert "DC Temperature" in text
    assert "68.4" in text


def test_stale_data_alert() -> None:
    transport = FakeTransport()
    _notifier(transport, FakeClock(), stale_seconds=60).handle(_sample(age=300))
    assert "No fresh data" in transport.requests[0]["payload"]["text"]


def test_scrape_failure_alert() -> None:
    transport = FakeTransport()
    _notifier(transport, FakeClock()).handle(_sample(error="no reply at 0x00FA"))
    text = transport.requests[0]["payload"]["text"]
    assert "Modbus reads failing" in text
    assert "no reply" in text


def test_cooldown_suppresses_repeats() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    notifier = _notifier(transport, clock, cooldown=600)
    notifier.handle(_sample(status=4))
    clock.advance(30)
    notifier.handle(_sample(status=4))
    assert len(transport.requests) == 1
    clock.advance(700)
    notifier.handle(_sample(status=4))
    assert len(transport.requests) == 2


def test_recovery_message_sent_once() -> None:
    transport = FakeTransport()
    clock = FakeClock()
    notifier = _notifier(transport, clock)
    notifier.handle(_sample(status=4))
    notifier.handle(_sample(status=2))
    notifier.handle(_sample(status=2))
    assert len(transport.requests) == 2
    assert "RECOVERED" in transport.requests[1]["payload"]["text"]


def test_transport_failure_does_not_raise() -> None:
    notifier = _notifier(FakeTransport(fail=True), FakeClock())
    notifier.handle(_sample(status=4))  # must not raise


def test_html_is_escaped() -> None:
    transport = FakeTransport()
    notifier = _notifier(transport, FakeClock())
    notifier.handle(_sample(error="bad <frame> & noise"))
    text = transport.requests[0]["payload"]["text"]
    assert "&lt;frame&gt;" in text
    assert "&amp;" in text


def test_describe_status() -> None:
    assert describe_status(2) == "normal"
    assert describe_status(4) == "fault"
    assert describe_status(None) == "unknown"
    assert describe_status(99) == "unknown"
