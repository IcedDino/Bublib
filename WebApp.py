from datetime import datetime
from flask import Flask, Response, request
from azure.eventhub import EventHubConsumerClient
from azure.iot.hub import IoTHubRegistryManager
import redis

# ---------------- CONFIGURATION ----------------
EVENT_HUB_CONNECTION_STRING = os.environ.get("EVENT_HUB_CONN_STR")
CONSUMER_GROUP = "$Default"
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

IOTHUB_CONNECTION_STRING = os.environ.get("IOT_HUB_CONN_STR")
DEVICE_ID = "RaspBerry"  # EXACT SAME name registered in IoT Hub
# Initialize registry_manager only if the connection string is available
registry_manager = IoTHubRegistryManager.from_connection_string(IOTHUB_CONNECTION_STRING) if IOTHUB_CONNECTION_STRING else None

# ---------------- INITIAL STATE ----------------
INITIAL_TELEMETRY = {
"motion": False,
@@ -296,6 +302,9 @@ def toggle_auto():

@app.route('/manual_control', methods=['POST'])
def manual_control():
    if not registry_manager:
        return "IoT Hub not configured", 500

with redis_client.pipeline() as pipe:
try:
pipe.watch(REDIS_KEY)
@@ -308,11 +317,19 @@ def manual_control():
pipe.multi()
pipe.hset(REDIS_KEY, 'light_status', new_light_status)
pipe.execute()

                # ---- SEND COMMAND TO DEVICE HERE ----
                cmd = "1" if new_light_status == "ON" else "0"
                registry_manager.send_c2d_message(DEVICE_ID, cmd)

except redis.exceptions.WatchError:
            # The key was modified by another client, abort the transaction
pass

return "OK"



@app.route('/stream')
def stream():
def event_stream():
@@ -330,8 +347,15 @@ def event_stream():
if state['auto_mode']:
desired = 'ON' if state['motion'] else 'OFF'
if desired != state['light_status']:
                        # Update state in Redis
redis_client.hset(REDIS_KEY, 'light_status', desired)
                        state['light_status'] = desired
                        state['light_status'] = desired  # Update for immediate SSE send

                        # Send command to device if configured
                        if registry_manager:
                            cmd = "1" if desired == "ON" else "0"
                            registry_manager.send_c2d_message(DEVICE_ID, cmd)
                            print(f"Auto-mode: sent command '{cmd}' to device '{DEVICE_ID}'")

if state != last_sent and state['received_at'] is not None:
yield f"data: {json.dumps(state)}\n\n"
@@ -352,7 +376,7 @@ def event_stream():
# ---------------- AZURE EVENT HUB LISTENER ----------------
def on_event(partition_context, event):
try:
        body = event.body_as_json()
        body = event.body_as_json(encoding='utf-8')
payload = body.get("event", {}).get("payload", body)
if isinstance(payload, str):
payload = json.loads(payload)
@@ -367,12 +391,13 @@ def on_event(partition_context, event):


def on_error(partition_context, error):
    print(f"EventHub error: {error}")
    if error:
        print(f"EventHub listener error on partition {partition_context.partition_id}: {error}")


def start_event_hub_listener():
if not EVENT_HUB_CONNECTION_STRING:
        print("No Event Hub connection string. Listener not started.")
        print("EVENT_HUB_CONN_STR not set. Event Hub listener will not start.")
return

if not redis_client.exists(REDIS_KEY):
@@ -387,13 +412,16 @@ def start_event_hub_listener():
conn_str=EVENT_HUB_CONNECTION_STRING,
consumer_group=CONSUMER_GROUP,
)

    with client:
        client.receive(
            on_event=on_event,
            on_error=on_error,
            starting_position="@latest",
        )
    print("Starting Event Hub listener...")
    try:
        with client:
            client.receive(
                on_event=on_event,
                on_error=on_error,
                starting_position="-1",  # Start from the latest event
            )
    except Exception as e:
        print(f"Event Hub listener failed to start: {e}")


# ---------------- GLOBAL START (works in Gunicorn) ----------------