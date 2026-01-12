"""
Home Assistant to SavePower MQTT Bridge for Tuya Thermostats

This script bridges Tuya thermostats integrated in Home Assistant to the
SavePower platform using the WT50 MQTT protocol.

Compatible with: WT50 Protocol v1.0.0 (firmware 2.6.0+)
Tested with: Tuya HA Integration (tuya-device-sharing-sdk)

Features:
- Publishes telemetry every 60 seconds
- Publishes full state every 600 seconds or on request
- Handles all WT50 commands with acknowledgements
- Maps HA HVAC modes to WT50 protocol modes
- Supports Tuya thermostats with auto/manual mode

When running as HA Add-on:
- Configuration is read from environment variables
- Uses Supervisor API token for authentication
- Device mapping configured via add-on options
"""

import asyncio
import json
import logging
import os
import time
from typing import Any

import aiohttp
import paho.mqtt.client as mqtt
import requests
import websockets

# ---------------- Configuration ----------------

def load_config():
    """Load configuration from environment variables (add-on) or defaults."""
    config = {
        # Home Assistant connection
        "ha_url": os.environ.get("HA_URL", "http://supervisor/core"),
        "ha_token": os.environ.get("SUPERVISOR_TOKEN", os.environ.get("HA_TOKEN", "")),

        # MQTT connection
        "mqtt_host": os.environ.get("MQTT_HOST", "core-mosquitto"),
        "mqtt_port": int(os.environ.get("MQTT_PORT", "1883")),
        "mqtt_username": os.environ.get("MQTT_USERNAME", ""),
        "mqtt_password": os.environ.get("MQTT_PASSWORD", ""),

        # SavePower settings
        "org_id": os.environ.get("ORG_ID", "default"),
        "publish_interval": int(os.environ.get("PUBLISH_INTERVAL", "60")),
        "telemetry_interval": int(os.environ.get("TELEMETRY_INTERVAL", "600")),

        # Logging
        "log_level": os.environ.get("LOG_LEVEL", "info").upper(),

        # Device mapping
        "device_mapping": {},
    }

    # Parse device mapping from JSON environment variable
    device_mapping_json = os.environ.get("DEVICE_MAPPING", "[]")
    try:
        mapping_list = json.loads(device_mapping_json)
        for item in mapping_list:
            if isinstance(item, dict) and "climate_id" in item and "entity_prefix" in item:
                config["device_mapping"][item["climate_id"]] = item["entity_prefix"]
    except json.JSONDecodeError:
        pass

    return config


# Load configuration
CONFIG = load_config()

HA_URL = CONFIG["ha_url"]
HA_TOKEN = CONFIG["ha_token"]
ORG_ID = CONFIG["org_id"]
MQTT_HOST = CONFIG["mqtt_host"]
MQTT_PORT = CONFIG["mqtt_port"]
MQTT_USERNAME = CONFIG["mqtt_username"]
MQTT_PASSWORD = CONFIG["mqtt_password"]

HEADERS = {"Authorization": f"Bearer {HA_TOKEN}"}

STATUS_TOPIC = f"savepower/{ORG_ID}/thermostats/status"
CONTROL_TOPIC = f"savepower/{ORG_ID}/thermostats/control"

PUBLISH_INTERVAL = CONFIG["publish_interval"]
TELEMETRY_INTERVAL = CONFIG["telemetry_interval"]

# Device mapping from configuration
DEVICE_MAPPING: dict[str, str] = CONFIG["device_mapping"]

# ---------------- Logging ----------------

logging.basicConfig(
    level=getattr(logging, CONFIG["log_level"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------- State ----------------

devices: dict[str, dict[str, Any]] = {}
last_publish: dict[str, float] = {}
last_telemetry: dict[str, float] = {}

# Command queue for thread-safe MQTT -> async bridge
command_queue: asyncio.Queue = None

# ---------------- Mode Mappings ----------------

# HA HVAC mode -> WT50 power mode (for switch on/off)
HA_HVAC_TO_WT50_POWER = {
    "heat": "heat_cool",
    "cool": "heat_cool",
    "heat_cool": "heat_cool",
    "auto": "heat_cool",
    "off": "off",
}

# WT50 power mode -> HA HVAC mode
WT50_POWER_TO_HA_HVAC = {
    "heat_cool": "heat_cool",
    "off": "off",
}

# Tuya thermostat mode -> WT50 thermostat_mode
# Tuya devices may report: auto, manual, home, away, program, etc.
TUYA_MODE_TO_WT50 = {
    "auto": "auto",      # Schedule/program mode
    "manual": "manual",  # Manual temperature control
    "home": "home",      # Home mode (some devices)
    "away": "away",      # Away mode (some devices)
    "program": "auto",   # Alternative name for schedule mode
}

# WT50 thermostat_mode -> Tuya mode
# Maps WT50 commands to Tuya-compatible values
WT50_TO_TUYA_MODE = {
    "auto": "auto",
    "manual": "manual",
    "home": "home",
    "away": "away",
}

# ---------------- MQTT ----------------

mqttc = mqtt.Client()


def on_mqtt_connect(client, userdata, flags, rc):
    """Handle MQTT connection."""
    if rc == 0:
        logger.info("Connected to MQTT broker")
        client.subscribe(CONTROL_TOPIC)
        logger.info(f"Subscribed to {CONTROL_TOPIC}")
    else:
        logger.error(f"MQTT connection failed with code {rc}")


def on_mqtt_message(client, userdata, msg):
    """Handle incoming MQTT commands."""
    try:
        cmd = json.loads(msg.payload)
        device_id = cmd.get("device_id")
        if not device_id:
            logger.warning("Received command without device_id")
            return
        # Put command in queue for async processing
        if command_queue:
            command_queue.put_nowait((device_id, cmd))
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse MQTT message: {e}")
    except Exception as e:
        logger.error(f"Error processing command: {e}")


def setup_mqtt():
    """Configure and connect to MQTT broker."""
    mqttc.on_connect = on_mqtt_connect
    mqttc.on_message = on_mqtt_message

    # Set credentials if provided
    if MQTT_USERNAME and MQTT_PASSWORD:
        mqttc.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        logger.info(f"Using MQTT authentication as user: {MQTT_USERNAME}")

    try:
        mqttc.connect(MQTT_HOST, MQTT_PORT, 60)
        mqttc.loop_start()
        logger.info(f"Connecting to MQTT broker at {MQTT_HOST}:{MQTT_PORT}")
    except Exception as e:
        logger.error(f"Failed to connect to MQTT broker: {e}")
        raise

# ---------------- HA helpers ----------------


def ha_get(entity_id: str) -> dict | None:
    """Get entity state from Home Assistant."""
    try:
        r = requests.get(
            f"{HA_URL}/api/states/{entity_id}",
            headers=HEADERS,
            timeout=2,
        )
        return r.json() if r.ok else None
    except requests.RequestException as e:
        logger.debug(f"Failed to get {entity_id}: {e}")
        return None


def ha_call(service: str, entity_id: str, data: dict | None = None) -> bool:
    """Call a Home Assistant service."""
    try:
        domain, svc = service.split(".")
        r = requests.post(
            f"{HA_URL}/api/services/{domain}/{svc}",
            headers=HEADERS,
            json={"entity_id": entity_id, **(data or {})},
            timeout=5,
        )
        return r.ok
    except requests.RequestException as e:
        logger.error(f"Failed to call {service} on {entity_id}: {e}")
        return False


# ---------------- Entity Resolution ----------------


def get_entity_prefix(climate_id: str) -> str:
    """
    Get the entity prefix for related entities.

    Tuya integration may use different naming for climate vs other entities.
    Example: climate.1l_pef but switch.thermo_1l_p_child_lock

    Uses DEVICE_MAPPING if configured, otherwise returns climate_id as-is.
    """
    return DEVICE_MAPPING.get(climate_id, climate_id)


def get_climate_entity(device_id: str) -> str:
    """Get the climate entity ID for a device."""
    # Check if device_id is a mapped prefix (reverse lookup)
    for climate_id, prefix in DEVICE_MAPPING.items():
        if prefix == device_id:
            return f"climate.{climate_id}"
    return f"climate.{device_id}"


# ---------------- Device State Helpers ----------------

# Cache for non-existent entities to avoid repeated checks
entity_cache: dict[str, bool] = {}  # entity_id -> exists


async def fetch_entities_parallel(entity_prefix: str) -> dict[str, Any]:
    """
    Fetch all Tuya thermostat entities in parallel using aiohttp.
    Returns dict with entity data.
    """
    async with aiohttp.ClientSession() as session:
        # Define entities to check
        entity_checks = [
            (f"binary_sensor.{entity_prefix}_valve", "valve"),
            (f"switch.{entity_prefix}_child_lock", "lock"),
            (f"switch.{entity_prefix}_frost_protection", "frost"),
            (f"number.{entity_prefix}_temperature_correction", "calibration"),
        ]
        
        # Filter cached non-existent entities
        to_fetch = [(eid, key) for eid, key in entity_checks if entity_cache.get(eid, True)]
        
        # Fetch in parallel
        async def fetch_one(eid):
            try:
                async with session.get(
                    f"{HA_URL}/api/states/{eid}",
                    headers=HEADERS,
                    timeout=aiohttp.ClientTimeout(total=1)
                ) as resp:
                    return await resp.json() if resp.ok else None
            except:
                return None
        
        tasks = [fetch_one(eid) for eid, _ in to_fetch]
        results = await asyncio.gather(*tasks)
        
        # Build result dict and update cache
        entity_data = {}
        for (eid, key), result in zip(to_fetch, results):
            entity_data[key] = result
            entity_cache[eid] = (result is not None)
        
        return entity_data


def get_device_data_from_climate(climate: dict, entity_data: dict = None) -> dict[str, Any]:
    """
    Extract device data from climate entity and related entities.
    """
    attrs = climate.get("attributes", {})
    entity_data = entity_data or {}
    
    data = {
        "climate": climate,
        "state": climate.get("state", "off"),
        "temperature": attrs.get("temperature"),
        "current_temperature": attrs.get("current_temperature"),
        "min_temp": attrs.get("min_temp"),
        "max_temp": attrs.get("max_temp"),
        "friendly_name": attrs.get("friendly_name", ""),
    }
    
    # Add entity data if available
    valve = entity_data.get("valve")
    if valve:
        data["valve"] = valve.get("state") == "on"
    
    lock = entity_data.get("lock")
    if lock:
        data["lock"] = lock.get("state") == "on"
    
    frost = entity_data.get("frost")
    if frost:
        data["frost"] = frost.get("state") == "on"
    
    cal = entity_data.get("calibration")
    if cal and cal.get("state") not in ("unavailable", "unknown"):
        try:
            data["calibration"] = float(cal["state"])
        except (ValueError, TypeError):
            pass
    
    return data


# ---------------- Status logic ----------------


def derive_status(climate_state: str, current_temp: float | None, target_temp: float | None) -> str:
    """
    Derive heating status from climate state and temperatures.

    Status values per WT50 protocol:
    - "off": Thermostat is powered off
    - "idle": Thermostat on but not heating (target reached)
    - "heating": Actively heating

    Since this Tuya integration doesn't expose valve state,
    we infer from temperature difference.
    """
    if climate_state == "off":
        return "off"

    if current_temp is None or target_temp is None:
        return "idle"

    # Infer heating if current temp is below target by 0.5°C
    return "heating" if current_temp < target_temp - 0.5 else "idle"


def get_thermostat_mode(climate: dict) -> str:
    """
    Get WT50 thermostat_mode from climate entity.

    Since this Tuya integration doesn't expose mode selection,
    we always return 'manual'.
    """
    # Check for preset_mode as fallback (some integrations use this)
    attrs = climate.get("attributes", {})
    preset = attrs.get("preset_mode")
    if preset:
        return TUYA_MODE_TO_WT50.get(preset, "manual")

    return "manual"  # Default


# ---------------- Publish ----------------


def publish_telemetry(device_id: str) -> None:
    """Publish basic telemetry (every 60 seconds)."""
    dev = devices.get(device_id)
    if not dev:
        return

    climate_state = dev.get("state", "off")
    current_temp = dev.get("current_temperature")
    target_temp = dev.get("temperature")
    
    status = derive_status(climate_state, current_temp, target_temp)
    wt50_mode = get_thermostat_mode(dev.get("climate", {}))

    telemetry = {
        "device_id": device_id,
        "status": status,
        "temperature": target_temp,
        "current_temperature": current_temp,
        "mode": wt50_mode,
    }

    mqttc.publish(STATUS_TOPIC, json.dumps(telemetry), qos=1)
    logger.debug(f"Published telemetry for {device_id}: {telemetry}")


async def publish_full_state(device_id: str) -> None:
    """Publish full device state (on request_state or every 600 seconds)."""
    # Refresh climate entity
    climate_entity = get_climate_entity(device_id)
    climate = ha_get(climate_entity)
    if not climate:
        logger.warning(f"Cannot publish full state for unknown device {device_id}")
        return

    # Fetch related entities in parallel
    entity_prefix = get_entity_prefix(device_id)
    entity_data = await fetch_entities_parallel(entity_prefix)
    
    # Extract and cache data
    dev = get_device_data_from_climate(climate, entity_data)
    devices[device_id] = dev

    climate_state = dev.get("state", "off")
    current_temp = dev.get("current_temperature")
    target_temp = dev.get("temperature")
    
    status = derive_status(climate_state, current_temp, target_temp)
    wt50_mode = get_thermostat_mode(climate)

    full_state = {
        "device_id": device_id,
        "friendly_name": dev.get("friendly_name", device_id),
        "status": status,
        "temperature": target_temp,
        "current_temperature": current_temp,
        "mode": wt50_mode,
    }

    # Include optional fields if available
    if dev.get("min_temp") is not None:
        full_state["min_temp"] = dev["min_temp"]
    
    if dev.get("max_temp") is not None:
        full_state["max_temp"] = dev["max_temp"]
    
    if dev.get("lock") is not None:
        full_state["lock"] = dev["lock"]
    
    if dev.get("frost") is not None:
        full_state["anti_frost"] = dev["frost"]
    
    if dev.get("calibration") is not None:
        full_state["calibration"] = dev["calibration"]
    
    # Note: Schedules are not typically exposed by Tuya HA integration
    # They must be managed through the Tuya app

    mqttc.publish(STATUS_TOPIC, json.dumps(full_state), qos=1)
    logger.info(f"Published full state for {device_id}: {full_state}")


# ---------------- Command Acknowledgement ----------------


def send_ack(device_id: str, success: bool, msg: str) -> None:
    """Send command acknowledgement per WT50 protocol."""
    ack = {
        "device_id": device_id,
        "ack": "ok" if success else "fail",
        "msg": msg,
    }
    mqttc.publish(STATUS_TOPIC, json.dumps(ack), qos=1)
    logger.info(f"Sent ack for {device_id}: {ack['ack']} - {msg}")


# ---------------- Commands ----------------


async def apply_command(device_id: str, cmd: dict) -> None:
    """Apply a command from the SavePower platform."""
    climate_entity = get_climate_entity(device_id)
    entity_prefix = get_entity_prefix(device_id)
    executed = []
    failed = []

    # Handle request_state command
    if cmd.get("command") == "request_state":
        await publish_full_state(device_id)
        send_ack(device_id, True, "request_state")
        return

    # Handle restart command (if possible via HA)
    if cmd.get("command") == "restart":
        # Most HA integrations don't support device restart
        send_ack(device_id, False, "restart not supported via HA")
        return

    # Set temperature
    if "temperature" in cmd:
        temp = cmd["temperature"]
        if ha_call("climate.set_temperature", climate_entity, {"temperature": temp}):
            executed.append(f"temperature={temp}")
        else:
            failed.append("temperature")

    # Set power mode (heat_cool/off) - controls HVAC on/off
    if "mode" in cmd:
        wt50_mode = cmd["mode"]
        ha_mode = WT50_POWER_TO_HA_HVAC.get(wt50_mode, "heat_cool")
        if ha_call("climate.set_hvac_mode", climate_entity, {"hvac_mode": ha_mode}):
            executed.append(f"mode={wt50_mode}")
        else:
            failed.append("mode")

    # Set thermostat mode (auto/manual/home/away)
    if "thermostat_mode" in cmd:
        wt50_mode = cmd["thermostat_mode"]
        tuya_mode = WT50_TO_TUYA_MODE.get(wt50_mode, wt50_mode)
        success = False

        # Try select entity first (some Tuya devices use this)
        mode_entity = f"select.{entity_prefix}_mode"
        if ha_call("select.select_option", mode_entity, {"option": tuya_mode}):
            success = True
        else:
            # Fallback: try climate preset_mode
            if ha_call("climate.set_preset_mode", climate_entity, {"preset_mode": tuya_mode}):
                success = True

        if success:
            executed.append(f"thermostat_mode={wt50_mode}")
        else:
            failed.append("thermostat_mode")

    # Child lock
    if "lock" in cmd:
        lock_entity = f"switch.{entity_prefix}_child_lock"
        service = "switch.turn_on" if cmd["lock"] else "switch.turn_off"
        if ha_call(service, lock_entity):
            executed.append(f"lock={cmd['lock']}")
        else:
            failed.append("lock")

    # Anti-frost protection
    if "anti_frost" in cmd:
        frost_entity = f"switch.{entity_prefix}_frost_protection"
        service = "switch.turn_on" if cmd["anti_frost"] else "switch.turn_off"
        if ha_call(service, frost_entity):
            executed.append(f"anti_frost={cmd['anti_frost']}")
        else:
            failed.append("anti_frost")

    # Upper temperature limit (Tuya-specific, maps to max_temp in WT50)
    if "max_temp" in cmd:
        # Try upper_temp first (Tuya naming), then max_temperature
        upper_entity = f"number.{entity_prefix}_upper_temp"
        if ha_call("number.set_value", upper_entity, {"value": cmd["max_temp"]}):
            executed.append(f"max_temp={cmd['max_temp']}")
        else:
            max_entity = f"number.{entity_prefix}_max_temperature"
            if ha_call("number.set_value", max_entity, {"value": cmd["max_temp"]}):
                executed.append(f"max_temp={cmd['max_temp']}")
            else:
                failed.append("max_temp")

    # Lower temperature limit (Tuya-specific)
    if "min_temp" in cmd:
        lower_entity = f"number.{entity_prefix}_lower_temp"
        if ha_call("number.set_value", lower_entity, {"value": cmd["min_temp"]}):
            executed.append(f"min_temp={cmd['min_temp']}")
        else:
            failed.append("min_temp")

    # Temperature calibration/correction
    if "calibration" in cmd:
        cal_entity = f"number.{entity_prefix}_temperature_correction"
        if ha_call("number.set_value", cal_entity, {"value": cmd["calibration"]}):
            executed.append(f"calibration={cmd['calibration']}")
        else:
            failed.append("calibration")

    # Sensor selection (Tuya-specific: in/out)
    if "sensor" in cmd:
        sensor_entity = f"select.{entity_prefix}_sensor_choose"
        if ha_call("select.select_option", sensor_entity, {"option": cmd["sensor"]}):
            executed.append(f"sensor={cmd['sensor']}")
        else:
            failed.append("sensor")

    # Hysteresis (may not be available on all Tuya models)
    if "hysteresis" in cmd:
        hyst_entity = f"number.{entity_prefix}_hysteresis"
        if ha_call("number.set_value", hyst_entity, {"value": cmd["hysteresis"]}):
            executed.append(f"hysteresis={cmd['hysteresis']}")
        else:
            failed.append("hysteresis")

    # Brightness (may not be available on all Tuya models)
    if "brightness" in cmd:
        brightness = cmd["brightness"]
        num_entity = f"number.{entity_prefix}_brightness"
        sel_entity = f"select.{entity_prefix}_brightness"
        if ha_call("number.set_value", num_entity, {"value": brightness}):
            executed.append(f"brightness={brightness}")
        else:
            brightness_options = ["off", "low", "medium", "high"]
            if 0 <= brightness <= 3:
                if ha_call("select.select_option", sel_entity, {"option": brightness_options[brightness]}):
                    executed.append(f"brightness={brightness}")
                else:
                    failed.append("brightness")
            else:
                failed.append("brightness")

    # Invert relay (may not be available on all Tuya models)
    if "invert_relay" in cmd:
        relay_entity = f"switch.{entity_prefix}_invert_relay"
        service = "switch.turn_on" if cmd["invert_relay"] else "switch.turn_off"
        if ha_call(service, relay_entity):
            executed.append(f"invert_relay={cmd['invert_relay']}")
        else:
            failed.append("invert_relay")

    # Sound (Tuya-specific, not on all models)
    if "sound" in cmd:
        sound_entity = f"switch.{entity_prefix}_sound"
        service = "switch.turn_on" if cmd["sound"] else "switch.turn_off"
        if ha_call(service, sound_entity):
            executed.append(f"sound={cmd['sound']}")
        else:
            failed.append("sound")

    # Schedule (WT50 protocol)
    # Note: Tuya schedules are typically managed within the device itself
    # and are not exposed as entities in Home Assistant
    if "schedule" in cmd:
        # Most Tuya integrations don't expose schedule control
        # The schedule would need to be managed through the Tuya app
        logger.warning(f"Schedule command received for {device_id} but not supported via HA")
        logger.debug(f"Schedule data: {cmd['schedule']}")
        failed.append("schedule (not supported via HA - use Tuya app)")

    # Send acknowledgement
    if executed and not failed:
        send_ack(device_id, True, ", ".join(executed))
    elif failed:
        msg = f"failed: {', '.join(failed)}"
        if executed:
            msg = f"ok: {', '.join(executed)}; {msg}"
        send_ack(device_id, False, msg)
    else:
        send_ack(device_id, False, "no valid commands")

    # Clear cached state to force refresh on next publish
    dev = devices.get(device_id, {})
    for key in ["valve", "lock", "frost", "calibration", "hysteresis",
                "upper_temp", "lower_temp", "sensor_choose",
                "brightness", "invert_relay", "sound",
                "thermostat_mode", "thermostat_mode_entity"]:
        dev.pop(key, None)


# ---------------- WebSocket loop ----------------


def get_websocket_url() -> str:
    """Get the WebSocket URL based on environment."""
    # When running as add-on, use supervisor proxy
    if "SUPERVISOR_TOKEN" in os.environ:
        return "ws://supervisor/core/websocket"
    # Fallback for standalone
    return HA_URL.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"


async def listen():
    """Listen for Home Assistant state changes via WebSocket."""
    uri = get_websocket_url()
    logger.info(f"WebSocket URI: {uri}")

    while True:
        try:
            async with websockets.connect(uri) as ws:
                # Authenticate
                auth_msg = await ws.recv()
                logger.debug(f"Auth required: {auth_msg}")

                await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
                auth_result = await ws.recv()
                result = json.loads(auth_result)

                if result.get("type") != "auth_ok":
                    logger.error(f"Authentication failed: {result}")
                    await asyncio.sleep(30)
                    continue

                logger.info("Connected to Home Assistant WebSocket")

                # Subscribe to state changes
                await ws.send(json.dumps({
                    "id": 1,
                    "type": "subscribe_events",
                    "event_type": "state_changed",
                }))

                while True:
                    msg = json.loads(await ws.recv())

                    if msg.get("type") != "event":
                        continue

                    data = msg.get("event", {}).get("data", {})
                    entity_id = data.get("entity_id", "")

                    if not entity_id.startswith("climate."):
                        continue

                    device_id = entity_id.split(".")[1]
                    new_state = data.get("new_state")

                    if not new_state:
                        continue

                    # Update device state - fetch entities in parallel
                    entity_prefix = get_entity_prefix(device_id)
                    entity_data = await fetch_entities_parallel(entity_prefix)
                    devices[device_id] = get_device_data_from_climate(new_state, entity_data)

                    # Note: Periodic publishing is handled by background tasks
                    # We just update the device state here for real-time changes

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"WebSocket connection closed: {e}. Reconnecting in 10s...")
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"WebSocket error: {e}. Reconnecting in 30s...")
            await asyncio.sleep(30)


async def process_commands():
    """Process MQTT commands from the queue."""
    global command_queue
    command_queue = asyncio.Queue()
    
    while True:
        try:
            device_id, cmd = await command_queue.get()
            try:
                await apply_command(device_id, cmd)
            except Exception as e:
                logger.error(f"Error applying command for {device_id}: {e}")
        except Exception as e:
            logger.error(f"Error in command processing task: {e}")
            await asyncio.sleep(1)


async def periodic_telemetry():
    """Publish telemetry for all devices every PUBLISH_INTERVAL seconds."""
    await asyncio.sleep(10)  # Wait for initial device population
    
    while True:
        try:
            # Get all known device IDs
            device_ids = list(devices.keys())
            
            if device_ids:
                logger.debug(f"Publishing periodic telemetry for {len(device_ids)} devices")
                
                for device_id in device_ids:
                    try:
                        publish_telemetry(device_id)
                        last_publish[device_id] = time.time()
                    except Exception as e:
                        logger.error(f"Error publishing telemetry for {device_id}: {e}")
                
                logger.debug(f"Periodic telemetry published for {len(device_ids)} devices")
            
            # Wait for next interval
            await asyncio.sleep(PUBLISH_INTERVAL)
            
        except Exception as e:
            logger.error(f"Error in periodic telemetry task: {e}")
            await asyncio.sleep(PUBLISH_INTERVAL)


async def periodic_full_state():
    """Publish full state for all devices every TELEMETRY_INTERVAL seconds."""
    await asyncio.sleep(20)  # Wait for initial device population
    
    while True:
        try:
            device_ids = list(devices.keys())
            
            if device_ids:
                logger.debug(f"Publishing periodic full state for {len(device_ids)} devices")
                
                # Publish in batches to avoid overload
                batch_size = 5
                for i in range(0, len(device_ids), batch_size):
                    batch = device_ids[i:i+batch_size]
                    tasks = [publish_full_state(device_id) for device_id in batch]
                    await asyncio.gather(*tasks, return_exceptions=True)
                    
                    # Update last telemetry time
                    now = time.time()
                    for device_id in batch:
                        last_telemetry[device_id] = now
                    
                    # Small delay between batches
                    if i + batch_size < len(device_ids):
                        await asyncio.sleep(0.5)
                
                logger.info(f"Periodic full state published for {len(device_ids)} devices")
            
            # Wait for next interval
            await asyncio.sleep(TELEMETRY_INTERVAL)
            
        except Exception as e:
            logger.error(f"Error in periodic full state task: {e}")
            await asyncio.sleep(TELEMETRY_INTERVAL)


async def initialize_devices():
    """Fetch and publish all devices on startup."""
    logger.info("Initializing devices...")
    
    # Get all climate entities from HA
    try:
        r = requests.get(f"{HA_URL}/api/states", headers=HEADERS, timeout=5)
        if not r.ok:
            logger.error(f"Failed to fetch states from HA: {r.status_code}")
            return
        
        all_states = r.json()
        climate_entities = [s for s in all_states if s["entity_id"].startswith("climate.")]
        
        logger.info(f"Found {len(climate_entities)} climate entities in Home Assistant")
        
        # Publish all devices in parallel batches (5 at a time to avoid overload)
        batch_size = 5
        for i in range(0, len(climate_entities), batch_size):
            batch = climate_entities[i:i+batch_size]
            tasks = []
            
            for climate in batch:
                device_id = climate["entity_id"].replace("climate.", "")
                # Only initialize if we have a mapping for this device
                if device_id in DEVICE_MAPPING or not DEVICE_MAPPING:
                    tasks.append(publish_full_state(device_id))
            
            # Execute batch in parallel
            await asyncio.gather(*tasks, return_exceptions=True)
            
            # Small delay between batches to avoid overwhelming HA
            await asyncio.sleep(0.5)
        
        logger.info(f"Initialization complete - published {len(climate_entities)} devices")
        
    except Exception as e:
        logger.error(f"Error during device initialization: {e}")


async def main():
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("SavePower Tuya Thermostat Bridge (WT50 Protocol)")
    logger.info("=" * 60)
    logger.info(f"Organization: {ORG_ID}")
    logger.info(f"MQTT: {MQTT_HOST}:{MQTT_PORT}")
    logger.info(f"Publish interval: {PUBLISH_INTERVAL}s")
    logger.info(f"Telemetry interval: {TELEMETRY_INTERVAL}s")
    logger.info(f"Device mappings: {len(DEVICE_MAPPING)}")
    for climate_id, prefix in DEVICE_MAPPING.items():
        logger.info(f"  climate.{climate_id} → {prefix}_*")

    # Check authentication
    if "SUPERVISOR_TOKEN" in os.environ:
        logger.info("Running as Home Assistant add-on (using Supervisor API)")
    elif HA_TOKEN:
        logger.info("Running standalone with provided HA_TOKEN")
    else:
        logger.error("No authentication token available!")
        logger.error("  - As add-on: SUPERVISOR_TOKEN is auto-provided")
        logger.error("  - Standalone: Set HA_TOKEN environment variable")
        return

    logger.info("=" * 60)

    # Setup MQTT connection
    setup_mqtt()
    
    # Initialize all devices (publish immediately)
    await initialize_devices()

    # Start background tasks
    command_task = asyncio.create_task(process_commands())
    telemetry_task = asyncio.create_task(periodic_telemetry())
    full_state_task = asyncio.create_task(periodic_full_state())
    
    # Start WebSocket listener (this runs forever)
    try:
        await listen()
    finally:
        command_task.cancel()
        telemetry_task.cancel()
        full_state_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
