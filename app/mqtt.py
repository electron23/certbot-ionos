"""Publish certificate status to MQTT with Home Assistant discovery.

Entities (all on one device "Zertifikats-Sync"):

    per certificate <name>:
        sensor.certsync_<name>_valid_until     timestamp
        sensor.certsync_<name>_days_left       days
        sensor.certsync_<name>_next_renewal    timestamp
        sensor.certsync_<name>_issued          timestamp
        sensor.certsync_<name>_status          ok / ausstehend / fehler (+ attributes)
    global:
        binary_sensor.certsync_problem         problem (+ attribute "probleme")
        sensor.certsync_last_check             timestamp
        event.certsync_event                   issued / deployed

Messages are retained, so Home Assistant has the values right after a restart.
Failures here are logged and never stop the certificate work.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
from pathlib import Path
from typing import Any

from .config import Config, MqttSettings

log = logging.getLogger("mqtt")

DEVICE = {
    "identifiers": ["certsync"],
    "name": "Zertifikats-Sync",
    "manufacturer": "certsync-ionos",
    "model": "Let's Encrypt via IONOS DNS",
    "configuration_url": "https://github.com/electron23/certbot-ionos",
}

CERT_ENTITIES = (
    # key,           platform, name suffix,          extra discovery fields
    ("valid_until",  "sensor", "gültig bis",          {"device_class": "timestamp", "icon": "mdi:certificate"}),
    ("days_left",    "sensor", "Restlaufzeit",        {"unit_of_measurement": "d", "state_class": "measurement",
                                                       "icon": "mdi:timer-sand"}),
    ("next_renewal", "sensor", "nächste Erneuerung",  {"device_class": "timestamp", "icon": "mdi:autorenew"}),
    ("issued",       "sensor", "ausgestellt am",      {"device_class": "timestamp", "icon": "mdi:calendar-check",
                                                       "entity_category": "diagnostic"}),
    ("status",       "sensor", "Status",              {"device_class": "enum", "options": ["ok", "ausstehend", "fehler"],
                                                       "icon": "mdi:shield-check"}),
)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class Publisher:
    def __init__(self, settings: MqttSettings, data_dir: str):
        import paho.mqtt.client as mqtt  # imported lazily: optional feature

        self.s = settings
        self.base = settings.topic
        self.avail = f"{self.base}/availability"
        self._published_path = Path(data_dir) / "mqtt_published.json"
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="certsync", clean_session=True
        )
        if settings.user:
            self.client.username_pw_set(settings.user, settings.password)
        if settings.tls:
            self.client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        self.client.will_set(self.avail, "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=5, max_delay=300)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self._config: Config | None = None
        self._snapshot: dict | None = None
        self.client.connect_async(settings.host, settings.port, keepalive=60)
        self.client.loop_start()

    # -- connection ----------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log.error("MQTT connection to %s:%s refused: %s", self.s.host, self.s.port, reason_code)
            return
        log.info("connected to MQTT broker %s:%s", self.s.host, self.s.port)
        client.publish(self.avail, "online", qos=1, retain=True)
        # (Re-)publish everything, e.g. after a broker restart
        if self._config is not None:
            self._publish_discovery(self._config)
        if self._snapshot is not None:
            self._publish_state(self._snapshot)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.warning("MQTT connection lost (%s), reconnecting", reason_code)

    def _pub(self, topic: str, payload: Any, retain: bool = True) -> None:
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False)
        self.client.publish(topic, payload, qos=1, retain=retain)

    # -- discovery -----------------------------------------------------------
    def _common(self) -> dict:
        return {
            "device": DEVICE,
            "availability_topic": self.avail,
            "origin": {"name": "certsync-ionos",
                       "support_url": "https://github.com/electron23/certbot-ionos"},
        }

    def _cert_configs(self, name: str) -> dict[str, dict]:
        slug = _slug(name)
        topic = f"{self.base}/cert/{slug}"
        out = {}
        for key, platform, suffix, extra in CERT_ENTITIES:
            uid = f"certsync_{slug}_{key}"
            cfg = {
                **self._common(),
                "name": f"{name} {suffix}",
                "unique_id": uid,
                "default_entity_id": f"{platform}.{uid}",
                "state_topic": topic,
                "value_template": "{{ value_json.%s | default(None) }}" % key,
                **extra,
            }
            if key == "status":
                cfg["json_attributes_topic"] = f"{topic}/attributes"
            out[f"{self.s.discovery_prefix}/{platform}/certsync/{uid}/config"] = cfg
        return out

    def _global_configs(self) -> dict[str, dict]:
        p = self.s.discovery_prefix
        return {
            f"{p}/binary_sensor/certsync/certsync_problem/config": {
                **self._common(),
                "name": "Problem",
                "unique_id": "certsync_problem",
                "default_entity_id": "binary_sensor.certsync_problem",
                "device_class": "problem",
                "state_topic": f"{self.base}/state",
                "value_template": "{{ 'ON' if value_json.problem else 'OFF' }}",
                "json_attributes_topic": f"{self.base}/problems",
            },
            f"{p}/sensor/certsync/certsync_last_check/config": {
                **self._common(),
                "name": "letzte Prüfung",
                "unique_id": "certsync_last_check",
                "default_entity_id": "sensor.certsync_last_check",
                "device_class": "timestamp",
                "entity_category": "diagnostic",
                "state_topic": f"{self.base}/state",
                "value_template": "{{ value_json.last_check | default(None) }}",
                "json_attributes_topic": f"{self.base}/info",
            },
            f"{p}/event/certsync/certsync_event/config": {
                **self._common(),
                "name": "Ereignis",
                "unique_id": "certsync_event",
                "default_entity_id": "event.certsync_event",
                "event_types": ["issued", "deployed"],
                "state_topic": f"{self.base}/event",
                "icon": "mdi:certificate-outline",
            },
        }

    def _publish_discovery(self, config: Config) -> None:
        wanted: dict[str, dict] = dict(self._global_configs())
        for cert in config.certs:
            wanted.update(self._cert_configs(cert.name))
        # Remove entities of certificates that are no longer configured
        try:
            previous = set(json.loads(self._published_path.read_text()))
        except (OSError, ValueError):
            previous = set()
        for topic in previous - set(wanted):
            self._pub(topic, "")
        for topic, cfg in wanted.items():
            self._pub(topic, cfg)
        try:
            self._published_path.write_text(json.dumps(sorted(wanted)))
        except OSError as exc:
            log.warning("cannot store published discovery topics: %s", exc)

    # -- state ---------------------------------------------------------------
    def _publish_state(self, snap: dict) -> None:
        self._pub(f"{self.base}/state", {"problem": snap["problem"], "last_check": snap["last_check"]})
        self._pub(f"{self.base}/problems", {"probleme": snap["problems"], "anzahl": len(snap["problems"])})
        self._pub(f"{self.base}/info", {"deploy_window": snap["deploy_window"],
                                        "zertifikate": sorted(snap["certs"])})
        for name, entry in snap["certs"].items():
            topic = f"{self.base}/cert/{_slug(name)}"
            self._pub(topic, {k: entry[k] for k in
                              ("valid_until", "days_left", "next_renewal", "issued", "status")})
            self._pub(f"{topic}/attributes", {
                "domains": entry["domains"],
                "ziele": entry["targets"],
                "fehler": entry["error"],
                "aussteller": entry["issuer"],
                "staging": entry["staging"],
            })

    # -- public API ----------------------------------------------------------
    def update(self, config: Config, snapshot: dict) -> None:
        discovery_changed = self._config is None or \
            [c.name for c in self._config.certs] != [c.name for c in config.certs]
        self._config, self._snapshot = config, snapshot
        if not self.client.is_connected():
            log.info("MQTT not connected yet, status is sent once connected")
            return
        if discovery_changed:
            self._publish_discovery(config)
        self._publish_state(snapshot)

    def event(self, payload: dict) -> None:
        # Events are not retained: Home Assistant must not replay them after a restart.
        if self.client.is_connected():
            self._pub(f"{self.base}/event", payload, retain=False)
        else:
            log.warning("MQTT not connected, event dropped: %s", payload.get("event_type"))

    def close(self) -> None:
        try:
            self.client.publish(self.avail, "offline", qos=1, retain=True).wait_for_publish(5)
        except Exception:  # noqa: BLE001 - best effort on shutdown
            pass
        self.client.loop_stop()
        self.client.disconnect()
