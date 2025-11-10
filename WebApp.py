import json
import os
import threading
import time
from datetime import datetime
from flask import Flask, Response, request
from azure.eventhub import EventHubConsumerClient
import redis

# ---------------- CONFIGURATION ----------------
EVENT_HUB_CONNECTION_STRING = os.environ.get("EVENT_HUB_CONN_STR")
CONSUMER_GROUP = "$Default"
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

# ---------------- INITIAL STATE ----------------
INITIAL_TELEMETRY = {
    "motion": False,
    "light_status": "OFF",
    "auto_mode": True,
    "received_at": None
}

# ---------------- REDIS & FLASK APP ----------------
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
REDIS_KEY = "bublib:telemetry"

app = Flask(__name__)

# ---------------- HTML TEMPLATE ----------------
HTML_TEMPLATE = """<html> ... (KEEP THE HTML EXACTLY AS YOU HAD IT) ... </html>"""


@app.route('/')
def index():
    return HTML_TEMPLATE


@app.route('/toggle_auto', methods=['POST'])
def toggle_auto():
    data = request.get_json()
    redis_client.hset(REDIS_KEY, 'auto_mode', json.dumps(data.get('auto_mode', True)))
    return "OK"


@app.route('/manual_control', methods=['POST'])
def manual_control():
    with redis_client.pipeline() as pipe:
        try:
            pipe.watch(REDIS_KEY)
            current_state = pipe.hgetall(REDIS_KEY)
            is_auto_mode = json.loads(current_state.get('auto_mode', 'true'))

            if not is_auto_mode:
                light_status = current_state.get('light_status', 'OFF')
                new_light_status = 'OFF' if light_status == 'ON' else 'ON'
                pipe.multi()
                pipe.hset(REDIS_KEY, 'light_status', new_light_status)
                pipe.execute()
        except redis.exceptions.WatchError:
            pass
    return "OK"


@app.route('/stream')
def stream():
    def event_stream():
        last_sent = None
        while True:
            with telemetry_lock:
                data = redis_client.hgetall(REDIS_KEY)
                state = {
                    'motion': json.loads(data.get('motion', 'false')),
                    'light_status': data.get('light_status', INITIAL_TELEMETRY['light_status']),
                    'auto_mode': json.loads(data.get('auto_mode', 'true')),
                    'received_at': data.get('received_at', None)
                }

                if state['auto_mode']:
                    desired = 'ON' if state['motion'] else 'OFF'
                    if desired != state['light_status']:
                        redis_client.hset(REDIS_KEY, 'light_status', desired)
                        state['light_status'] = desired

            if state != last_sent and state['received_at'] is not None:
                yield f"data: {json.dumps(state)}\n\n"
                last_sent = state

            time.sleep(0.1)

    return Response(
        event_stream(),
        mimetype='text/event-stream',
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )


# ---------------- AZURE EVENT HUB LISTENER ----------------
def on_event(partition_context, event):
    try:
        body = event.body_as_json()
        payload = body.get("event", {}).get("payload", body)
        if isinstance(payload, str):
            payload = json.loads(payload)

        motion_detected = payload.get('motion', False)
        redis_client.hset(REDIS_KEY, mapping={
            'motion': json.dumps(motion_detected),
            'received_at': datetime.now().isoformat()
        })
    except Exception as e:
        print(f"Error processing event: {e}")


def on_error(partition_context, error):
    print(f"EventHub error: {error}")


def start_event_hub_listener():
    if not EVENT_HUB_CONNECTION_STRING:
        print("No Event Hub connection string. Listener not started.")
        return

    if not redis_client.exists(REDIS_KEY):
        redis_client.hset(REDIS_KEY, mapping={
            'motion': json.dumps(INITIAL_TELEMETRY['motion']),
            'light_status': INITIAL_TELEMETRY['light_status'],
            'auto_mode': json.dumps(INITIAL_TELEMETRY['auto_mode']),
            'received_at': ''
        })

    client = EventHubConsumerClient.from_connection_string(
        conn_str=EVENT_HUB_CONNECTION_STRING,
        consumer_group=CONSUMER_GROUP,
    )

    with client:
        client.receive(
            on_event=on_event,
            on_error=on_error,
            starting_position="@latest",
        )


# ---------------- GLOBAL START (works in Gunicorn) ----------------
telemetry_lock = threading.Lock()

listener_thread = threading.Thread(target=start_event_hub_listener, daemon=True)
listener_thread.start()


# ---------------- MAIN (local dev) ----------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
