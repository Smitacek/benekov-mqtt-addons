import json
import os
import signal
import sys
import threading
import time
from typing import Dict, List

import paho.mqtt.client as mqtt

from .api import HMIClient, build_languages, parse_page, read_values, read_ids, write_value
from .discovery import sensor_config, number_config, select_config, topics, slugify


def get_env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v in (None, "", "null", "None") else v


def _load_options_json():
    try:
        with open('/data/options.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


class BenekovMQTT:
    def __init__(self):
        opts = _load_options_json() or {}

        def opt(path, default=""):
            cur = opts
            for part in path.split('.'):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    return default
            return cur

        self.base_url = str(opt('device_host', get_env("HMI_BASE_URL", "")))
        self.username = str(opt('username', get_env("HMI_USER", "")))
        self.password = str(opt('password', get_env("HMI_PASS", "")))
        self.poll_interval = int(opt('poll_interval', get_env("POLL_INTERVAL", "30") or 30))
        if self.poll_interval < 30:
            self.poll_interval = 30
        self.discovery_prefix = str(opt('discovery_prefix', get_env("DISCOVERY_PREFIX", "homeassistant")))
        self.base_topic = str(opt('base_topic', get_env("BASE_TOPIC", "benekov")))

        # include_pages preferred from options.json (YAML list)
        ip = opt('include_pages', None)
        if isinstance(ip, list):
            self.include_pages = [str(p) for p in ip if p]
        else:
            self.include_pages = [p for p in get_env("INCLUDE_PAGES", "").split(" ") if p]
        if not self.include_pages or self.include_pages == ["HMI00001.cgi"]:
            # Default monitoring pages
            self.include_pages = [
                "HMI00001.cgi",   # home: výkon, teploty, palivo, stav
                "HMI65000.cgi",   # alarmy
                "HMI00033.cgi",   # podávání/ventilátor
            ]

        self.mqtt_host = str(opt('mqtt.host', get_env("MQTT_HOST", "core-mosquitto")))
        self.mqtt_port = int(opt('mqtt.port', get_env("MQTT_PORT", "1883") or 1883))
        self.mqtt_user = str(opt('mqtt.username', get_env("MQTT_USER", "")))
        self.mqtt_pass = str(opt('mqtt.password', get_env("MQTT_PASS", "")))

        # Profile control: monitor (read-only, whitelist) vs all
        self.profile = str(opt('profile', 'monitor')).lower() or 'monitor'
        self.read_only = (self.profile != 'all')
        # Whitelist for monitor profile with friendly names/units
        self.monitor_whitelist = {
            # HMI00001
            ("HMI00001.cgi", "o044"): {"label": "Aktuální výkon", "unit": "%"},
            ("HMI00001.cgi", "o075"): {"label": "B2 Teplota kotle", "unit": "°C"},
            ("HMI00001.cgi", "o082"): {"label": "B7 Teplota zpátečky", "unit": "°C"},
            ("HMI00001.cgi", "o089"): {"label": "B8 Teplota spalin", "unit": "°C"},
            ("HMI00001.cgi", "o038"): {"label": "Stav kotle"},
            ("HMI00001.cgi", "o148"): {"label": "Palivo"},
            # HMI65000 (alarmy)
            ("HMI65000.cgi", "o011"): {"label": "Alarmy aktivní"},
            ("HMI65000.cgi", "o018"): {"label": "Alarmy historie"},
            ("HMI65000.cgi", "o025"): {"label": "Alarm ID"},
            # HMI00033 (podávání/ventilátor)
            ("HMI00033.cgi", "o010"): {"label": "Čas podávání", "unit": "s"},
            ("HMI00033.cgi", "o020"): {"label": "Výkon ventilátoru", "unit": "%"},
        }

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
                # Filter entries to only those that appear in Read.cgi or are explicit writable
                try:
                    idset = read_ids(self.client, pg['read'])
                except Exception:
                    idset = set()
                filtered = []
                for ent in pg['entries']:
                    ok = (ent['id'] in idset or ent.get('it') in ('v', 'e'))
                    if not ok:
                        continue
                    # Apply monitor whitelist if enabled
                    if self.read_only:
                        key = (p, ent['id'])
                        if key not in self.monitor_whitelist:
                            continue
                        # Override friendly label/unit if provided
                        meta = self.monitor_whitelist[key]
                        if meta.get('label'):
                            ent['label'] = meta['label']
                        if meta.get('unit'):
                            ent['unit'] = meta['unit']
                    filtered.append(ent)
                pg['entries'] = filtered
                # Ensure mandatory monitor entries for home page even if not parsed
                if self.read_only and p == "HMI00001.cgi":
                    need = [
                        ("o044", {"label": "Aktuální výkon", "unit": "%", "it": "v"}),
                        ("o075", {"label": "B2 Teplota kotle", "unit": "°C", "it": "v"}),
                        ("o082", {"label": "B7 Teplota zpátečky", "unit": "°C", "it": "v"}),
                        ("o089", {"label": "B8 Teplota spalin", "unit": "°C", "it": "v"}),
                        ("o038", {"label": "Stav kotle", "it": "e", "enum_lg": "2. 512"}),
                        ("o148", {"label": "Palivo", "it": "e"}),
                    ]
                    present_ids = {e['id'] for e in pg['entries']}
                    for oid, meta in need:
                        if oid in present_ids:
                            continue
                        ent = {
                            'page': p,
                            'id': oid,
                            'label': meta.get('label', oid),
                            'unit': meta.get('unit'),
                            'it': meta.get('it', 'v'),
                            'mi': None,
                            'enum': None,
                        }
                        # Attach enum from languages if requested
                        lgk = meta.get('enum_lg')
                        if lgk and lgk in self.languages:
                            ent['enum'] = self.languages[lgk]
                        pg['entries'].append(ent)
                self.pages[p] = pg
                self.log(f"parsed {p}: {pg['title']!r}, entries={len(pg['entries'])}, read={pg['read']}, html_len={pg.get('html_len')}, read_ids={len(idset)}")
                # If still no entries but read endpoint has ids, generate generic entries
                if not pg['entries'] and idset:
                    gen = []
                    for oid in sorted(idset):
                        # Honor monitor whitelist if enabled
                        if self.read_only:
                            key = (p, oid)
                            if key not in self.monitor_whitelist:
                                continue
                            meta = self.monitor_whitelist[key]
                            gen.append({'page': p, 'id': oid, 'label': meta.get('label', oid), 'unit': meta.get('unit'), 'it': 'v', 'mi': None, 'enum': None})
                        else:
                            gen.append({'page': p, 'id': oid, 'label': oid, 'unit': None, 'it': 'v', 'mi': None, 'enum': None})
                    pg['entries'] = gen
                    self.pages[p] = pg
                    self.log(f"generated {len(gen)} generic entries for {p} from {pg['read']}")
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
                if (it == 'v' and mi) and not self.read_only:
                    # Optional limits not parsed here; could be enhanced
                    t2, p2 = number_config(self.discovery_prefix, self.base_topic, self.base_url, page, ent['id'], label, unit, None, None, None)
                    self.mqtt.publish(t2, json.dumps(p2), retain=True)
                    # subscribe command
                    self.mqtt.subscribe(topics(self.base_topic, self.base_url, page, ent['id'])["command"])

                # If enum -> select entity with options if present and writable
                if (it == 'e' and enum and mi) and not self.read_only:
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
                    # Keep trying next loop, but log once per cycle for visibility
                    self.log(f"no value for {ent['id']} on {read_ep}")
                    continue
                typ, val = typval
                # Map enums to text option if available
                state_payload = val
                if ent.get('it') == 'e' and ent.get('enum'):
                    try:
                        idx = int(val)
                        opts = ent['enum']
                        if 0 <= idx < len(opts):
                            state_payload = opts[idx]
                    except Exception:
                        pass
                # Publish state
                t = topics(self.base_topic, self.base_url, ent['page'], ent['id'])
                self.mqtt.publish(t['state'], state_payload, retain=True)
                # Attributes
                attrs = {
                    'page': ent['page'],
                    'label': ent['label'],
                    'unit': ent['unit'],
                    'type': ent['it'],
                }
                if ent.get('enum'):
                    attrs['options'] = ent['enum']
                    # Keep numeric index alongside, if numeric
                    try:
                        attrs['index'] = int(val)
                    except Exception:
                        pass
                self.mqtt.publish(t['attr'], json.dumps(attrs), retain=True)

    def on_message(self, client, userdata, msg):
        # Command handler for number/select writes
        if self.read_only:
            # Ignore commands in monitor profile
            return
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
        self.log(f"building pages for {len(self.include_pages)} pages: {', '.join(self.include_pages)}")
        self.publish_discovery()
        self.log("published discovery")
        # Initial state
        self.push_state()
        self.log("initial state published")

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
