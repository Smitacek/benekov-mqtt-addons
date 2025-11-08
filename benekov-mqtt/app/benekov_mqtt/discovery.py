import hashlib
import json
import re
from typing import Dict, List, Optional


def slugify(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[^a-z0-9_]+", "_", t)
    t = re.sub(r"_+", "_", t).strip('_')
    return t or "item"


def unique_id(host: str, page: str, obj_id: str) -> str:
    base = f"{host}|{page}|{obj_id}"
    return hashlib.sha1(base.encode()).hexdigest()


def device_payload(host: str, name: Optional[str] = None) -> Dict:
    name = name or f"Benekov @ {host}"
    return {
        "identifiers": [f"benekov-{host}"],
        "name": name,
        "manufacturer": "Benekov/Siemens",
        "model": "Climatix HMI",
    }


def topics(base_topic: str, host: str, page: str, obj_id: str) -> Dict[str, str]:
    root = f"{base_topic}/{slugify(host)}/{page.replace('.cgi','')}/{obj_id}"
    return {
        "state": f"{root}/state",
        "command": f"{root}/set",
        "attr": f"{root}/attributes",
    }


def sensor_config(discovery_prefix: str, base_topic: str, host: str, page: str, obj_id: str,
                  name: str, unit: Optional[str] = None) -> (str, Dict):
    obj = slugify(name or obj_id)
    uid = unique_id(host, page, obj_id)
    t = topics(base_topic, host, page, obj_id)
    topic = f"{discovery_prefix}/sensor/benekov_{slugify(host)}/{obj}/config"
    payload = {
        "name": name or obj_id,
        "unique_id": uid,
        "state_topic": t["state"],
        "json_attributes_topic": t["attr"],
        "device": device_payload(host),
        "icon": "mdi:thermometer",
    }
    if unit:
        payload["unit_of_measurement"] = unit
    return topic, payload


def number_config(discovery_prefix: str, base_topic: str, host: str, page: str, obj_id: str,
                  name: str, unit: Optional[str], minimum: Optional[float], maximum: Optional[float], step: Optional[float]) -> (str, Dict):
    obj = slugify(name or obj_id)
    uid = unique_id(host, page, obj_id) + "-num"
    t = topics(base_topic, host, page, obj_id)
    topic = f"{discovery_prefix}/number/benekov_{slugify(host)}/{obj}/config"
    payload = {
        "name": name or obj_id,
        "unique_id": uid,
        "state_topic": t["state"],
        "command_topic": t["command"],
        "json_attributes_topic": t["attr"],
        "device": device_payload(host),
        "icon": "mdi:tune",
    }
    if unit:
        payload["unit_of_measurement"] = unit
    if minimum is not None:
        payload["min"] = minimum
    if maximum is not None:
        payload["max"] = maximum
    if step is not None:
        payload["step"] = step
    return topic, payload


def select_config(discovery_prefix: str, base_topic: str, host: str, page: str, obj_id: str,
                  name: str, options: List[str]) -> (str, Dict):
    obj = slugify(name or obj_id)
    uid = unique_id(host, page, obj_id) + "-sel"
    t = topics(base_topic, host, page, obj_id)
    topic = f"{discovery_prefix}/select/benekov_{slugify(host)}/{obj}/config"
    payload = {
        "name": name or obj_id,
        "unique_id": uid,
        "state_topic": t["state"],
        "command_topic": t["command"],
        "options": options,
        "json_attributes_topic": t["attr"],
        "device": device_payload(host),
        "icon": "mdi:menu",
    }
    return topic, payload

