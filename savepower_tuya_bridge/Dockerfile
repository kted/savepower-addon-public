ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python and dependencies
RUN apk add --no-cache python3 py3-pip

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt /app/
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application
COPY ha_thermostat_mqtt_bridge.py /app/
COPY run.sh /app/

# Make run script executable
RUN chmod a+x /app/run.sh

# Set entrypoint
CMD [ "/app/run.sh" ]
