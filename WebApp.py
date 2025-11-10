import json
import os
import threading
import time
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
    "light_status": "OFF",
    "auto_mode": True,
    "received_at": None
}

# ---------------- REDIS & FLASK APP ----------------
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
REDIS_KEY = "bublib:telemetry"

app = Flask(__name__)

# ---------------- HTML TEMPLATE ----------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Bublib Smart Bulb</title>
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">

    <style>
        body {
            padding-top: env(safe-area-inset-top);
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
        .server-status {
            position: fixed;
            bottom: 10px;
            font-size: 0.8em;
            color: #888;
            transition: color 0.5s ease;
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

        <div id="serverStatus" class="server-status">Connecting...</div>
    </div>

    <script>
        const powerButton = document.getElementById('powerButton');
        const autoModeSwitch = document.getElementById('autoModeSwitch');
        const motionStatus = document.getElementById('motionStatus');
        const serverStatus = document.getElementById('serverStatus');
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

        eventSource.onopen = function() {
            serverStatus.textContent = 'Server Connected';
            serverStatus.style.color = '#34c759'; // A nice green color
        };

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
            serverStatus.textContent = 'Connection Lost';
            serverStatus.style.color = '#ff3b30'; // A red color for errors
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
    redis_client.hset(REDIS_KEY, 'auto_mode', json.dumps(data.get('auto_mode', True)))
    return "OK"


@app.route('/manual_control', methods=['POST'])
def manual_control():
    if not registry_manager:
        return "IoT Hub not configured", 500

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
                        # Update state in Redis
                        redis_client.hset(REDIS_KEY, 'light_status', desired)
                        state['light_status'] = desired  # Update for immediate SSE send

                        # Send command to device if configured
                        if registry_manager:
                            cmd = "1" if desired == "ON" else "0"
                            registry_manager.send_c2d_message(DEVICE_ID, cmd)
                            print(f"Auto-mode: sent command '{cmd}' to device '{DEVICE_ID}'")

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
        body = event.body_as_json(encoding='utf-8')
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
    if error:
        print(f"EventHub listener error on partition {partition_context.partition_id}: {error}")


def start_event_hub_listener():
    if not EVENT_HUB_CONNECTION_STRING:
        print("EVENT_HUB_CONN_STR not set. Event Hub listener will not start.")
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
telemetry_lock = threading.Lock()

listener_thread = threading.Thread(target=start_event_hub_listener, daemon=True)
listener_thread.start()


# ---------------- MAIN (local dev) ----------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
