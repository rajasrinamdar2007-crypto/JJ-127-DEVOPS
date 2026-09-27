import math
import os
import sys

from dotenv import load_dotenv

from app import contract

load_dotenv()

MQTT_HOST = os.getenv("MQTT_HOST", contract.TRANSPORT["broker_host"])
MQTT_PORT = int(os.getenv("MQTT_PORT", str(contract.TRANSPORT["port"])))
MQTT_QOS = int(os.getenv("MQTT_QOS", str(contract.TRANSPORT["qos"])))
MQTT_KEEPALIVE = int(os.getenv("MQTT_KEEPALIVE", "60"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")

# A camera is a fixed thing bolted to a pole. Its position comes from configuration,
# not from a receiver, which is why these are required rather than defaulted.
CAMERA_ID = os.getenv("CAMERA_ID")
CAMERA_LAT = float(os.getenv("CAMERA_LAT", "0"))
CAMERA_LON = float(os.getenv("CAMERA_LON", "0"))
# Survey noise only. Keep it small: this is a stationary camera, not a vehicle, and a
# large value would smear the map and break any future geofence assumptions.
CAMERA_JITTER_M = float(os.getenv("CAMERA_JITTER_M", "0"))
CAMERA_DIRECTION = os.getenv("CAMERA_DIRECTION") or None
CAMERA_LANE = int(os.getenv("CAMERA_LANE")) if os.getenv("CAMERA_LANE") else None

INBOX_DIR = os.getenv("INBOX_DIR", "/data/inbox")
PROCESSED_DIR = os.getenv("PROCESSED_DIR", "/data/processed")
OUTBOX_DIR = os.getenv("OUTBOX_DIR", "/data/outbox")

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1.0"))
# How long an event may sit awaiting a PUBACK before we consider the ACK lost and
# republish it. Without this a dropped ACK would wedge one event in the outbox forever.
ACK_TIMEOUT_SECONDS = float(os.getenv("ACK_TIMEOUT_SECONDS", "30"))

SCHEMA_VERSION = contract.SCHEMA_VERSION

METRES_PER_DEGREE_LAT = 111_320.0


def metres_per_degree_lon(lat: float) -> float:
    return METRES_PER_DEGREE_LAT * math.cos(math.radians(lat))


def event_topic() -> str:
    return contract.TRANSPORT["topic_pattern"].replace("{camera_id}", CAMERA_ID)


def fatal_if_unconfigured():
    """Refuse to start rather than publish to a topic the ACL will deny.

    Without this a missing CAMERA_ID silently becomes anpr/None/sightings and every
    event is dropped at the broker with no local clue why.
    """
    missing = [
        name
        for name, value in (
            ("CAMERA_ID", CAMERA_ID),
            ("CAMERA_LAT", os.getenv("CAMERA_LAT")),
            ("CAMERA_LON", os.getenv("CAMERA_LON")),
        )
        if not value
    ]
    if missing:
        sys.exit(f"[edge-agent] FATAL: not configured: {', '.join(missing)}")
    if not -90.0 <= CAMERA_LAT <= 90.0:
        sys.exit(f"[edge-agent] FATAL: CAMERA_LAT {CAMERA_LAT} out of range")
    if not -180.0 <= CAMERA_LON <= 180.0:
        sys.exit(f"[edge-agent] FATAL: CAMERA_LON {CAMERA_LON} out of range")
