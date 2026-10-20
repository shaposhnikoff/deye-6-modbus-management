"""MQTT bridge: publish inverter state and Home Assistant discovery.

MQTT runs inside the collector process (``dmon serve --mqtt``) or as a standalone
``dmon mqtt``. Both own the serial port exclusively, so run only one of them.

Home Assistant gets one retained discovery config per sensor, so no YAML is needed there.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

import paho.mqtt.client as mqtt

from deye_monitoring.exporter import Sample
from deye_monitoring.metrics import slugify

LOGGER = logging.getLogger(__name__)

# The one-hot status register is not a usable numeric sensor in Home Assistant.
DISCOVERY_SKIP_NAMES = {"Running Status"}


@dataclass(frozen=True)
class MqttConfig:
    """Broker and topic layout."""

    host: str = "192.168.10.68"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    prefix: str = "homeassistant"
    node: str = "deye_6k"
    state_topic: str = "deye/state"
    availability_topic: str = "deye/availability"
    retain: bool = True

    @property
    def discovery_topic(self) -> str:
        """Prefix used for discovery configs."""
        return f"{self.prefix}/sensor/{self.node}"


def _unique_id(node: str, name: str) -> str:
    return f"{node}_{slugify(name)}"


def state_payload(sample: Sample) -> str:
    """JSON payload describing one poll."""
    values = {reading.name: reading.value for reading in sample.readings}
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "serial": sample.serial,
        "online": sample.error is None,
        "values": values,
    }
    if sample.error:
        payload["error"] = sample.error
    return json.dumps(payload, ensure_ascii=False)


def discovery_configs(sample: Sample, config: MqttConfig) -> dict[str, str]:
    """Build ``topic -> payload`` pairs for Home Assistant MQTT discovery."""
    device = {
        "identifiers": [config.node],
        "name": "Deye SUN-6K",
        "manufacturer": "Deye",
        "model": "SUN-6K-SG05LP1",
    }
    if sample.serial:
        device["serial_number"] = sample.serial

    configs: dict[str, str] = {}
    seen: set[str] = set()
    for reading in sample.readings:
        if reading.name in DISCOVERY_SKIP_NAMES:
            continue
        unique = _unique_id(config.node, reading.name)
        if unique in seen:
            continue
        seen.add(unique)
        payload = {
            "name": reading.name,
            "unique_id": unique,
            "state_topic": config.state_topic,
            "availability_topic": config.availability_topic,
            "value_template": "{{ value_json.values['" + reading.name + "'] }}",
            "device": device,
        }
        if reading.unit:
            payload["unit_of_measurement"] = reading.unit
            payload["state_class"] = "measurement"
        configs[f"{config.discovery_topic}/{unique}/config"] = json.dumps(
            payload, ensure_ascii=False
        )
    return configs


class MqttPublisher:
    """Thin wrapper around paho with discovery and availability handling."""

    def __init__(self, config: MqttConfig, client: mqtt.Client | None = None) -> None:
        self.config = config
        self._client = client or mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{config.node}-{int(time.time())}",
        )
        if config.username:
            self._client.username_pw_set(config.username, config.password)
        self._client.will_set(config.availability_topic, "offline", retain=True)

    def connect(self) -> None:
        """Connect, start the loop and mark the node online."""
        self._client.connect(self.config.host, self.config.port, keepalive=60)
        self._client.loop_start()
        self._client.publish(self.config.availability_topic, "online", retain=True)
        LOGGER.info("mqtt connected to %s:%s", self.config.host, self.config.port)

    def close(self) -> None:
        """Publish offline and disconnect."""
        try:
            self._client.publish(self.config.availability_topic, "offline", retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as error:  # noqa: BLE001 - shutdown must not raise
            LOGGER.debug("mqtt shutdown: %s", error)

    def publish_discovery(self, sample: Sample) -> None:
        """Publish retained discovery configs for every sensor."""
        configs = discovery_configs(sample, self.config)
        for topic, payload in configs.items():
            self._client.publish(topic, payload, qos=1, retain=True)
        LOGGER.info("published %d discovery configs", len(configs))

    def publish_state(self, sample: Sample) -> None:
        """Publish one state snapshot."""
        self._client.publish(
            self.config.state_topic, state_payload(sample), qos=1, retain=self.config.retain
        )

    def handle(self, sample: Sample) -> None:
        """Collector callback: publish state, and discovery once we know the serial."""
        self.publish_state(sample)
        if sample.error is None and not getattr(self, "_discovered", False):
            self.publish_discovery(sample)
            self._discovered = True


def node_id(value: str) -> str:
    """Sanitise a node id for topic names."""
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
