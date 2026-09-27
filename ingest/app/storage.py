import logging

import psycopg

from app import settings

log = logging.getLogger(__name__)

_INSERT_SIGHTING = """
    INSERT INTO sightings (
        plate_string, confidence, camera_id, lat, lon, timestamp,
        image_ref, direction, lane, vehicle_type
    )
    VALUES (
        %(plate_string)s, %(confidence)s, %(camera_id)s, %(lat)s, %(lon)s, %(timestamp)s,
        %(image_ref)s, %(direction)s, %(lane)s, %(vehicle_type)s
    )
    ON CONFLICT (plate_string, camera_id, timestamp) DO NOTHING
"""

_BLACKLIST_LOOKUP = """
    SELECT 1 FROM blacklist WHERE plate_string = %(plate_string)s LIMIT 1
"""

_INSERT_ALERT = """
    INSERT INTO alerts (plate_string, matched_at, camera_id, lat, lon)
    VALUES (%(plate_string)s, now(), %(camera_id)s, %(lat)s, %(lon)s)
"""


def _connect():
    return psycopg.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
        dbname=settings.POSTGRES_DB,
    )


def ping() -> bool:
    """Cheap liveness probe for the DB, so /health cannot report ok while we are useless."""
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:
        log.warning("Database ping failed", exc_info=True)
        return False


def save_sighting(sighting: dict) -> str:
    """Store one sighting, then alert if its plate is blacklisted.

    Returns "inserted", "duplicate" or "alerted".

    The blacklist lookup and the alert insert deliberately share the sighting's
    transaction: an alert must never exist for a sighting that was not stored, and a
    stored sighting must never silently skip its alert.

    Dedup falls out of the early return on "duplicate" - a QoS 1 redelivery of a
    blacklisted sighting does not raise a second alert.
    """
    params = {
        "plate_string": sighting.get("plate_string"),
        "confidence": sighting.get("confidence"),
        "camera_id": sighting.get("camera_id"),
        "lat": sighting.get("lat"),
        "lon": sighting.get("lon"),
        "timestamp": sighting.get("timestamp"),
        "image_ref": sighting.get("image_ref"),
        "direction": sighting.get("direction"),
        "lane": sighting.get("lane"),
        "vehicle_type": sighting.get("vehicle_type"),
    }

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_INSERT_SIGHTING, params)
            if cur.rowcount == 0:
                log.info(
                    "Duplicate sighting dropped: %s (%s @ %s)",
                    params["plate_string"],
                    params["camera_id"],
                    params["timestamp"],
                )
                return "duplicate"

            cur.execute(_BLACKLIST_LOOKUP, params)
            blacklisted = cur.fetchone() is not None

            if blacklisted:
                cur.execute(_INSERT_ALERT, params)
        conn.commit()

    if blacklisted:
        log.warning(
            "BLACKLIST MATCH %s seen at %s (%s)",
            params["plate_string"],
            params["camera_id"],
            params["timestamp"],
        )
        return "alerted"

    log.info(
        "Stored %s (%s conf=%.2f)",
        params["plate_string"],
        params["camera_id"],
        params["confidence"],
    )
    return "inserted"
