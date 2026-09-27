import json
import logging

import paho.mqtt.client as mqtt

from app import contract, settings, storage

log = logging.getLogger(__name__)

_client = None


def on_connect(client, userdata, flags, reason_code, properties=None):
    if getattr(reason_code, "is_failure", False):
        log.error("Broker refused the connection: %s", reason_code)
        return
    log.info(
        "Connected to %s:%s, subscribing to %s at QoS %s",
        settings.MQTT_HOST,
        settings.MQTT_PORT,
        settings.MQTT_TOPIC,
        settings.MQTT_QOS,
    )
    client.subscribe(settings.MQTT_TOPIC, qos=settings.MQTT_QOS)


def on_disconnect(client, userdata, flags, reason_code=None, properties=None):
    log.warning("Disconnected from broker: %s", reason_code)


def _handle(msg):
    try:
        sighting = json.loads(msg.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        log.warning("Dropping unparseable payload on %s: %s", msg.topic, e)
        return

    problems = contract.validate(sighting)
    if problems:
        log.warning("Dropping invalid sighting on %s: %s", msg.topic, "; ".join(problems))
        return

    # OD_INTERFACE.md: the payload's camera_id must equal the topic's, otherwise we
    # would be storing a position against the wrong camera identity.
    topic_camera = contract.camera_id_from_topic(msg.topic)
    if topic_camera is None:
        log.warning("Dropping sighting on topic that does not match the contract: %r", msg.topic)
        return
    if topic_camera != sighting["camera_id"]:
        log.warning(
            "Dropping sighting: topic says %r, payload says %r",
            topic_camera,
            sighting["camera_id"],
        )
        return

    result = storage.save_sighting(sighting)
    if result == "alerted":
        log.warning(
            "ALERT %s at %s (%s)",
            sighting["plate_string"],
            sighting["camera_id"],
            sighting["timestamp"],
        )
    else:
        log.info(
            "[%s] %s (%s @ %s)",
            result,
            sighting["plate_string"],
            sighting["camera_id"],
            sighting["timestamp"],
        )


def on_message(client, userdata, msg):
    # Outermost guard: paho does not contain exceptions raised in this callback, and
    # one escaping here tears down the network loop and silently stops ingest.
    try:
        _handle(msg)
    except Exception:
        log.exception("Unhandled error while processing %s; message dropped", msg.topic)


def is_connected() -> bool:
    return _client is not None and _client.is_connected()


def start():
    global _client

    _client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="ingest",
        clean_session=True,
    )
    if settings.MQTT_USERNAME:
        _client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
    _client.on_connect = on_connect
    _client.on_disconnect = on_disconnect
    _client.on_message = on_message
    _client.reconnect_delay_set(min_delay=1, max_delay=30)
    _client.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=settings.MQTT_KEEPALIVE)
    _client.loop_start()

    log.info(
        "Ingest started. MQTT %s:%s -> POSTGRES %s:%s",
        settings.MQTT_HOST,
        settings.MQTT_PORT,
        settings.POSTGRES_HOST,
        settings.POSTGRES_PORT,
    )


def stop():
    if _client is not None:
        _client.loop_stop()
        _client.disconnect()
