import os
import json
from datetime import datetime
from flask import Flask, Response, request
from azure.eventhub import EventHubConsumerClient
from azure.iot.hub import IoTHubRegistryManager
import redis

# ---------------- CONFIGURATION ----------------
EVENT_HUB_CONNECTION_STRING = os.environ.get("EVENT_HUB_CONN_STR")
CONSUMER_GROUP = "$Default"
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
REDIS_KEY = "device_state"

IOTHUB_CONNECTION_STRING = os.environ.get("IOT_HUB_CONN_STR")
DEVICE_ID = "RaspBerry"  # EXACT SAME name registered in IoT Hub

# Initialize registry_manager only if the connection string is available
registry_manager = IoTHubRegistryManager.from_connection_string(IOTHUB_CONNECTION_STRING) if IOTHUB_CONNECTION_STRING else None

# ---------------- INITIAL STATE ----------------
INITIAL_TELEMETRY = {
    "motion": False,
    "light_status": "OFF",
    "auto_mode": False,
    "received_at": None
}

# ---------------- REDIS CLIENT ----------------
redis_client = redis.from_url(REDIS_URL)

# ---------------- FLASK APP ----------------
app = Flask(__name__)


@app.route('/manual_control', methods=['POST'])
def manual_control():
    if not registry_manager:
        return "IoT Hub not configured", 500

    new_light_status = request.json.get("light_status")
    if new_light_status not in ("ON", "OFF"):
        return "Invalid light status", 400

    try:
        with redis_client.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(REDIS_KEY)
                    pipe.multi()
                    pipe.hset(REDIS_KEY, 'light_status', new_light_status)
                    pipe.execute()

                    # Send command to device
                    cmd = "1" if new_light_status == "ON" else "0"
                    registry_manager.send_c2d_message(DEVICE_ID, cmd)
                    break
                except redis.exceptions.WatchError:
                    # Retry if the key was modified by another client
                    continue
    except Exception as e:
        return f"Redis error: {e}", 500

    return "OK"


@app.route('/stream')
def stream():
    def event_stream():
        last_sent = None
        while True:
            state = redis_client.hgetall(REDIS_KEY)
            state = {k.decode(): v.decode() for k, v in state.items()}

            # Auto-mode logic
            if state.get('auto_mode') == "True":
                desired = 'ON' if state.get('motion') == "True" else 'OFF'
                if desired != state.get('light_status'):
                    redis_client.hset(REDIS_KEY, 'light_status', desired)
                    state['light_status'] = desired

                    # Send command to device if configured
                    if registry_manager:
                        cmd = "1" if desired == "ON" else "0"
                        registry_manager.send_c2d_message(DEVICE_ID, cmd)
                        print(f"Auto-mode: sent command '{cmd}' to device '{DEVICE_ID}'")

            if state != last_sent:
                state['received_at'] = datetime.utcnow().isoformat()
                yield f"data: {json.dumps(state)}\n\n"
                last_sent = state

    return Response(event_stream(), mimetype='text/event-stream')


# ---------------- AZURE EVENT HUB LISTENER ----------------
def on_event(partition_context, event):
    try:
        body = event.body_as_json(encoding='utf-8')
        payload = body.get("event", {}).get("payload", body)
        if isinstance(payload, str):
            payload = json.loads(payload)
        redis_client.hmset(REDIS_KEY, payload)
    except Exception as e:
        print(f"Failed to process event: {e}")


def on_error(partition_context, error):
    print(f"EventHub error: {error}")


def start_event_hub_listener():
    if not EVENT_HUB_CONNECTION_STRING:
        print("EVENT_HUB_CONN_STR not set. Event Hub listener will not start.")
        return

    client = EventHubConsumerClient.from_connection_string(
        conn_str=EVENT_HUB_CONNECTION_STRING,
        consumer_group=CONSUMER_GROUP
    )

    if not redis_client.exists(REDIS_KEY):
        redis_client.hmset(REDIS_KEY, INITIAL_TELEMETRY)

    print("Starting Event Hub listener...")
    try:
        with client:
            client.receive(
                on_event=on_event,
                on_error=on_error,
                starting_position="-1"  # Start from the latest event
            )
    except Exception as e:
        print(f"Event Hub listener failed to start: {e}")


# ---------------- GLOBAL START ----------------
if __name__ == "__main__":
    start_event_hub_listener()
    app.run(host="0.0.0.0", port=5000, debug=True)
