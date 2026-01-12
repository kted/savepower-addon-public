# SavePower Tuya Thermostat Bridge

This add-on bridges Tuya thermostats integrated in Home Assistant to the SavePower platform using the WT50 MQTT protocol.

## Features

- Publishes thermostat telemetry every 60 seconds (configurable)
- Publishes full device state every 600 seconds (configurable)
- Handles all WT50 protocol commands with acknowledgements
- Automatically detects available thermostat features
- Supports multiple thermostat models with different capabilities

## Configuration

### Basic Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `mqtt_host` | `core-mosquitto` | MQTT broker hostname |
| `mqtt_port` | `1883` | MQTT broker port |
| `mqtt_username` | (empty) | MQTT username (optional) |
| `mqtt_password` | (empty) | MQTT password (optional) |
| `organization_id` | `default` | SavePower organization ID |
| `publish_interval` | `60` | Seconds between telemetry updates |
| `telemetry_interval` | `600` | Seconds between full state updates |
| `log_level` | `info` | Logging level (debug/info/warning/error) |

### Device Mapping

The Tuya integration may create entities with different naming patterns. For example:
- Climate entity: `climate.1l_pef`
- Related entities: `switch.thermo_1l_p_child_lock`

Use `device_mapping` to map climate entity IDs to their related entity prefixes:

```yaml
device_mapping:
  - climate_id: "1l_pef"
    entity_prefix: "thermo_1l_p"
  - climate_id: "2p_1_1_pef"
    entity_prefix: "thermo_2p_1_1_p"
```

**Finding your entity names:**
1. Go to **Settings → Devices & Services → Tuya**
2. Click on your thermostat device
3. Note the entity IDs (e.g., `climate.xxx`, `switch.xxx_child_lock`)
4. The climate ID is the part after `climate.`
5. The entity prefix is the common part before `_child_lock`, `_frost_protection`, etc.

## MQTT Topics

| Topic | Direction | Description |
|-------|-----------|-------------|
| `savepower/{org}/thermostats/status` | → Server | Telemetry and state updates |
| `savepower/{org}/thermostats/control` | ← Server | Commands from SavePower |

## Supported Features

Features are automatically detected per device. Not all thermostats support all features.

| Feature | Command | Description |
|---------|---------|-------------|
| Temperature | `temperature` | Set target temperature |
| Power | `mode` | `heat_cool` or `off` |
| Thermostat Mode | `thermostat_mode` | `auto`, `manual`, `home`, `away` |
| Child Lock | `lock` | Enable/disable child lock |
| Frost Protection | `anti_frost` | Enable/disable anti-frost |
| Max Temperature | `max_temp` | Upper temperature limit |
| Min Temperature | `min_temp` | Lower temperature limit |
| Calibration | `calibration` | Temperature correction offset |
| Sensor | `sensor` | Sensor selection (`in`/`out`) |
| Sound | `sound` | Enable/disable button sounds |
| Request State | `command: "request_state"` | Request full state dump |

## Example Messages

### Telemetry (every 60 seconds)

```json
{
  "device_id": "thermo_1l_p",
  "status": "heating",
  "target_temp": 22.0,
  "current_temp": 19.5,
  "mode": "auto"
}
```

### Full State (every 600 seconds or on request)

```json
{
  "device_id": "thermo_1l_p",
  "friendly_name": "Living Room Thermostat",
  "status": "heating",
  "target_temp": 22.0,
  "current_temp": 19.5,
  "mode": "auto",
  "lock": false,
  "anti_frost": false,
  "max_temp": 35,
  "calibration": -1.0
}
```

### Command Acknowledgement

```json
{
  "device_id": "thermo_1l_p",
  "ack": "ok",
  "msg": "temperature=22.0, mode=heat_cool"
}
```

## Troubleshooting

### No thermostats detected

- Ensure your Tuya thermostats are properly set up in Home Assistant
- Check that the Tuya integration is connected and devices are online
- Look at the add-on logs for connection errors

### Commands not working

- Verify the device mapping is correct
- Check the add-on logs for error messages
- Some features may not be available on your thermostat model

### MQTT connection failed

- If using the Mosquitto add-on, use `core-mosquitto` as the host
- Check username/password if authentication is enabled
- Verify the MQTT broker is running

## Changelog

### 1.0.0
- Initial release
- Support for Tuya thermostats via WT50 protocol
- Automatic feature detection
- Configurable intervals and device mapping
