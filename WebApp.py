# flask_telemetry_dashboard.py
import json
import os
import threading
import time
from datetime import datetime
from flask import Flask, render_template, Response, request
from azure.eventhub import EventHubConsumerClient
import redis

# ---------------- CONFIGURATION ----------------
EVENT_HUB_CONNECTION_STRING = os.environ.get("EVENT_HUB_CONN_STR") or "Endpoint=sb://iothub-ns-mvptorreta-55640691-46b6b52c16.servicebus.windows.net/;SharedAccessKeyName=service;SharedAccessKey=J9kflef+yGNDptqfJFSLUugtYaOrsNxZ2AIoTP52ALw=;EntityPath=mvptorreta"
CONSUMER_GROUP = "$Default"
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

# ---------------- INITIAL STATE ----------------
# This is the default state if Redis is empty.
INITIAL_TELEMETRY = {
    "motion": False,
    "light_status": "OFF", # ON, OFF
    "auto_mode": True,
    "received_at": None
}

# ---------------- REDIS & FLASK APP ----------------
# Connect to Redis. `decode_responses=True` means we get strings back, not bytes.
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# The key we will use to store our state hash in Redis.
REDIS_KEY = "bublib:telemetry"

app = Flask(__name__)

# ---------------- HTML TEMPLATE ----------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Bublib Smart Bulb</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background: #1c1c1e;
            color: white;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            text-align: center;
        }
        .container {
            width: 90%;
            max-width: 380px;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 35px;
        }
        .header h1 {
            margin: 0;
            font-size: 3em;
            font-weight: 700;
            color: #f0f0f0;
        }
        .header p {
            margin: -5px 0 0 0;
            font-size: 1.2em;
            font-weight: 500;
            color: #888;
        }
        .power-button {
            width: 100%; /* Set width to match parent container */
            aspect-ratio: 1 / 1; /* Maintain a 1:1 aspect ratio (a square) */
            height: auto; /* Allow height to be determined by aspect-ratio */
            border-radius: 50%; /* Make the square a circle */
            border: 12px solid #444; /* Adjusted border */
            background-color: #2c2c2e;
            color: white;
            font-size: 4em; /* Adjusted font size */
            font-weight: bold;
            cursor: pointer;
            display: flex;
            justify-content: center;
            align-items: center;
            transition: all 0.3s ease;
            -webkit-tap-highlight-color: transparent; /* Removes tap highlight on mobile */
        }
        .power-button.on {
            border-color: #007aff;
            box-shadow: 0 0 30px rgba(0, 122, 255, 0.8);
        }
        .power-button.off {
            border-color: #444;
        }
        .power-button:disabled {
            cursor: not-allowed;
            opacity: 0.6;
        }
         .power-button:not(:disabled):active {
            transform: scale(0.97);
        }
        .auto-control {
            background: #2c2c2e;
            border-radius: 20px;
            padding: 20px 25px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            width: 100%;
            box-sizing: border-box;
        }
        .label {
            font-size: 1.1em;
            font-weight: 500;
        }
        .switch {
            position: relative;
            display: inline-block;
            width: 60px;
            height: 34px;
        }
        .switch input { display: none; }
        .slider {
            position: absolute;
            cursor: pointer;
            top: 0; left: 0; right: 0; bottom: 0;
            background-color: #444;
            transition: .4s;
            border-radius: 34px;
        }
        .slider:before {
            position: absolute;
            content: "";
            height: 26px;
            width: 26px;
            left: 4px;
            bottom: 4px;
            background-color: white;
            transition: .4s;
            border-radius: 50%;
        }
        input:checked + .slider { background-color: #007aff; }
        input:checked + .slider:before { transform: translateX(26px); }

        .status-indicator {
            padding: 12px 20px;
            border-radius: 15px;
            font-weight: bold;
            width: 100%;
            box-sizing: border-box;
        }
        .status-indicator.motion { background: #007aff; color: white; }
        .status-indicator.no-motion { background: #2c2c2e; color: #aaa; }

        .timestamp {
            color: #888;
            font-size: 0.9em;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Bublib</h1>
            <p>Smart Bulb</p>
        </div>

        <button id="powerButton" class="power-button off">OFF</button>

        <div class="auto-control">
            <span class="label">Auto Detection</span>
            <label class="switch">
                <input type="checkbox" id="autoModeSwitch" checked>
                <span class="slider"></span>
            </label>
        </div>

        <div id="motionStatus" class="status-indicator no-motion">No Motion</div>

        <div class="timestamp">
            Last Update: <span id="timestamp">Never</span>
        </div>
    </div>

    <script>
        const powerButton = document.getElementById('powerButton');
        const autoModeSwitch = document.getElementById('autoModeSwitch');
        const motionStatus = document.getElementById('motionStatus');
        const timestamp = document.getElementById('timestamp');

        // --- Event Listeners for Controls ---
        autoModeSwitch.addEventListener('change', function() {
            fetch('/toggle_auto', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ auto_mode: this.checked })
            });
        });

        powerButton.addEventListener('click', function() {
            // Send the request to the server
            fetch('/manual_control', { method: 'POST' });

            // Optimistic UI update: Immediately toggle the button's state for better feedback
            // This will be corrected by the server-sent event if the state is out of sync.
            if (!this.disabled) {
                const isCurrentlyOn = this.classList.contains('on');
                if (isCurrentlyOn) {
                    this.classList.remove('on');
                    this.classList.add('off');
                    this.textContent = 'OFF';
                } else {
                    this.classList.remove('off');
                    this.classList.add('on');
                    this.textContent = 'ON';
                }
            }
        });

        // --- Server-Sent Events for Status Updates ---
        const eventSource = new EventSource('/stream');

        eventSource.onmessage = function(event) {
            const data = JSON.parse(event.data);

            // Update Auto Mode Switch and disable power button if needed
            autoModeSwitch.checked = data.auto_mode;
            powerButton.disabled = data.auto_mode;

            // Update Power Button state (True source of truth)
            if (data.light_status === "ON") {
                powerButton.textContent = "ON";
                powerButton.classList.remove('off');
                powerButton.classList.add('on');
            } else {
                powerButton.textContent = "OFF";
                powerButton.classList.remove('on');
                powerButton.classList.add('off');
            }

            // Update Motion Status
            if (data.motion) {
                motionStatus.textContent = "Motion Detected";
                motionStatus.classList.remove('no-motion');
                motionStatus.classList.add('motion');
            } else {
                motionStatus.textContent = "No Motion";
                motionStatus.classList.remove('motion');
                motionStatus.classList.add('no-motion');
            }

            // Update Timestamp
            const date = new Date(data.received_at);
            timestamp.textContent = date.toLocaleString();
        };

        eventSource.onerror = function() {
            motionStatus.textContent = 'Connection Lost';
            motionStatus.classList.remove('motion');
            motionStatus.classList.add('no-motion');
        };
    </script>
</body>
</html>
"""


@app.route('/')
def index():
    return HTML_TEMPLATE

@app.route('/toggle_auto', methods=['POST'])
def toggle_auto():
    data = request.get_json()
    new_mode = data.get('auto_mode', True)
    redis_client.hset(REDIS_KEY, 'auto_mode', json.dumps(new_mode))
    return "OK"

@app.route('/manual_control', methods=['POST'])
def manual_control():
    # Use a Redis transaction to safely read and write the state.
    with redis_client.pipeline() as pipe:
        try:
            pipe.watch(REDIS_KEY) # Watch for changes from other clients
            current_state = pipe.hgetall(REDIS_KEY)
            is_auto_mode = json.loads(current_state.get('auto_mode', 'true'))

            # Manual control is only allowed if auto_mode is off
            if not is_auto_mode:
                light_status = current_state.get('light_status', 'OFF')
                new_light_status = 'OFF' if light_status == 'ON' else 'ON'
                
                pipe.multi() # Start transaction
                pipe.hset(REDIS_KEY, 'light_status', new_light_status)
                pipe.execute()
        except redis.exceptions.WatchError:
            # The state was changed by another process (e.g., the stream loop)
            # while we were working. We can just ignore the click.
            pass

    return "OK"

@app.route('/stream')
def stream():
    def event_stream():
        last_sent = None
        while True:
            with telemetry_lock:
                # Fetch the complete state from Redis
                current_state_str = redis_client.hgetall(REDIS_KEY)
                # Deserialize values from JSON strings
                current_state = {
                    'motion': json.loads(current_state_str.get('motion', 'false')),
                    'light_status': current_state_str.get('light_status', INITIAL_TELEMETRY['light_status']),
                    'auto_mode': json.loads(current_state_str.get('auto_mode', 'true')),
                    'received_at': current_state_str.get('received_at', None)
                }

                # Server-side logic for auto mode
                if current_state['auto_mode']:
                    new_light_status = 'ON' if current_state['motion'] else 'OFF'
                    if new_light_status != current_state['light_status']:
                        current_state['light_status'] = new_light_status
                        redis_client.hset(REDIS_KEY, 'light_status', new_light_status)

            if current_state != last_sent and current_state['received_at'] is not None:
                yield f"data: {json.dumps(current_state)}\n\n"
                last_sent = current_state

            time.sleep(0.1)
    
    return Response(event_stream(), mimetype='text/event-stream')


# ---------------- AZURE EVENT HUB LISTENER ----------------
def on_event(partition_context, event):
    global latest_telemetry

    try:
        body = event.body_as_json()
        payload = body  # Assuming direct payload

        # Handle nested payload if necessary
        if "event" in body and "payload" in body["event"]:
            payload = json.loads(body["event"]["payload"])

        # Prepare data to be stored in Redis.
        # We store booleans and other non-string types as JSON strings.
        motion_detected = payload.get('motion', False)
        update_payload = {
            'motion': json.dumps(motion_detected),
            'received_at': datetime.now().isoformat()
        }

        # Update the hash in Redis
        redis_client.hset(REDIS_KEY, mapping=update_payload)

        print(f"Received: motion={payload.get('motion')}")

    except Exception as e:
        print(f"Error processing event: {e}")

def on_error(partition_context, error):
    print(f"Error on partition {partition_context.partition_id}: {error}")


def start_event_hub_listener():
    """Start listening to Azure IoT Hub events in a background thread"""
    if not EVENT_HUB_CONNECTION_STRING or "Endpoint=sb://iothub-ns" in EVENT_HUB_CONNECTION_STRING:
        print("WARNING: Event Hub connection string not set or is default. Listener not started.")
        return

    # Initialize state in Redis if it doesn't exist
    if not redis_client.exists(REDIS_KEY):
        # Store booleans as JSON strings
        initial_payload = {
            'motion': json.dumps(INITIAL_TELEMETRY['motion']),
            'light_status': INITIAL_TELEMETRY['light_status'],
            'auto_mode': json.dumps(INITIAL_TELEMETRY['auto_mode']),
            'received_at': INITIAL_TELEMETRY['received_at'] or ''
        }
        redis_client.hset(REDIS_KEY, mapping=initial_payload)
        print("Initialized state in Redis.")

    try:
        client = EventHubConsumerClient.from_connection_string(
            conn_str=EVENT_HUB_CONNECTION_STRING,
            consumer_group=CONSUMER_GROUP,
        )
        
        print("Starting Event Hub listener...")
        # Using `receive_batch` in a loop is often more robust for long-running listeners
        with client:
            client.receive(
                on_event=on_event,
                on_error=on_error,
                starting_position="-1",  # "-1" is from the beginning of the partition. Use "@latest" for new events.
            )
    except Exception as e:
        print(f"Failed to start Event Hub listener: {e}")

# ---------------- MAIN ----------------
if __name__ == '__main__':
    # This lock is no longer strictly needed for state, but can be kept if other global resources are added.
    telemetry_lock = threading.Lock()

    listener_thread = threading.Thread(target=start_event_hub_listener, daemon=True)
    listener_thread.start()

    # Get port from environment variable, defaulting to 5001 for local dev
    port = int(os.environ.get('PORT', 5001))
    print("Starting Flask server on http://localhost:5001")
    app.run(host='0.0.0.0', port=port, debug=False)
