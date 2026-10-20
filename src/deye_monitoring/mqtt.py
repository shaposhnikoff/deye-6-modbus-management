"""MQTT bridge: publish inverter state and Home Assistant discovery.

Two modes share the same publisher:

* ``dmon serve --mqtt`` publishes from the live sample (same process as the serial owner);
* ``dmon publish --cache /run/deye/state.json`` publishes from the local JSON cache and never
  touches the serial port, so it can run as its own service.

Home Assistant gets one retained discovery config per register, so no YAML is needed there.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

import paho.mqtt.client as mqtt

from deye_monitoring.cache import CacheSnapshot
from deye_monitoring.exporter import Sample
from deye_monitoring.metrics import slugify
from deye_monitoring.registers import REGISTERS

LOGGER = logging.getLogger(__name__)


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


def state_payload(
    values: dict[str, float],
    serial: str | None,
    online: bool,
    *,
    error: str | None = None,
    timestamp: str | None = None,
    tou: dict | None = None,
) -> str:
    """JSON payload describing one poll."""
    payload: dict[str, object] = {
        "timestamp": timestamp or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "serial": serial,
        "online": online,
        "values": values,
    }
    if error:
        payload["error"] = error
    if tou:
        payload["tou"] = tou
    return json.dumps(payload, ensure_ascii=False)


UNIT_BY_NAME = {register.name: register.unit for register in REGISTERS}

# Home Assistant needs these to accept a sensor in the Energy dashboard.
DEVICE_CLASS_BY_UNIT = {
    "W": "power",
    "kW": "power",
    "kWh": "energy",
    "V": "voltage",
    "A": "current",
    "°C": "temperature",
    "Hz": "frequency",
}
STATE_CLASS_BY_UNIT = {"kWh": "total_increasing"}


def discovery_configs(
    values: dict[str, float], serial: str | None, config: MqttConfig
) -> dict[str, str]:
    """Build ``topic -> payload`` pairs for Home Assistant MQTT discovery."""
    device = {
        "identifiers": [config.node],
        "name": "Deye SUN-6K",
        "manufacturer": "Deye",
        "model": "SUN-6K-SG05LP1",
    }
    if serial:
        device["serial_number"] = serial

    configs: dict[str, str] = {}
    for name in sorted(values):
        if name == "Running Status":
            continue
        unit = UNIT_BY_NAME.get(name, "")
        unique = _unique_id(config.node, name)
        payload = {
            "name": name,
            "unique_id": unique,
            "state_topic": config.state_topic,
            "availability_topic": config.availability_topic,
            # Subscript, not attribute access: value_json.values() is a dict method.
            "value_template": "{{ value_json['values']['" + name + "'] }}",
            "device": device,
        }
        if unit:
            payload["unit_of_measurement"] = unit
            device_class = DEVICE_CLASS_BY_UNIT.get(unit)
            if device_class:
                payload["device_class"] = device_class
            payload["state_class"] = STATE_CLASS_BY_UNIT.get(unit, "measurement")
        configs[f"{config.discovery_topic}/{unique}/config"] = json.dumps(
            payload, ensure_ascii=False
        )
    return configs


def node_id(value: str) -> str:
    """Sanitise a node id for topic names."""
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


class MqttPublisher:
    """Thin wrapper around paho with discovery and availability handling."""

    def __init__(self, config: MqttConfig, client: mqtt.Client | None = None) -> None:
        self.config = config
        self._client = client or mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{config.node}-{int(time.time())}",
        )
        self._discovered = False
        self._online: bool | None = None
        if config.username:
            self._client.username_pw_set(config.username, config.password)
        self._client.will_set(config.availability_topic, "offline", retain=True)

    def connect(self) -> None:
        """Connect, start the loop and mark the node online."""
        self._client.connect(self.config.host, self.config.port, keepalive=60)
        self._client.loop_start()
        self._set_available(True)
        LOGGER.info("mqtt connected to %s:%s", self.config.host, self.config.port)

    def close(self) -> None:
        """Publish offline and disconnect."""
        try:
            self._client.publish(self.config.availability_topic, "offline", retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as error:  # noqa: BLE001 - shutdown must not raise
            LOGGER.debug("mqtt shutdown: %s", error)

    def publish_discovery(self, values: dict[str, float], serial: str | None) -> None:
        """Publish retained discovery configs for the values we actually publish."""
        configs = discovery_configs(values, serial, self.config)
        for topic, payload in configs.items():
            self._client.publish(topic, payload, qos=1, retain=True)
        LOGGER.info("published %d discovery configs", len(configs))

    def publish_state(
        self,
        values: dict[str, float],
        serial: str | None,
        online: bool,
        *,
        error: str | None = None,
        timestamp: str | None = None,
        tou: dict | None = None,
    ) -> None:
        """Publish one state snapshot."""
        self._client.publish(
            self.config.state_topic,
            state_payload(values, serial, online, error=error, timestamp=timestamp, tou=tou),
            qos=1,
            retain=self.config.retain,
        )

    def _set_available(self, online: bool, reason: str = "") -> None:
        """Publish availability only when it changes, so the topic never flaps."""
        if online == self._online:
            return
        self._online = online
        self._client.publish(
            self.config.availability_topic, "online" if online else "offline", retain=True
        )
        if not online:
            LOGGER.warning("publishing offline: %s", reason)

    def publish_offline(self, reason: str) -> None:
        """Flag the device as unavailable without publishing stale values."""
        self._set_available(False, reason)

    def handle(self, sample: Sample) -> None:
        """Collector callback used by ``serve --mqtt``."""
        values = {reading.name: reading.value for reading in sample.readings}
        self._set_available(sample.error is None, sample.error or "")
        self.publish_state(
            values,
            sample.serial,
            sample.error is None,
            error=sample.error,
        )
        if sample.error is None and not self._discovered:
            self.publish_discovery(values, sample.serial)
            self._discovered = True

    def publish_from_cache(self, snapshot: CacheSnapshot) -> None:
        """Publish a snapshot read from the local cache file."""
        if not snapshot.is_fresh():
            self.publish_offline(f"cache is {snapshot.age():.0f}s old")
            return
        self._set_available(True)
        self.publish_state(
            snapshot.values,
            snapshot.serial,
            snapshot.online,
            error=snapshot.error,
            tou=snapshot.tou,
        )
        if snapshot.online and not self._discovered:
            self.publish_discovery(snapshot.values, snapshot.serial)
            self._discovered = True
