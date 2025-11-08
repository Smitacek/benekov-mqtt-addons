# Benekov MQTT Bridge (Home Assistant Add-on)

Publishes Siemens Climatix HMI (Benekov) values to MQTT with Home Assistant Discovery and exposes writable items as number/select entities.

## Features
- Read-only polling via `HMIxxxxxRead.cgi` (HTTP Basic).
- Optional writes via `HMIinput.cgi` (only for items that expose `mi=...`).
- MQTT Discovery (sensor/number/select), grouped polling per page.
- Configurable pages to include, topics, and polling interval (>= 30s).

## Options (example)
```yaml
device_host: "http://192.168.50.94/"
username: WEB
password: "SBTAdmin!"
poll_interval: 30
base_topic: benekov
discovery_prefix: homeassistant
profile: monitor   # monitor|all; monitor = read-only + whitelist
include_pages:
  - HMI00001.cgi    # výkon/teploty/palivo/stav
  - HMI65000.cgi    # alarmy
  - HMI00033.cgi    # podávání/ventilátor
mqtt:
  host: core-mosquitto
  port: 1883
  username:
  password:
```

### Monitor profile (default)
- Publikuje pouze provozní metriky (read-only):
  - Aktuální výkon (%), B2/B7/B8 teploty (°C), Stav kotle, Palivo
  - Alarmy (aktivní), Alarmy (historie), Alarm ID
  - Čas podávání (s), Výkon ventilátoru (%), Doběh ventilátoru (s)
- Nezakládá ovládací `number/select` entity.

### All profile
- Publikuje všechny položky a založí ovládací `number/select` (kde je `mi`).

## Topics
- State: `benekov/<host>/<page>/<oNNN>/state`
- Command: `benekov/<host>/<page>/<oNNN>/set`
- Attributes: `benekov/<host>/<page>/<oNNN>/attributes`

## Notes
- Keep polling reasonable (>= 30s) to avoid stressing the embedded HMI.
- Only items with `mi` are published as writable (number/select). Writes map select payloads either by index or by exact label.

## Local build (dev)
```
docker build -t benekov-mqtt addon/benekov-mqtt
```

## Home Assistant (Supervisor)
- Add this repo as a local add-on repository or copy `addon/benekov-mqtt` under your local add-ons share.
- Install "Benekov MQTT Bridge", configure options, start.
