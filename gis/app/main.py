"""GIS API service.

Serves the plate-sighting feed, per-plate trajectories and the blacklist alert
feed to the frontend (MapLibre), plus a read-only database viewer. Reads from the
shared PostGIS ``sightings``, ``alerts`` and ``blacklist`` tables.

Schema is fixed by ``db/init.sql`` and is intentionally left untouched. Fields the
UI shows that have no backing column -- the confidence band, the alert status,
its colour and marker radius -- are computed here from
``plate_status_types``. Those are presentation only and never filter what is
stored: ingest keeps everything it is handed, and a band can be re-tuned without
re-publishing history.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from psycopg import sql

from app import db, plate_status_types, settings

router = APIRouter()

#: Same rule the edge agent applies before publishing (OD_INTERFACE.md rule 1), so
#: /plates/mh12ab1234 and /plates/MH%2012%20AB%201234 both find the same plate.
_NON_PLATE = re.compile(r"[^A-Za-z0-9]")


def _normalize_plate(raw: str) -> str:
    return _NON_PLATE.sub("", raw or "").upper()


def _parse_since(value: str | None) -> datetime | None:
    """Parse an optional ISO-8601 filter, rejecting junk with 400 rather than 500."""
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"invalid ISO-8601 timestamp: {value!r}"
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _point(lon: Any, lat: Any) -> dict[str, Any]:
    # GeoJSON is [lon, lat] - the reverse of how we store and log it.
    return {"type": "Point", "coordinates": [lon, lat]}


def _sighting_feature(row: dict[str, Any]) -> dict[str, Any]:
    band = plate_status_types.confidence_band(row.get("confidence"))
    blacklisted = bool(row.get("is_blacklisted"))
    style = plate_status_types.style_for(band)
    vehicle = plate_status_types.style_for_vehicle(row.get("vehicle_type"))

    return {
        "type": "Feature",
        "geometry": _point(row.get("lon"), row.get("lat")),
        "properties": {
            "id": row.get("id"),
            "plate_string": row.get("plate_string"),
            "confidence": row.get("confidence"),
            "confidence_band": band,
            "camera_id": row.get("camera_id"),
            "camera_name": row.get("camera_name"),
            "camera_zone": row.get("camera_zone"),
            "direction": row.get("direction"),
            "lane": row.get("lane"),
            "image_ref": row.get("image_ref"),
            "timestamp": _iso(row.get("timestamp")),
            "received_at": _iso(row.get("received_at")),
            "vehicle_type": row.get("vehicle_type"),
            "vehicle_color_hex": vehicle["color_hex"],
            "vehicle_color_rgb": list(vehicle["color_rgb"]),
            "is_blacklisted": blacklisted,
            "alert_status": plate_status_types.alert_key(blacklisted),
            "color_hex": style["color_hex"],
            "color_rgb": list(style["color_rgb"]),
            "radius": style["radius"],
        },
    }


@router.get("/health")
def health() -> dict[str, Any]:
    db_ok = db.ping()
    return {
        "status": "ok" if db_ok else "degraded",
        "service": "gis",
        "db": db_ok,
    }


@router.get("/plate-status-types")
def plate_status_types_endpoint() -> dict[str, dict]:
    """Return the presentation registry so the map legend is driven by the API.

    One flat, namespaced registry (confidence bands plus alert statuses) rather
    than hard-coded colours in the frontend, so re-tuning a band is a deploy of
    this service and not of the map.
    """
    return {
        key: plate_status_types.serialize(meta)
        for key, meta in sorted(plate_status_types.merged_registry().items())
    }


@router.get("/cameras")
def cameras() -> dict[str, Any]:
    """Return the registered camera network, for map markers and legends.

    As a GeoJSON FeatureCollection so a client can hand it straight to deck.gl or
    MapLibre as a source, the same shape as every other feed here.
    """
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, name, zone, lat, lon FROM cameras ORDER BY id")
            rows = cur.fetchall()
    except Exception as exc:  # pragma: no cover - DB unavailable
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}")

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": _point(row.get("lon"), row.get("lat")),
                "properties": {
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "zone": row.get("zone"),
                },
            }
            for row in rows
        ],
    }


@router.get("/sightings/geojson")
def sightings_geojson(
    since: str | None = Query(default=None, description="ISO timestamp; only sightings at/after this"),
    limit: int = Query(default=settings.DEFAULT_SIGHTING_LIMIT, ge=1, le=settings.MAX_SIGHTING_LIMIT),
) -> dict[str, Any]:
    """Return recent sightings as a GeoJSON FeatureCollection, newest first."""
    since_at = _parse_since(since)

    # EXISTS, not a JOIN: a plate can be blacklisted for several reasons (the
    # blacklist PK is (plate_string, reason)), so a join would duplicate sightings.
    # The cameras LEFT JOIN cannot duplicate: cameras.id is a primary key.
    sql_text = """
        SELECT s.id, s.plate_string, s.confidence, s.camera_id, s.lat, s.lon,
               s.timestamp, s.image_ref, s.direction, s.lane, s.received_at,
               s.vehicle_type,
               c.name AS camera_name, c.zone AS camera_zone,
               EXISTS (
                   SELECT 1 FROM blacklist b WHERE b.plate_string = s.plate_string
               ) AS is_blacklisted
        FROM sightings s
        LEFT JOIN cameras c ON c.id = s.camera_id
    """
    params: dict[str, Any] = {"limit": limit}
    if since_at is not None:
        sql_text += " WHERE s.timestamp >= %(since)s"
        params["since"] = since_at
    sql_text += " ORDER BY s.timestamp DESC LIMIT %(limit)s"

    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(sql_text, params)
            rows = cur.fetchall()
    except Exception as exc:  # pragma: no cover - DB unavailable
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}")

    return {
        "type": "FeatureCollection",
        "features": [_sighting_feature(row) for row in rows],
    }


@router.get("/plates/{plate_string}/trajectory")
def plate_trajectory(
    plate_string: str,
    limit: int = Query(default=settings.MAX_TRAJECTORY_POINTS, ge=2, le=settings.MAX_TRAJECTORY_POINTS),
) -> dict[str, Any]:
    """Return one plate's sightings in chronological order, ready to animate.

    The FeatureCollection holds a Point feature per sighting (so MapLibre can drive
    a symbol layer or a playback cursor) plus, when there are at least two points,
    a single LineString feature for the path taken.

    TODO(fuzzy): this is an exact ``plate_string`` match. ``pg_trgm`` is already
    enabled in ``db/init.sql`` for exactly this, so near-miss lookups
    ("MH12AB1234" vs "MH12AB1Z34") can be added with a
    ``plate_string % %(query)s`` clause -- but that also needs a
    ``CREATE INDEX ... USING GIN (plate_string gin_trgm_ops)`` in init.sql, which
    does not exist yet. Without the index a trigram search is a sequential scan,
    so the two changes must land together rather than the query alone.
    """
    plate = _normalize_plate(plate_string)
    if not plate:
        raise HTTPException(status_code=400, detail="plate_string contained no usable characters")

    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.lat, s.lon, s.timestamp, s.camera_id, s.confidence,
                       s.vehicle_type,
                       c.name AS camera_name, c.zone AS camera_zone
                FROM sightings s
                LEFT JOIN cameras c ON c.id = s.camera_id
                WHERE s.plate_string = %(plate)s
                ORDER BY s.timestamp ASC
                LIMIT %(limit)s
                """,
                {"plate": plate, "limit": limit},
            )
            rows = cur.fetchall()
    except Exception as exc:  # pragma: no cover - DB unavailable
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}")

    if not rows:
        raise HTTPException(status_code=404, detail=f"no sightings for plate {plate}")

    features: list[dict[str, Any]] = []
    for row in rows:
        band = plate_status_types.confidence_band(row.get("confidence"))
        features.append(
            {
                "type": "Feature",
                "geometry": _point(row.get("lon"), row.get("lat")),
                "properties": {
                    "id": row.get("id"),
                    "plate_string": plate,
                    "camera_id": row.get("camera_id"),
                    "camera_name": row.get("camera_name"),
                    "camera_zone": row.get("camera_zone"),
                    "confidence": row.get("confidence"),
                    "confidence_band": band,
                    "vehicle_type": row.get("vehicle_type"),
                    "timestamp": _iso(row.get("timestamp")),
                },
            }
        )

    first_seen = rows[0].get("timestamp")
    last_seen = rows[-1].get("timestamp")
    duration = None
    if isinstance(first_seen, datetime) and isinstance(last_seen, datetime):
        duration = round((last_seen - first_seen).total_seconds(), 3)

    # A GeoJSON LineString needs at least two positions, so a single sighting gets
    # its Point feature and no path. Emitting a 1-point LineString would make the
    # whole FeatureCollection invalid for strict validators and MapLibre.
    if len(rows) >= 2:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[row.get("lon"), row.get("lat")] for row in rows],
                },
                "properties": {
                    "kind": "trajectory",
                    "plate_string": plate,
                    "point_count": len(rows),
                    "first_seen": _iso(first_seen),
                    "last_seen": _iso(last_seen),
                    "duration_seconds": duration,
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "plate": plate,
        "point_count": len(rows),
        "first_seen": _iso(first_seen),
        "last_seen": _iso(last_seen),
        "duration_seconds": duration,
        "features": features,
    }


@router.get("/alerts")
def alerts(
    since: str | None = Query(default=None, description="ISO timestamp; only alerts at/after this"),
    limit: int = Query(default=settings.DEFAULT_ALERT_LIMIT, ge=1, le=1000),
) -> dict[str, Any]:
    """Return recent blacklist-match alerts as a GeoJSON FeatureCollection.

    ``reasons`` is aggregated from the ``blacklist`` table at read time. The
    ``alerts`` table deliberately does not denormalise it, so a reason edited or
    added later is reflected here without a backfill.
    """
    since_at = _parse_since(since)

    sql_text = """
        SELECT a.id, a.plate_string, a.matched_at, a.camera_id, a.lat, a.lon,
               COALESCE(
                   (SELECT array_agg(b.reason ORDER BY b.reason)
                    FROM blacklist b WHERE b.plate_string = a.plate_string),
                   ARRAY[]::text[]
               ) AS reasons
        FROM alerts a
    """
    params: dict[str, Any] = {"limit": limit}
    if since_at is not None:
        sql_text += " WHERE a.matched_at >= %(since)s"
        params["since"] = since_at
    sql_text += " ORDER BY a.matched_at DESC LIMIT %(limit)s"

    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(sql_text, params)
            rows = cur.fetchall()
    except Exception as exc:  # pragma: no cover - DB unavailable
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}")

    style = plate_status_types.style_for_alert(True)
    features = [
        {
            "type": "Feature",
            "geometry": _point(row.get("lon"), row.get("lat")),
            "properties": {
                "id": row.get("id"),
                "plate_string": row.get("plate_string"),
                "camera_id": row.get("camera_id"),
                "matched_at": _iso(row.get("matched_at")),
                "reasons": list(row.get("reasons") or []),
                "alert_status": plate_status_types.alert_key(True),
                "label": style["label"],
                "color_hex": style["color_hex"],
                "color_rgb": list(style["color_rgb"]),
                "radius": style["radius"],
            },
        }
        for row in rows
    ]

    return {"type": "FeatureCollection", "features": features}


@router.get("/tables")
def tables() -> list[str]:
    """List whitelisted tables exposed by the read-only viewer."""
    return list(settings.ALLOWED_TABLES)


@router.get("/table/{table_name}")
def table_data(
    table_name: str,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    """Return rows from a whitelisted table for the database viewer.

    PostGIS geometry/geography columns are rendered as WKT text instead of raw
    binary so the viewer stays legible.
    """
    if table_name not in settings.ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail="unknown table")

    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT f_geography_column AS col
                FROM geography_columns
                WHERE f_table_name = %s
                UNION
                SELECT f_geometry_column AS col
                FROM geometry_columns
                WHERE f_table_name = %s
                """,
                (table_name, table_name),
            )
            spatial = {row["col"] for row in cur.fetchall()}

            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table_name,),
            )
            columns = [row["column_name"] for row in cur.fetchall()]

            exprs = [
                sql.SQL("ST_AsText({0}) AS {0}").format(sql.Identifier(col))
                if col in spatial
                else sql.SQL("{0} AS {0}").format(sql.Identifier(col))
                for col in columns
            ]

            # Identifier-wrapped, not string-interpolated: the whitelist already
            # gates the table, but this endpoint is the one place a request path
            # reaches SQL, so it should not depend on a single check.
            query = sql.SQL("SELECT {0} FROM {1} ORDER BY 1 DESC LIMIT %s").format(
                sql.SQL(", ").join(exprs), sql.Identifier(table_name)
            )
            cur.execute(query, (limit,))
            return cur.fetchall()
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - DB unavailable / query error
        raise HTTPException(status_code=500, detail=f"query failed: {exc}")


def create_app() -> FastAPI:
    app = FastAPI(title="GIS API", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
