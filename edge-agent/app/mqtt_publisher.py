import json
import logging
import threading
import time

import paho.mqtt.client as mqtt

from app import settings, spool

log = logging.getLogger(__name__)

_outbox = None
_connected = threading.Event()
_pending = {}  # paho mid -> outbox Path
_inflight = {}  # outbox Path -> monotonic timestamp sent


def on_connect(client, userdata, flags, reason_code, properties=None):
    if getattr(reason_code, "is_failure", False):
        log.error("Broker refused the connection: %s", reason_code)
        return
    _connected.set()
    log.info(
        "connected to %s:%s, publishing to %s at QoS %s",
        settings.MQTT_HOST,
        settings.MQTT_PORT,
        settings.event_topic(),
        settings.MQTT_QOS,
    )


def on_disconnect(client, userdata, flags, reason_code=None, properties=None):
    _connected.clear()
    # clean_session means the broker forgets our unacknowledged QoS 1 messages, but
    # they are still on our disk. Forget our in-flight bookkeeping too, otherwise the
    # next drain would skip exactly the events that most need republishing.
    _inflight.clear()
    log.warning("disconnected (rc=%s); events stay in the outbox", reason_code)


def on_publish(client, userdata, mid, reason_code=None, properties=None):
    path = _pending.pop(mid, None)
    if path is None:
        return

    # on_publish fires for QoS 1 on PUBACK - but it also fires when the broker
    # *refuses* the publish. Treating a refusal as a delivery would delete the event
    # from the outbox and lose the sighting with no trace.
    if reason_code is not None and getattr(reason_code, "is_failure", False):
        _inflight.pop(path, None)
        log.error("Broker refused %s: %s; keeping it in the outbox", path.name, reason_code)
        return

    _inflight.pop(path, None)
    try:
        _outbox.remove(path)
        log.info("ACK: %s delivered, removed from outbox", path.name)
    except Exception:
        log.exception("Failed to remove %s from the outbox after ACK", path.name)


def _drain(client):
    now = time.monotonic()
    for path, event in _outbox.peek():
        sent_at = _inflight.get(path)
        if sent_at is not None:
            if now - sent_at < settings.ACK_TIMEOUT_SECONDS:
                continue
            log.warning(
                "No ACK for %s within %.0fs; republishing",
                path.name,
                settings.ACK_TIMEOUT_SECONDS,
            )
            _inflight.pop(path, None)

        info = client.publish(
            settings.event_topic(),
            json.dumps(event),
            qos=settings.MQTT_QOS,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            # Queued-but-full, not delivered. Leave it in the outbox for the next pass.
            log.warning("Publish not queued for %s (rc=%s); will retry", path.name, info.rc)
            continue

        _inflight[path] = now
        _pending[info.mid] = path


def start():
    global _outbox
    _outbox = spool.Outbox()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"edge-{settings.CAMERA_ID}",
        clean_session=True,
    )
    if settings.MQTT_USERNAME:
        client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_publish = on_publish
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.loop_start()

    pending = _outbox.count()
    if pending:
        log.info("Resuming with %d event(s) already in the outbox", pending)

    while True:
        try:
            if not _connected.is_set():
                client.connect(
                    settings.MQTT_HOST,
                    settings.MQTT_PORT,
                    keepalive=settings.MQTT_KEEPALIVE,
                )
            else:
                _drain(client)
        except Exception:
            # Includes the broker being down. Events stay on disk either way.
            log.exception("publish loop error; events stay in the outbox")
        time.sleep(settings.POLL_SECONDS)
