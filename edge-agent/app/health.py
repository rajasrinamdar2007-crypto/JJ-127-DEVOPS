import sys
import time

import paho.mqtt.client as mqtt

from app import settings

TIMEOUT_SECONDS = 5.0


def main() -> int:
    """Exit 0 only if this container can reach the broker with its own credentials.

    A refused connection is reported by paho through on_connect with a failure reason
    code rather than as an exception, so we drive the loop ourselves and wait for
    is_connected() instead of trusting connect() to have authenticated us.
    """
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"health-{settings.CAMERA_ID}",
    )
    if settings.MQTT_USERNAME:
        client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)

    connected = False
    try:
        client.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=10)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            client.loop(timeout=0.5)
            if client.is_connected():
                connected = True
                break
    except Exception as e:
        print(f"[health] broker unreachable: {e}")
    finally:
        try:
            client.disconnect()
        except Exception:
            pass

    # Test the flag we latched, not client.is_connected() - disconnect() above has
    # already flipped that back to False by now.
    if connected:
        print(f"[health] ok: {settings.CAMERA_ID} -> {settings.MQTT_HOST}:{settings.MQTT_PORT}")
        return 0

    print("[health] could not authenticate with the broker")
    return 1


if __name__ == "__main__":
    sys.exit(main())
