import json
import os
import signal
import sys
import threading
import time
from typing import Dict, List

import paho.mqtt.client as mqtt

from .api import HMIClient, build_languages, parse_page, read_values, write_value
from .discovery import sensor_config, number_config, select_config, topics, slugify


def get_env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v in (None, "", "null", "None") else v


class BenekovMQTT:
    def __init__(self):
        self.base_url = get_env("HMI_BASE_URL")
        self.username = get_env("HMI_USER")
        self.password = get_env("HMI_PASS")
        self.poll_interval = int(get_env("POLL_INTERVAL", "30"))
        if self.poll_interval < 30:
            self.poll_interval = 30
        self.discovery_prefix = get_env("DISCOVERY_PREFIX", "homeassistant")
        self.base_topic = get_env("BASE_TOPIC", "benekov")
        # include pages space separated
        self.include_pages: List[str] = [p for p in get_env("INCLUDE_PAGES", "").split(" ") if p]

        self.mqtt_host = get_env("MQTT_HOST", "core-mosquitto")
        self.mqtt_port = int(get_env("MQTT_PORT", "1883"))
        self.mqtt_user = get_env("MQTT_USER", "")
        self.mqtt_pass = get_env("MQTT_PASS", "")

        if not self.base_url:
            print("HMI_BASE_URL not set", file=sys.stderr)
            sys.exit(2)

        self.client = HMIClient(self.base_url, self.username, self.password)
        self.languages = build_languages(self.client)
        self.pages: Dict[str, Dict] = {}
        self.entities: Dict[str, Dict] = {}  # key -> entity def

        self.mqtt = mqtt.Client()
        if self.mqtt_user:
            self.mqtt.username_pw_set(self.mqtt_user, self.mqtt_pass or None)
        self.mqtt.on_message = self.on_message
        self.running = True

    def log(self, *args):
        print("[benekov]", *args, file=sys.stdout, flush=True)

    def connect_mqtt(self):
        self.mqtt.will_set(f"{self.base_topic}/{slugify(self.base_url)}/status", payload="offline", retain=True)
        self.mqtt.connect(self.mqtt_host, self.mqtt_port, keepalive=30)
        self.mqtt.loop_start()
        self.mqtt.publish(f"{self.base_topic}/{slugify(self.base_url)}/status", "online", retain=True)

    def build_pages(self):
        pages = self.include_pages or ["HMI00001.cgi"]
        for p in pages:
            try:
                pg = parse_page(self.client, self.languages, p)
                self.pages[p] = pg
            except Exception as e:
                self.log(f"parse_page failed for {p}: {e}")

    def publish_discovery(self):
        for page, pg in self.pages.items():
            for ent in pg['entries']:
                ent_id = f"{page}|{ent['id']}"
                label = ent.get('label') or ent['id']
                unit = ent.get('unit')
                it = ent.get('it')
                mi = ent.get('mi')
                enum = ent.get('enum')

                # Keep a primary sensor for all entries
                topic, payload = sensor_config(self.discovery_prefix, self.base_topic, self.base_url, page, ent['id'], label, unit)
                self.mqtt.publish(topic, json.dumps(payload), retain=True)

                # If writable numeric -> number entity
                if it == 'v' and mi:
                    # Optional limits not parsed here; could be enhanced
                    t2, p2 = number_config(self.discovery_prefix, self.base_topic, self.base_url, page, ent['id'], label, unit, None, None, None)
                    self.mqtt.publish(t2, json.dumps(p2), retain=True)
                    # subscribe command
                    self.mqtt.subscribe(topics(self.base_topic, self.base_url, page, ent['id'])["command"])

                # If enum -> select entity with options if present and writable
                if it == 'e' and enum and mi:
                    t3, p3 = select_config(self.discovery_prefix, self.base_topic, self.base_url, page, ent['id'], label, enum)
                    self.mqtt.publish(t3, json.dumps(p3), retain=True)
                    self.mqtt.subscribe(topics(self.base_topic, self.base_url, page, ent['id'])["command"])

                self.entities[ent_id] = {
                    'page': page, 'read': pg['read'], 'id': ent['id'], 'label': label, 'unit': unit,
                    'it': it, 'mi': mi, 'enum': enum,
                }

    def push_state(self):
        # Read and publish
        # Group by read endpoint to minimize requests
        by_read: Dict[str, List[Dict]] = {}
        for ent in self.entities.values():
            by_read.setdefault(ent['read'], []).append(ent)
        for read_ep, ents in by_read.items():
            try:
                vals = read_values(self.client, read_ep)
            except Exception as e:
                self.log(f"read failed {read_ep}: {e}")
                continue
            for ent in ents:
                typval = vals.get(ent['id'])
                if not typval:
                    continue
                typ, val = typval
                # Publish raw state; HA templates/entities handle types
                t = topics(self.base_topic, self.base_url, ent['page'], ent['id'])
                self.mqtt.publish(t['state'], val, retain=True)
                # Attributes
                attrs = {
                    'page': ent['page'],
                    'label': ent['label'],
                    'unit': ent['unit'],
                    'type': ent['it'],
                }
                if ent.get('enum'):
                    attrs['options'] = ent['enum']
                self.mqtt.publish(t['attr'], json.dumps(attrs), retain=True)

    def on_message(self, client, userdata, msg):
        # Command handler for number/select writes
        topic = msg.topic
        payload = msg.payload.decode('utf-8').strip()
        for key, ent in self.entities.items():
            t = topics(self.base_topic, self.base_url, ent['page'], ent['id'])
            if topic == t['command']:
                mi = ent.get('mi')
                if not mi:
                    self.log(f"No write MI for {ent['id']}")
                    return
                write_ok = False
                if ent['it'] == 'v':
                    # Numeric value passthrough
                    write_ok = write_value(self.client, mi, payload)
                elif ent['it'] == 'e':
                    # Map label to index if needed
                    if ent.get('enum'):
                        opts = ent['enum']
                        try:
                            # Accept either numeric index or label
                            if payload.isdigit():
                                idx = int(payload)
                            else:
                                idx = opts.index(payload)
                            write_ok = write_value(self.client, mi, str(idx))
                        except Exception:
                            write_ok = False
                    else:
                        # If no enum list, try raw payload
                        write_ok = write_value(self.client, mi, payload)
                # Refresh state soon after write
                if write_ok:
                    self.log(f"Write OK {ent['id']} <- {payload}")
                    time.sleep(0.5)
                    self.push_state()
                else:
                    self.log(f"Write FAILED {ent['id']} <- {payload}")
                break

    def run(self):
        self.connect_mqtt()
        self.build_pages()
        self.publish_discovery()
        # Initial state
        self.push_state()

        def loop():
            while self.running:
                try:
                    self.push_state()
                except Exception as e:
                    self.log(f"poll error: {e}")
                time.sleep(self.poll_interval)

        t = threading.Thread(target=loop, daemon=True)
        t.start()

        def handle_stop(signum, frame):
            self.running = False
            self.mqtt.publish(f"{self.base_topic}/{slugify(self.base_url)}/status", "offline", retain=True)
            self.mqtt.loop_stop()
            self.mqtt.disconnect()
            sys.exit(0)

        signal.signal(signal.SIGTERM, handle_stop)
        signal.signal(signal.SIGINT, handle_stop)

        # Keep main thread alive
        while self.running:
            time.sleep(1)


if __name__ == "__main__":
    BenekovMQTT().run()
