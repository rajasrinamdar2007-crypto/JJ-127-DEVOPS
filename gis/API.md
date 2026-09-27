# GIS API

Read-only HTTP over the PostGIS tables `ingest` writes. This is the only data source the
front end needs: it renders sightings, per-plate trajectories and blacklist alerts, and
nothing here mutates the database.

Every example below is verbatim output from a running service, not hand-written.

## Conventions

- **Routes are unprefixed.** There is no `/api` on this service. The dashboard calls a
  relative `/api/...`, but that prefix is added by the reverse proxy in front of it (nginx in
  the container, or `vite.config.ts` in dev), which strips it again before forwarding here.
  The service answers `/sightings/geojson`, never `/api/sightings/geojson`. See
  [CORS](#cors) for why the front end does it that way.
- **GeoJSON, not a bespoke envelope.** `/sightings/geojson`, `/plates/{plate}/trajectory`,
  `/alerts` and `/cameras` all return `FeatureCollection`, so they drop straight into MapLibre
  `GeoJSONSource` without a transform layer.
- **Field names are the API's, not the UI's.** `plate_string` rather than `plate_number`,
  `timestamp` rather than `detected_at`, `is_blacklisted` rather than `is_flagged`,
  `camera_zone` rather than `zone`, and `confidence` on a `0.0`–`1.0` scale rather than a
  `0`–`100` percentage. Clients use these names directly; a translation layer would be one
  more thing to keep in step with the API forever.
- **Coordinates are `[lon, lat]`** — GeoJSON order, the reverse of the `lat`/`lon` column
  order used in the database and in the logs. This is the single most common way to get a
  map that plots in the Indian Ocean instead of Pune.
- **Timestamps are ISO-8601 with an explicit offset.** The database is `timestamptz`;
  responses carry `+00:00` rather than a bare `Z`, and always carry an offset, so
  `new Date(...)` parses without guessing.
- **Presentation fields are computed here, not stored.** `confidence_band`, `color_hex`,
  `color_rgb`, `radius` and `alert_status` have no backing column. They are derived from
  `confidence`, `vehicle_type` and blacklist membership so the map legend is a deploy of this
  service rather than a rebuild of the front end. Re-tuning a band never requires re-publishing
  history — and, importantly, never filters or discards stored sightings.
- **`?since=` takes ISO-8601.** A naive timestamp (`2026-09-26T12:00:00`) is assumed UTC.
  A trailing `Z` is accepted. Anything unparseable is a `400`, not a `500`.
- **Every feed is bounded.** Omitting `?limit=` returns a capped default, so a client that
  forgets the parameter cannot pull the whole table.
- **This service is read-only.** There is no `POST`/`PUT`/`DELETE` anywhere, and no endpoint
  to flag or unflag a plate. A blacklist change is an `UPDATE` against the `blacklist` table;
  the next sighting picks it up. The dashboard's blacklist column is therefore a label, not
  a control.

## Plate normalisation

`/plates/{plate_string}/trajectory` applies the same normalisation the edge agent applies
before publishing (OD_INTERFACE.md rule 1): non-alphanumerics stripped, uppercased. So all
of these are the same plate and return the same trajectory:

```
/plates/MH12CD5678/trajectory
/plates/mh12cd5678/trajectory
/plates/MH12-CD-5678/trajectory
/plates/MH%2012%20CD%205678/trajectory   -> "MH 12 CD 5678"
```

A path segment that normalises to nothing (e.g. `---`) is a `400`.

---

## `GET /sightings/geojson`

Recent sightings, newest first. Computed fields: `confidence_band` from `confidence`, and
`is_blacklisted` from an `EXISTS` against the `blacklist` table.

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `since` | ISO-8601 string | none | only sightings at/after this instant |
| `limit` | int | `500` | `1`–`5000` |

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": { "type": "Point", "coordinates": [73.78679, 18.52951] },
      "properties": {
        "id": 3,
        "plate_string": "KA05EE4321",
        "confidence": 0.38,
        "confidence_band": "low",
        "camera_id": "cam-pune-baner-02",
        "direction": null,
        "lane": null,
        "image_ref": "/data/frames/e.jpg",
        "timestamp": "2026-09-26T13:05:40.111381+00:00",
        "received_at": "2026-09-26T13:05:42.153470+00:00",
        "is_blacklisted": false,
        "alert_status": "alert:clear",
        "color_hex": "#64748b",
        "color_rgb": [100, 116, 139],
        "radius": 4
      }
    }
  ]
}
```

`properties`:

| Field | Type | Source |
| --- | --- | --- |
| `id` | int | `sightings.id` |
| `plate_string` | string | normalised at the edge |
| `confidence` | float | raw detector score, `0.0`–`1.0` |
| `confidence_band` | string | computed — see [Presentation registry](#presentation-registry) |
| `camera_id` | string | which camera saw it |
| `camera_name` | string \| null | joined from `cameras`; `null` if the camera is not registered |
| `camera_zone` | string \| null | joined from `cameras`; `null` if unregistered or the camera has no zone |
| `direction` | string \| null | `N`/`S`/`E`/`W`/`NE`/`NW`/`SE`/`SW`, nullable |
| `lane` | int \| null | nullable |
| `image_ref` | string \| null | nullable, path only — no image is served |
| `timestamp` | string | when the sighting happened |
| `received_at` | string | when ingest stored it; `timestamp` is what you animate on |
| `vehicle_type` | string \| null | `car`/`bike`/`truck`/`bus`, or `null` when the detector could not classify it |
| `vehicle_color_hex` / `vehicle_color_rgb` | string / int[3] | presentation, per `vehicle_type` |
| `is_blacklisted` | bool | computed from `blacklist` |
| `alert_status` | string | `alert:blacklisted` or `alert:clear` |
| `color_hex` / `color_rgb` / `radius` | — | presentation, per confidence band |

`camera_name`/`camera_zone` come from a `LEFT JOIN` on `cameras.id`, so a sighting from an
unregistered camera is still returned in full — the metadata is `null`, not the row. There is no
foreign key from `sightings.camera_id` to `cameras.id` for the same reason: a camera can be
misconfigured and still produce sightings, and losing them at write time would be worse than
labelling them as unregistered.

An empty result is `{"type": "FeatureCollection", "features": []}` with a `200` — not a
`404`. "Nothing recent" is a normal state for a live map, not an error.

---

## `GET /plates/{plate_string}/trajectory`

One plate's sightings in **ascending** chronological order — the order a map animates
through. Returns timestamped `Point` features for every sighting, plus a single
`LineString` feature carrying `kind: "trajectory"` summarising the path.

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `limit` | int | `2000` | `2`–`2000` |

```json
{
  "type": "FeatureCollection",
  "plate": "MH12CD5678",
  "point_count": 3,
  "first_seen": "2026-09-26T13:05:40.082922+00:00",
  "last_seen": "2026-09-26T13:05:40.101730+00:00",
  "duration_seconds": 0.019,
  "features": [
    {
      "type": "Feature",
      "geometry": { "type": "Point", "coordinates": [73.786817, 18.529482] },
      "properties": {
        "id": 2,
        "plate_string": "MH12CD5678",
        "camera_id": "cam-pune-baner-02",
        "confidence": 0.88,
        "confidence_band": "high",
        "timestamp": "2026-09-26T13:05:40.082922+00:00"
      }
    }
  ]
}
```

`plate`, `point_count`, `first_seen`, `last_seen` and `duration_seconds` are GeoJSON foreign
members on the collection. MapLibre ignores them; they save a client from re-deriving the
playback window. The `LineString` feature repeats them in its `properties` so the path is
self-describing when inspected alone:

```json
{
  "kind": "trajectory",
  "plate_string": "MH12CD5678",
  "point_count": 3,
  "first_seen": "2026-09-26T13:05:40.082922+00:00",
  "last_seen": "2026-09-26T13:05:40.101730+00:00",
  "duration_seconds": 0.019
}
```

Two behaviours worth designing around:

- **A single sighting returns no `LineString` at all.** A `LineString` needs at least two
  positions; emitting a one-position one makes the whole `FeatureCollection` invalid for
  strict validators and MapLibre. You still get the `Point`.
- **`?limit` is capped below at `2`,** so `?limit=1` is a `422`. For "just the latest
  position", use `/sightings/geojson?since=…` rather than truncating a trajectory.

`404` when the plate has no sightings at all:

```json
{ "detail": "no sightings for plate NOSUCH" }
```

### Not implemented: fuzzy plate matching

Matching is **exact** on the normalised string. `pg_trgm` is already enabled in
`db/init.sql` for this, but no GIN trigram index exists yet, so a trigram search today would
be a sequential scan. A fuzzy `plate_string % :query` clause and its
`CREATE INDEX ... USING GIN (plate_string gin_trgm_ops)` must land together. See the TODO in
`gis/app/main.py`.

---

## `GET /alerts`

Blacklist matches, newest first. `reasons` is aggregated from `blacklist` at read time.

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `since` | ISO-8601 string | none | only alerts at/after this instant |
| `limit` | int | `200` | `1`–`1000` |

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": { "type": "Point", "coordinates": [73.738894, 18.59129] },
      "properties": {
        "id": 1,
        "plate_string": "MH99ZZ0001",
        "camera_id": "cam-pune-hinjewadi-01",
        "matched_at": "2026-09-26T13:05:42.183212+00:00",
        "reasons": ["stolen vehicle", "unpaid toll"],
        "alert_status": "alert:blacklisted",
        "label": "Blacklisted plate",
        "color_hex": "#dc2626",
        "color_rgb": [220, 38, 38],
        "radius": 9
      }
    }
  ]
}
```

The `alerts` table stores no `reason` and no geometry. Rather than denormalise either onto
every alert row, the reason is read from `blacklist` and the `Point` is built here — so
adding or editing a blacklist reason is reflected immediately, with no backfill. The
presentation fields are fixed to the blacklisted style: on this endpoint, *every* feature is
an alert.

---

## `GET /cameras`

The camera registry, so the map can label its own network instead of hard-coding camera
positions. A GeoJSON `FeatureCollection` of `Point`s, ordered by `id`.

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `limit` | int | `100` | `1`–`1000` |

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": { "type": "Point", "coordinates": [73.7868, 18.5295] },
      "properties": { "id": "cam-pune-baner-02", "name": "Baner Junction", "zone": "West" }
    }
  ]
}
```

`properties` is just `id`, `name` and `zone` — there are no presentation fields here,
deliberately: a camera marker is a fixed icon, not a status, so there is nothing for the
[registry](#plate-status-types) to say about it.

The registry is seeded on first boot from the `CAM*_ID` / `_NAME` / `_ZONE` / `_LAT` / `_LON`
variables in `.env` by `db/zz-cameras.sh`, which the same `.env` feeds to the edge agents. The
agents and the API therefore cannot disagree about which cameras exist. Re-seeding only happens
on an empty volume: change a camera's name and you need `make reset`, or a direct `UPDATE`.

---

## `GET /plate-status-types`

The presentation registry, so the map legend is driven by the API rather than hard-coded in
the front end. One flat, namespaced object: confidence bands, alert statuses and vehicle
classes share a single namespace so a client can iterate it to build a legend.

```json
{
  "alert:blacklisted": { "label": "Blacklisted plate", "color_rgb": [220, 38, 38], "color_hex": "#dc2626", "radius": 9, "kind": "alert" },
  "alert:clear":       { "label": "Not blacklisted",   "color_rgb": [148, 163, 184], "color_hex": "#94a3b8", "radius": 3, "kind": "alert" },
  "high":              { "label": "High confidence",    "color_rgb": [22, 163, 74],  "color_hex": "#16a34a", "radius": 6, "kind": "confidence" },
  "low":               { "label": "Low confidence",     "color_rgb": [100, 116, 139],"color_hex": "#64748b", "radius": 4, "kind": "confidence" },
  "medium":            { "label": "Medium confidence",  "color_rgb": [202, 138, 4],  "color_hex": "#ca8a04", "radius": 5, "kind": "confidence" },
  "unknown":           { "label": "Unknown confidence", "color_rgb": [148, 163, 184],"color_hex": "#94a3b8", "radius": 4, "kind": "confidence" },
  "vehicle:bike":      { "label": "Bike",               "color_rgb": [5, 150, 105],  "color_hex": "#059669", "radius": 5, "kind": "vehicle" },
  "vehicle:bus":       { "label": "Bus",                "color_rgb": [124, 58, 237], "color_hex": "#7c3aed", "radius": 8, "kind": "vehicle" },
  "vehicle:car":       { "label": "Car",                "color_rgb": [37, 99, 235],  "color_hex": "#2563eb", "radius": 6, "kind": "vehicle" },
  "vehicle:truck":     { "label": "Truck",              "color_rgb": [217, 119, 6],  "color_hex": "#d97706", "radius": 7, "kind": "vehicle" }
}
```

Every value has `label`, `color_rgb` (0–255 triple, for MapLibre's `circle-color`),
`color_hex`, `radius` (MapLibre `circle-radius` in px) and `kind` (`confidence`, `alert` or
`vehicle`).

The keys are namespaced by `kind`, so a client can select by prefix: unprefixed keys
(`high`/`medium`/`low`/`unknown`) are confidence bands, `alert:*` are alert states, and
`vehicle:*` are vehicle classes. A sighting with `vehicle_type: null` matches **no**
`vehicle:*` key — there is deliberately no `vehicle:unknown` entry, because inventing a
"no vehicle" class would suggest the detector reported one.

### How `confidence` becomes a band

Boundaries live in `gis/app/plate_status_types.py` (`CONFIDENCE_BANDS`):

| `confidence` | band |
| --- | --- |
| `>= 0.85` | `high` |
| `>= 0.60` | `medium` |
| `>= 0.00` | `low` |
| `null` / non-numeric | `unknown` |

A missing confidence is `unknown`, not `low` — "the detector said nothing" is a different
statement from "the detector was unsure", and the map should not conflate them. The column
is `NOT NULL` today, so `unknown` is defensive rather than reachable via this endpoint.

An unregistered status key still renders: it gets a stable colour derived from a hash of its
name, so a new band appears with a consistent colour across reloads instead of vanishing.

---

## `GET /health`

```json
{ "status": "ok", "service": "gis", "db": true }
```

`status` is `degraded` and `db` is `false` if the database is unreachable — it is a real
`SELECT 1` probe, not a static string, so this cannot report `ok` while the API is useless.
Still `200` when degraded, which is what a container healthcheck wants.

---

## `GET /tables` and `GET /table/{table_name}`

A read-only row viewer for debugging, over a fixed whitelist.

`GET /tables` → `["sightings", "alerts", "blacklist"]`

This one endpoint reads no data: it echoes the `ALLOWED_TABLES` config. It therefore keeps
returning `200` while the database is down, unlike every other route. Don't read that as
the API being healthy — `/health` is the signal.

`GET /table/{table_name}` takes `?limit=` (default `200`, `1`–`1000`) and returns an array of
row objects, newest first. PostGIS `geometry`/`geography` columns are rendered as WKT text
rather than raw hex, so they are readable:

```json
[{ "location": "POINT(73.808 18.5079)", "timestamp": "2026-09-26T12:36:48.570219Z", "plate_string": "MH99ZZ0001", "id": 14, "confidence": 0.71, "camera_id": "cam-pune-kothrud-03", "direction": "N", "lane": 1, "image_ref": null, "lat": 18.5079, "lon": 73.808, "received_at": "2026-09-26T13:01:48.570219Z" }]
```

`404` for anything not whitelisted, including injection attempts —
`/table/blacklist%3Bdrop%20table%20sightings` is a `404`, not a `500`. The whitelist is the
primary gate; the table name is additionally passed as a `psycopg.sql.Identifier`, so this
endpoint does not depend on a single check being correct.

---

## Status codes

| Code | When |
| --- | --- |
| `200` | success, **including an empty `FeatureCollection`** |
| `400` | unparseable `since`; a plate path segment that normalises to nothing |
| `404` | plate has no sightings; `table/{name}` not whitelisted |
| `422` | `?limit=` out of range, including `trajectory?limit=1` |
| `500` | a whitelisted table query failed |
| `503` | database unreachable on a data endpoint (`/tables` excepted — see above) |

Verified by stopping the database under the running service: `/health` returned
`200 {"status":"degraded","db":false}` while `/sightings/geojson`, `/alerts` and
`/plates/{plate}/trajectory` all returned `503`.

`400` bodies are `{"detail": "invalid ISO-8601 timestamp: 'garbage'"}` and
`{"detail": "plate_string contained no usable characters"}`; `404` on a plate is
`{"detail": "no sightings for plate NOSUCH"}`.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `POSTGRES_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_DB` | `db` / `5432` / `anpruser` / `REDACTED` / `anpr` | database connection |
| `ALLOWED_TABLES` | `sightings,alerts,blacklist,cameras` | tables `/table/{name}` may expose |
| `DEFAULT_SIGHTING_LIMIT` | `500` | default `?limit=` on `/sightings/geojson` |
| `MAX_SIGHTING_LIMIT` | `5000` | hard ceiling on the same |
| `DEFAULT_ALERT_LIMIT` | `200` | default `?limit=` on `/alerts` |
| `MAX_TRAJECTORY_POINTS` | `2000` | default **and** ceiling on `trajectory?limit=` |
| `CORS_ORIGINS` | localhost `5173`/`8001` origins | comma-separated; see below |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | bind address |

### CORS

The service sends permissive CORS headers for the origins in `CORS_ORIGINS`. **The front end
should not depend on this.** Serving the SPA and proxying the API from the same nginx origin
is the intended path, and it makes CORS irrelevant for the browser: a reverse proxy is not a
cross-origin request, so no preflight is issued and the allowlist is never consulted.

In concrete terms, the dashboard at `http://localhost:8080` requests
`http://localhost:8080/api/sightings/geojson`, nginx rewrites that to
`http://gis:8000/sightings/geojson`, and the browser only ever sees one origin. `CORS_ORIGINS`
is never read on that path.

Keep `CORS_ORIGINS` for direct hits against the API during development — a Vite dev server on
`5173` talking straight to the API — and widen it only as far as that requires. It is not a
security control, and no `ALLOWED_ORIGINS` variable exists; the name is `CORS_ORIGINS`,
matching the rest of the project.

Note that `vite.config.ts` proxies `/api` in development too, so even the dev server does not
normally need CORS. `CORS_ORIGINS` matters only if you point `VITE_API_URL` at an absolute
cross-origin URL.

## Not implemented

Known gaps, so a client is not written against something that does not exist yet:

- **No `bbox` or radius filter.** `/sightings/geojson` has no spatial predicate, so a
  zoomed-out map refetches everything and the GIST index on `sightings.location` goes
  unused. `ST_Intersects` or `ST_DWithin` is the natural next endpoint.
- **No heatmap endpoint.**
- **No fuzzy plate matching** (see above).
- **No pagination cursors.** `?limit=` truncates; there is no way to page past the cap.
- **No WebSocket or SSE feed.** Clients poll.
- **No image serving.** `image_ref` is a path recorded at the edge; the bytes are not here.
- **No writes at all.** No endpoint flags a plate, edits a camera, or acknowledges an alert.
  The front end treats the blacklist as read-only for this reason.
- **No authentication or authorisation.** Every route is open to anyone who can reach the
  port, and there is no per-camera or per-user scoping. Do not publish this service as-is.

