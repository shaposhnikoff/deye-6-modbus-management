"""Tests for the MQTT bridge (no broker involved)."""

from __future__ import annotations

import json
import time

from deye_monitoring.exporter import Sample
from deye_monitoring.mqtt import (
    MqttConfig,
    MqttPublisher,
    discovery_configs,
    node_id,
    state_payload,
)
from deye_monitoring.snapshot import Reading

SERIAL = "2601255637"


class FakeClient:
    """Records published messages instead of talking to a broker."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool]] = []
        self.auth: tuple[str, str | None] | None = None
        self.will: tuple[str, str] | None = None
        self.connection: tuple[str, int] | None = None
        self.looping = False
        self.disconnected = False

    def username_pw_set(self, username: str, password: str | None) -> None:
        self.auth = (username, password)

    def will_set(self, topic: str, payload: str, retain: bool | None = None) -> None:
        self.will = (topic, payload)

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        self.connection = (host, port)

    def loop_start(self) -> None:
        self.looping = True

    def loop_stop(self) -> None:
        self.looping = False

    def disconnect(self) -> None:
        self.disconnected = True

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        self.published.append((topic, payload, retain))


def _sample() -> Sample:
    readings = (
        Reading("PV1 Power", 654, "W", 0x00BA, "pv"),
        Reading("Battery", 89, "%", 0x00B8, "battery"),
        Reading("Running Status", 2, "", 0x003B, "device"),
    )
    return Sample(readings=readings, serial=SERIAL, duration=0.2, last_success=time.time())


def _config() -> MqttConfig:
    return MqttConfig(host="broker", port=1883, node="deye_6k")


def test_state_payload_shape() -> None:
    payload = json.loads(state_payload(_sample()))
    assert payload["serial"] == SERIAL
    assert payload["online"] is True
    assert payload["values"]["PV1 Power"] == 654
    assert payload["values"]["Battery"] == 89


def test_state_payload_marks_failure() -> None:
    sample = Sample(readings=(), serial=SERIAL, error="no reply")
    payload = json.loads(state_payload(sample))
    assert payload["online"] is False
    assert payload["error"] == "no reply"


def test_discovery_topics_and_payload() -> None:
    configs = discovery_configs(_sample(), _config())
    topic = "homeassistant/sensor/deye_6k/deye_6k_pv1_power/config"
    assert topic in configs
    payload = json.loads(configs[topic])
    assert payload["name"] == "PV1 Power"
    assert payload["unit_of_measurement"] == "W"
    assert payload["state_topic"] == "deye/state"
    assert payload["availability_topic"] == "deye/availability"
    assert payload["value_template"] == "{{ value_json.values['PV1 Power'] }}"
    assert payload["device"]["identifiers"] == ["deye_6k"]
    assert payload["device"]["serial_number"] == SERIAL


def test_discovery_skips_running_status() -> None:
    configs = discovery_configs(_sample(), _config())
    names = [json.loads(payload)["name"] for payload in configs.values()]
    assert "Running Status" not in names


def test_discovery_unique_ids_are_unique() -> None:
    configs = discovery_configs(_sample(), _config())
    ids = [json.loads(payload)["unique_id"] for payload in configs.values()]
    assert len(ids) == len(set(ids))


def test_publisher_connect_sets_availability_and_lwt() -> None:
    client = FakeClient()
    publisher = MqttPublisher(_config(), client=client)  # type: ignore[arg-type]
    publisher.connect()
    assert client.connection == ("broker", 1883)
    assert client.looping is True
    assert ("deye/availability", "online", True) in client.published


def test_publisher_close_publishes_offline() -> None:
    client = FakeClient()
    publisher = MqttPublisher(_config(), client=client)  # type: ignore[arg-type]
    publisher.connect()
    publisher.close()
    assert ("deye/availability", "offline", True) in client.published
    assert client.disconnected is True


def test_handle_publishes_state_then_discovery_once() -> None:
    client = FakeClient()
    publisher = MqttPublisher(_config(), client=client)  # type: ignore[arg-type]
    publisher.handle(_sample())
    discovery_after_first = sum(1 for topic, _, _ in client.published if topic.endswith("/config"))
    assert discovery_after_first >= 2
    publisher.handle(_sample())
    discovery_after_second = sum(1 for topic, _, _ in client.published if topic.endswith("/config"))
    assert discovery_after_second == discovery_after_first
    states = [topic for topic, _, _ in client.published if topic == "deye/state"]
    assert len(states) == 2


def test_publisher_sets_credentials_when_configured() -> None:
    client = FakeClient()
    config = MqttConfig(username="user", password="secret")
    MqttPublisher(config, client=client)  # type: ignore[arg-type]
    assert client.auth == ("user", "secret")


def test_node_id_sanitises() -> None:
    assert node_id("DEYE 6K!") == "deye_6k"
