#!/usr/bin/env bash
set -e

CONFIG_PATH=/data/options.json

# Extract configuration
export MQTT_HOST=$(jq -r '.mqtt_host' $CONFIG_PATH)
export MQTT_PORT=$(jq -r '.mqtt_port' $CONFIG_PATH)
export MQTT_USERNAME=$(jq -r '.mqtt_username // empty' $CONFIG_PATH)
export MQTT_PASSWORD=$(jq -r '.mqtt_password // empty' $CONFIG_PATH)
export ORG_ID=$(jq -r '.organization_id' $CONFIG_PATH)
export PUBLISH_INTERVAL=$(jq -r '.publish_interval' $CONFIG_PATH)
export TELEMETRY_INTERVAL=$(jq -r '.telemetry_interval' $CONFIG_PATH)
export LOG_LEVEL=$(jq -r '.log_level' $CONFIG_PATH)
export DEVICE_MAPPING=$(jq -c '.device_mapping' $CONFIG_PATH)

# Get Supervisor token for HA API access
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN}"

echo "Starting SavePower Tuya Thermostat Bridge..."
echo "  MQTT: ${MQTT_HOST}:${MQTT_PORT}"
echo "  Organization: ${ORG_ID}"
echo "  Publish interval: ${PUBLISH_INTERVAL}s"
echo "  Telemetry interval: ${TELEMETRY_INTERVAL}s"
echo "  Log level: ${LOG_LEVEL}"

exec python3 /app/ha_thermostat_mqtt_bridge.py
