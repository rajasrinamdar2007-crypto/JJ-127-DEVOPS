import logging
import random
import re
from datetime import datetime, timezone

from app import contract, settings

log = logging.getLogger(__name__)

_NON_PLATE = re.compile(r"[^A-Za-z0-9]")

# Mirrors the vehicle_type enum in contract/event_contract.json.
_VEHICLE_TYPES = frozenset({"car", "bike", "truck", "bus"})


def clean_plate(raw) -> str | None:
    """OD_INTERFACE.md rule 1: the plate, not the description.

    'mh 12 ab 1234' -> 'MH12AB1234'. Returns None if nothing usable survives, so the
    caller drops the detection instead of publishing an empty plate.
    """
    if not isinstance(raw, str):
        return None
    plate = _NON_PLATE.sub("", raw).upper()
    return plate or None


def clean_confidence(raw) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if not 0.0 <= float(raw) <= 1.0:
        return None
    return float(raw)


def jitter_position(lat: float, lon: float) -> tuple:
    """Nudge the configured position by up to CAMERA_JITTER_M metres.

    A stationary camera should not jitter at all, so the default is exactly 0. When
    jitter is enabled it is applied in metres and converted to degrees, because
    "0.0001 degrees" means something very different on lat than on lon.
    """
    metres = settings.CAMERA_JITTER_M
    if metres <= 0:
        return lat, lon

    lat = round(lat + random.uniform(-metres, metres) / settings.METRES_PER_DEGREE_LAT, 6)
    lon_scale = settings.metres_per_degree_lon(lat)
    if lon_scale <= 0:
        return lat, lon
    lon = round(lon + random.uniform(-metres, metres) / lon_scale, 6)
    return lat, lon


def to_event(detection: dict) -> tuple:
    """Turn one synthetic detection into a contract sighting.

    Returns (event, None) or (None, reason). The reason is a plain string so the
    watcher can log why a file was set aside without inventing an exception type.
    """
    if not isinstance(detection, dict):
        return None, f"expected a JSON object, got {type(detection).__name__}"

    plate = clean_plate(detection.get("plate_string"))
    if plate is None:
        return None, "plate_string missing or unusable"

    confidence = clean_confidence(detection.get("confidence"))
    if confidence is None:
        return None, f"confidence {detection.get('confidence')!r} missing or outside 0.0-1.0"

    # Microsecond precision on purpose. Ingest dedups on
    # (plate_string, camera_id, timestamp); at whole-second resolution the same plate
    # passing twice in one second would be silently discarded as a duplicate.
    timestamp = datetime.now(timezone.utc).isoformat()
    lat, lon = jitter_position(settings.CAMERA_LAT, settings.CAMERA_LON)

    event = {
        "plate_string": plate,
        "confidence": confidence,
        "camera_id": settings.CAMERA_ID,
        "lat": lat,
        "lon": lon,
        "timestamp": timestamp,
    }

    image_ref = detection.get("image_path")
    if isinstance(image_ref, str) and image_ref:
        event["image_ref"] = image_ref

    direction = detection.get("direction") or settings.CAMERA_DIRECTION
    if direction is not None:
        event["direction"] = direction

    lane = detection.get("lane", settings.CAMERA_LANE)
    if isinstance(lane, int) and not isinstance(lane, bool):
        event["lane"] = lane

    # Forwarded verbatim, and only when it is one of the contract's four classes. A
    # missing or unrecognised value is dropped rather than passed through: the
    # contract enum is the check, and a bad class here would be rejected wholesale by
    # ingest, losing an otherwise valid sighting (plate, position and all) over a field
    # the detector got wrong. Absent is always a safer answer than wrong.
    vehicle_type = detection.get("vehicle_type")
    if vehicle_type in _VEHICLE_TYPES:
        event["vehicle_type"] = vehicle_type
    elif vehicle_type is not None:
        log.warning(
            "Unrecognised vehicle_type %r on a detection; omitting rather than rejecting the sighting",
            vehicle_type,
        )

    # Last line of defence. If this ever fires, the contract and this module have
    # drifted apart, and we would rather hold the file than publish junk.
    problems = contract.validate(event)
    if problems:
        return None, f"built event violates the contract: {'; '.join(problems)}"

    return event, None
