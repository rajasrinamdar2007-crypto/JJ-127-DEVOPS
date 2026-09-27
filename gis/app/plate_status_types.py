"""Plate status presentation registry.

Single source of truth for how a sighting or alert is *presented* (label, map
colour, marker radius). This module decides nothing about truth: ``confidence``
comes straight from the detection and blacklist membership comes from the
``blacklist`` table. It only decides how each of those looks on the map.

Three tiers, mirroring the pattern used elsewhere in this project:

* ``REGISTRY`` -- canonical entries with an explicit label/colour/radius. Every
  status anyone on the platform cares about is listed here.
* Confidence bands the contract permits but nobody registered -- rendered with a
  stable derived colour so they stay visible and distinct.
* Anything else -- rendered with a neutral fallback, so a new status is never
  invisible.

The band thresholds are presentation only. They are *not* a filter on what gets
stored: ingest keeps every sighting it is handed, and the map decides what to
emphasise. Re-tuning a band never requires re-publishing history.

Keys are namespaced (``alert:blacklisted``) so confidence bands and alert statuses
can share one flat registry in the ``/plate-status-types`` response.
"""

from __future__ import annotations

from typing import Any, Iterable

#: Registry entries: key -> {label, color_rgb (0-255 tuple), color_hex, radius,
#: kind}. ``radius`` is the MapLibre circle-radius in pixels.
REGISTRY: dict[str, dict[str, Any]] = {
    "high": {
        "label": "High confidence",
        "color_rgb": (22, 163, 74),
        "color_hex": "#16a34a",
        "radius": 6,
        "kind": "confidence",
    },
    "medium": {
        "label": "Medium confidence",
        "color_rgb": (202, 138, 4),
        "color_hex": "#ca8a04",
        "radius": 5,
        "kind": "confidence",
    },
    "low": {
        "label": "Low confidence",
        "color_rgb": (100, 116, 139),
        "color_hex": "#64748b",
        "radius": 4,
        "kind": "confidence",
    },
    "unknown": {
        "label": "Unknown confidence",
        "color_rgb": (148, 163, 184),
        "color_hex": "#94a3b8",
        "radius": 4,
        "kind": "confidence",
    },
    "alert:blacklisted": {
        "label": "Blacklisted plate",
        "color_rgb": (220, 38, 38),
        "color_hex": "#dc2626",
        "radius": 9,
        "kind": "alert",
    },
    "alert:clear": {
        "label": "Not blacklisted",
        "color_rgb": (148, 163, 184),
        "color_hex": "#94a3b8",
        "radius": 3,
        "kind": "alert",
    },
    # Vehicle class colours. These are the pin colours, so a client that renders from
    # this registry keeps its palette in step with the API instead of hard-coding hex
    # values that drift. A sighting with no vehicle_type falls back to auto_style()
    # under "unknown", which never collides with these four.
    "vehicle:car": {
        "label": "Car",
        "color_rgb": (37, 99, 235),
        "color_hex": "#2563eb",
        "radius": 6,
        "kind": "vehicle",
    },
    "vehicle:bike": {
        "label": "Bike",
        "color_rgb": (5, 150, 105),
        "color_hex": "#059669",
        "radius": 5,
        "kind": "vehicle",
    },
    "vehicle:truck": {
        "label": "Truck",
        "color_rgb": (217, 119, 6),
        "color_hex": "#d97706",
        "radius": 7,
        "kind": "vehicle",
    },
    "vehicle:bus": {
        "label": "Bus",
        "color_rgb": (124, 58, 237),
        "color_hex": "#7c3aed",
        "radius": 8,
        "kind": "vehicle",
    },
}

#: Mirrors the vehicle_type enum in contract/event_contract.json.
VEHICLE_TYPES: tuple[str, ...] = ("car", "bike", "truck", "bus")

#: Confidence bands, highest first. A confidence of 0.9 is "high", 0.7 "medium".
#: Tune these freely: nothing downstream of this dict depends on the values.
CONFIDENCE_BANDS: tuple[tuple[float, str], ...] = (
    (0.85, "high"),
    (0.60, "medium"),
    (0.00, "low"),
)

#: Neutral fallback for a status nothing has been registered for.
_FALLBACK_RGB = (148, 163, 184)
_FALLBACK_HEX = "#94a3b8"
_FALLBACK_RADIUS = 4

#: Compact palette for auto-assigning a stable colour to an unregistered key.
#: Deterministic per name (rolling hash -> palette index) so the same key always
#: gets the same colour across reloads.
_AUTO_PALETTE: list[tuple[int, int, int]] = [
    (37, 99, 235),
    (22, 163, 74),
    (202, 138, 4),
    (13, 148, 136),
    (225, 29, 72),
    (124, 58, 237),
    (76, 29, 149),
    (100, 116, 139),
]


def _stable_index(name: str) -> int:
    total = 0
    for ch in name:
        total = (total * 31 + ord(ch)) & 0x7FFFFFFF
    return total % len(_AUTO_PALETTE)


def auto_style(name: str, in_palette: bool = False) -> dict[str, Any]:
    """Return a deterministic, distinct style for an unregistered status."""
    if in_palette and name:
        color_rgb = _AUTO_PALETTE[_stable_index(name)]
        color_hex = "#{:02x}{:02x}{:02x}".format(*color_rgb)
    else:
        color_rgb = _FALLBACK_RGB
        color_hex = _FALLBACK_HEX
    return {
        "label": name.replace("_", " ").replace(":", " ").title(),
        "color_rgb": color_rgb,
        "color_hex": color_hex,
        "radius": _FALLBACK_RADIUS,
        "kind": "confidence",
    }


def style_for(key: str) -> dict[str, Any]:
    """Return the presentation style for a single status key (never raises)."""
    normalised = (key or "").lower()
    if normalised in REGISTRY:
        return dict(REGISTRY[normalised])
    if normalised:
        return auto_style(normalised, in_palette=True)
    return auto_style(normalised, in_palette=False)


def confidence_band(confidence: Any) -> str:
    """Map a raw confidence onto a registry key.

    A null or non-numeric confidence is "unknown" rather than "low": a missing
    score is a different statement from a bad one, and the map should not imply
    the detector was unsure when it actually said nothing.
    """
    if confidence is None or isinstance(confidence, bool):
        return "unknown"
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return "unknown"
    for threshold, key in CONFIDENCE_BANDS:
        if value >= threshold:
            return key
    return "unknown"


def style_for_confidence(confidence: Any) -> dict[str, Any]:
    return dict(REGISTRY[confidence_band(confidence)])


def alert_key(is_blacklisted: bool) -> str:
    return "alert:blacklisted" if is_blacklisted else "alert:clear"


def style_for_alert(is_blacklisted: bool) -> dict[str, Any]:
    return dict(REGISTRY[alert_key(bool(is_blacklisted))])


def vehicle_key(vehicle_type: Any) -> str | None:
    """Namespace a raw vehicle_type, or None when there is nothing to style.

    None means the detector could not classify the vehicle, which is a legitimate
    stored value rather than an error - so the caller gets None and decides whether to
    fall back, instead of being handed a confident-looking colour for "unknown".
    """
    if not isinstance(vehicle_type, str):
        return None
    normalised = vehicle_type.strip().lower()
    if not normalised or normalised == "null":
        return None
    return f"vehicle:{normalised}"


def style_for_vehicle(vehicle_type: Any) -> dict[str, Any]:
    key = vehicle_key(vehicle_type)
    if key is None:
        return auto_style("", in_palette=False)
    return style_for(key)


def serialize(meta: dict[str, Any]) -> dict[str, Any]:
    """Flatten a registry entry into plain JSON types (tuples are not JSON-safe)."""
    return {
        "label": meta["label"],
        "color_rgb": list(meta["color_rgb"]),
        "color_hex": meta["color_hex"],
        "radius": meta["radius"],
        "kind": meta["kind"],
    }


def merged_registry(extra_keys: Iterable[str] = ()) -> dict[str, dict[str, Any]]:
    """Return the full registry for the UI: registered + any extra keys.

    ``extra_keys`` lets the caller fold in statuses discovered at runtime (a new
    band, say) so they render with a stable colour before anyone registers them.
    """
    merged: dict[str, dict[str, Any]] = {key: style_for(key) for key in REGISTRY}
    for key in extra_keys:
        if key and key not in merged:
            merged[key] = style_for(key)
    return merged


__all__ = [
    "REGISTRY",
    "CONFIDENCE_BANDS",
    "VEHICLE_TYPES",
    "alert_key",
    "auto_style",
    "confidence_band",
    "merged_registry",
    "serialize",
    "style_for",
    "style_for_alert",
    "style_for_confidence",
    "style_for_vehicle",
    "vehicle_key",
]
