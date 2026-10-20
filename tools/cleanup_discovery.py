"""Remove retained Home Assistant discovery configs that the exporter no longer publishes.

Run after renaming sensors or dropping registers, otherwise Home Assistant keeps "unavailable"
entities that nothing will ever update.

    uv run python tools/cleanup_discovery.py [--dry-run]
"""

from __future__ import annotations

import argparse
import time

import paho.mqtt.client as mqtt

from deye_monitoring.cache import read_snapshot
from deye_monitoring.mqtt import MqttConfig, discovery_configs


def main() -> int:
    """Compare retained discovery topics with the expected set and delete the rest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", default="/run/deye/state.json")
    parser.add_argument("--broker", default=MqttConfig().host)
    parser.add_argument("--port", type=int, default=MqttConfig().port)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    snapshot = read_snapshot(args.cache)
    if snapshot is None:
        print(f"error: cannot read cache {args.cache}; is the collector running?")
        return 1

    config = MqttConfig(host=args.broker, port=args.port)
    expected = set(discovery_configs(snapshot.values, snapshot.serial, config))

    found: list[str] = []
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="deye-discovery-cleanup"
    )
    client.on_message = lambda _c, _u, message: found.append(message.topic)
    client.connect(config.host, config.port)
    client.subscribe(f"{config.discovery_topic}/#", qos=1)
    client.loop_start()
    time.sleep(8)
    client.loop_stop()

    orphans = sorted(set(found) - expected)
    print(f"expected {len(expected)}, found {len(set(found))}, orphans {len(orphans)}")
    if args.dry_run:
        for topic in orphans:
            print(f"  would delete {topic}")
        return 0

    for topic in orphans:
        client.publish(topic, None, qos=1, retain=True)
    if orphans:
        client.loop_start()
        time.sleep(2)
        client.loop_stop()
    print(f"deleted {len(orphans)} retained configs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
