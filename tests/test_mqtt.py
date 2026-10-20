"""Tests for the MQTT bridge (no broker involved)."""

from __future__ import annotations

import json
import time

from deye_monitoring.cache import CacheSnapshot
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
    )
    return Sample(readings=readings, serial=SERIAL, duration=0.2, last_success=time.time())


VALUES = {"PV1 Power": 654.0, "Battery": 89.0}
RUNNING = {"Running Status": 2.0}


def _config() -> MqttConfig:
    return MqttConfig(host="broker", port=1883, node="deye_6k")


def _snapshot(*, age: float = 1.0, online: bool = True) -> CacheSnapshot:
    return CacheSnapshot(
        unix=time.time() - age,
        serial=SERIAL,
        online=online,
        interval=10.0,
        values={"PV1 Power": 654, "Battery": 89},
        tou={"enabled": True, "slots": []},
    )


def test_state_payload_shape() -> None:
    payload = json.loads(state_payload({"PV1 Power": 654}, SERIAL, True))
    assert payload["serial"] == SERIAL
    assert payload["online"] is True
    assert payload["values"]["PV1 Power"] == 654


def test_state_payload_includes_tou_and_error() -> None:
    payload = json.loads(
        state_payload({"A": 1}, SERIAL, False, error="no reply", tou={"enabled": True})
    )
    assert payload["online"] is False
    assert payload["error"] == "no reply"
    assert payload["tou"] == {"enabled": True}


def test_discovery_covers_published_values_and_skips_status() -> None:
    configs = discovery_configs({**VALUES, **RUNNING}, SERIAL, _config())
    assert len(configs) == 2
    topic = "homeassistant/sensor/deye_6k/deye_6k_pv1_power/config"
    payload = json.loads(configs[topic])
    assert payload["name"] == "PV1 Power"
    assert payload["unit_of_measurement"] == "W"
    assert payload["state_topic"] == "deye/state"
    assert payload["availability_topic"] == "deye/availability"
    assert payload["value_template"] == "{{ value_json['values']['PV1 Power'] }}"
    assert payload["device"]["serial_number"] == SERIAL


def test_discovery_unique_ids_are_unique() -> None:
    ids = [
        json.loads(payload)["unique_id"]
        for payload in discovery_configs(VALUES, SERIAL, _config()).values()
    ]
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
    assert discovery_after_first == 2
    publisher.handle(_sample())
    discovery_after_second = sum(1 for topic, _, _ in client.published if topic.endswith("/config"))
    assert discovery_after_second == discovery_after_first


def test_publish_from_cache_publishes_state_and_discovery() -> None:
    client = FakeClient()
    publisher = MqttPublisher(_config(), client=client)  # type: ignore[arg-type]
    publisher.publish_from_cache(_snapshot())
    topics = [topic for topic, _, _ in client.published]
    assert "deye/state" in topics
    assert any(topic.endswith("/config") for topic in topics)


def test_publish_from_stale_cache_goes_offline() -> None:
    client = FakeClient()
    publisher = MqttPublisher(_config(), client=client)  # type: ignore[arg-type]
    publisher.publish_from_cache(_snapshot(age=60))
    assert ("deye/availability", "offline", True) in client.published
    assert all(topic != "deye/state" for topic, _, _ in client.published)


def test_publisher_sets_credentials_when_configured() -> None:
    client = FakeClient()
    MqttPublisher(MqttConfig(username="user", password="secret"), client=client)  # type: ignore[arg-type]
    assert client.auth == ("user", "secret")


def test_node_id_sanitises() -> None:
    assert node_id("DEYE 6K!") == "deye_6k"


def test_availability_returns_to_online_after_recovery() -> None:
    client = FakeClient()
    publisher = MqttPublisher(_config(), client=client)  # type: ignore[arg-type]
    publisher.publish_from_cache(_snapshot(age=60))
    publisher.publish_from_cache(_snapshot(age=1))
    availability = [
        payload for topic, payload, _ in client.published if topic == "deye/availability"
    ]
    assert availability == ["offline", "online"]


def test_availability_does_not_flap_on_repeated_ticks() -> None:
    client = FakeClient()
    publisher = MqttPublisher(_config(), client=client)  # type: ignore[arg-type]
    for _ in range(3):
        publisher.publish_from_cache(_snapshot(age=1))
    availability = [
        payload for topic, payload, _ in client.published if topic == "deye/availability"
    ]
    assert availability == ["online"]
