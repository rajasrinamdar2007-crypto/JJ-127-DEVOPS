#!/usr/bin/env python3
"""Log every sighting on the bus, and nothing else.

Deliberately independent of `ingest`: this subscribes to anpr/+/sightings and prints
what arrives, so you can tell "the detector published nothing" apart from "ingest threw
it away". When a sighting is missing from the database, the first question is which of
those two happened, and this is how you answer it without adding logging to a service
that is supposed to be doing something else.

It is also the fastest way to see a malformed or truncated payload: it prints the raw
bytes it received, not a re-serialised version of them.

The one thing it does *not* do is acknowledge anything on your behalf. It subscribes
without requesting QoS 1 delivery, so it cannot steal a message from `ingest` or
perturb the delivery path it is meant to be observing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys

import paho.mqtt.client as mqtt

log = logging.getLogger("server.subscriber")

TOPIC = "anpr/+/sightings"

# Colour only when attached to a terminal, so redirected output stays clean.
_TTY = sys.stdout.isatty()
_RED = "\033[31m" if _TTY else ""
_YELLOW = "\033[33m" if _TTY else ""
_CYAN = "\033[36m" if _TTY else ""
_DIM = "\033[2m" if _TTY else ""
_RESET = "\033[0m" if _TTY else ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Log all anpr/+/sightings MQTT traffic.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", default=os.getenv("MQTT_HOST", "mosquitto"))
    p.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    p.add_argument("--username", default=os.getenv("MQTT_USERNAME"))
    p.add_argument("--password", default=os.getenv("MQTT_PASSWORD"))
    p.add_argument(
        "--count",
        type=int,
        default=0,
        help="exit after this many messages (0 = run until interrupted)",
    )
    p.add_argument(
        "--raw",
        action="store_true",
        help="print the raw payload instead of a summary",
    )
    return p.parse_args(argv)


def summarise(topic: str, payload: bytes) -> str:
    """One line per message, or the raw bytes if it will not parse."""
    try:
        event = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return f"{_RED}UNPARSEABLE{_RESET} {len(payload)}B {payload[:400]!r}"

    if not isinstance(event, dict):
        return f"{_RED}UNEXPECTED{_RESET} {type(event).__name__}: {str(event)[:200]}"

    plate = event.get("plate_string", "?")
    camera = event.get("camera_id", "?")
    conf = event.get("confidence", "?")
    vehicle = event.get("vehicle_type") or f"{_DIM}unclassified{_RESET}"
    stamp = event.get("timestamp", "?")
    # The fields that actually break a pipeline, called out rather than buried: a
    # missing plate or an out-of-range confidence is what ingest will reject on.
    problems = []
    if not event.get("plate_string"):
        problems.append(f"{_RED}no plate{_RESET}")
    if not isinstance(conf, (int, float)) or not 0.0 <= conf <= 1.0:
        problems.append(f"{_RED}confidence={conf!r}{_RESET}")
    flag = f" {' '.join(problems)}" if problems else ""
    return f"{_CYAN}{plate:<12}{_RESET} {camera:<22} conf={conf:<5} {vehicle:<12} {stamp}{flag}"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    # Callback API v2 signatures. Naming them keeps the intent explicit and avoids the
    # positional-argument trap where a mismatched signature fails at message time, deep
    # in a callback, rather than at connect time.
    def on_connect(client, _userdata, _flags, reason_code, _properties=None):
        if reason_code != 0:
            log.error("connection refused by the broker: %s", reason_code)
            raise SystemExit(1)
        log.info("connected to %s:%s", args.host, args.port)
        client.subscribe(TOPIC, qos=0)
        log.info("subscribed to %s (qos 0 - observing only, not participating in delivery)", TOPIC)

    seen = {"count": 0}

    def on_message(_client, _userdata, msg):
        payload = msg.payload
        seen["count"] += 1
        if args.raw:
            print(f"{msg.topic} {payload!r}", flush=True)
        else:
            print(f"{summarise(msg.topic, payload)}", flush=True)
        if args.count and seen["count"] >= args.count:
            log.info("reached --count %d, exiting", args.count)
            _client.disconnect()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if args.username:
        client.username_pw_set(args.username, args.password)
    client.on_connect = on_connect
    client.on_message = on_message

    def on_disconnect(_client, _userdata, _flags, reason_code, _properties=None):
        if not _stop:
            log.warning("disconnected: %s", reason_code)

    _stop = False

    def handle_stop(_signum, _frame):
        global _stop
        _stop = True
        log.info("stopping")
        client.disconnect()

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    client.on_disconnect = on_disconnect

    try:
        client.connect(args.host, args.port, keepalive=60)
    except OSError as e:
        log.error("cannot reach the broker at %s:%s: %s", args.host, args.port, e)
        log.error("is the stack up? try: docker compose up -d mosquitto")
        return 1

    client.loop_forever()
    log.info("saw %d message(s)", seen["count"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
