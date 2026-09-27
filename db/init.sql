-- JJ-127-DEVOPS schema bootstrap.
-- Field-for-field mirror of contract/event_contract.json -> event.fields.
-- If you change one, change all three (this file, the contract, ingest/app/contract.py).

CREATE EXTENSION IF NOT EXISTS postgis;

-- Enabled now, used later: fuzzy plate matching ("MH12AB1234" vs "MH12AB1Z34")
-- needs similarity() and a GIN trgm index. See _registration_notes in the contract.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS sightings (
    id           serial PRIMARY KEY,
    plate_string text NOT NULL,
    confidence   double precision NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    camera_id    text NOT NULL,
    lat          double precision NOT NULL CHECK (lat  >= -90.0 AND lat  <= 90.0),
    lon          double precision NOT NULL CHECK (lon  >= -180.0 AND lon <= 180.0),
    timestamp    timestamptz NOT NULL,
    image_ref    text,
    direction    text CHECK (direction IS NULL OR direction IN ('N','S','E','W','NE','NW','SE','SW')),
    lane         int,
    -- Supplied by the detector, never inferred here. NULL means "the detector could not
    -- classify this vehicle" and is a legitimate, honest value - see OD_INTERFACE.md.
    vehicle_type text CHECK (vehicle_type IS NULL OR vehicle_type IN ('car','bike','truck','bus')),
    received_at  timestamptz NOT NULL DEFAULT now(),
    location     geometry(Point, 4326)
        GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED,
    -- Dedup key. QoS 1 is at-least-once, so the broker WILL redeliver; this is what
    -- makes storage.py's ON CONFLICT a no-op instead of a duplicate row.
    CONSTRAINT sightings_dedup_key UNIQUE (plate_string, camera_id, timestamp)
);

-- Plate history in reverse-chronological order; also serves plain plate lookups.
CREATE INDEX IF NOT EXISTS idx_sightings_plate_time ON sightings (plate_string, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_sightings_camera_time ON sightings (camera_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_sightings_location ON sightings USING GIST (location);

CREATE TABLE IF NOT EXISTS blacklist (
    plate_string text NOT NULL,
    reason       text NOT NULL,
    added_at     timestamptz NOT NULL DEFAULT now(),
    -- Composite, not plate_string alone: one plate can be listed for several reasons.
    PRIMARY KEY (plate_string, reason)
);

-- MVP alerting: written by ingest synchronously when a stored sighting's plate is
-- on the blacklist. No separate alert microservice, no queue. If alerts ever need
-- to fan out, that is the seam to cut at.
CREATE TABLE IF NOT EXISTS alerts (
    id           serial PRIMARY KEY,
    plate_string text NOT NULL,
    matched_at   timestamptz NOT NULL DEFAULT now(),
    camera_id    text NOT NULL,
    lat          double precision NOT NULL,
    lon          double precision NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_matched_at ON alerts (matched_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_plate_time ON alerts (plate_string, matched_at DESC);

-- Camera registry. The only place a human-readable name or a zone exists: sightings
-- store a camera_id and nothing else, so the map would otherwise have to invent a
-- label. Seeded by db/zz-cameras.sh from the CAM*_ID/_NAME/_ZONE/_LAT/_LON vars in .env,
-- which is the same single source of truth docker-compose.yml gives the edge agents.
--
-- `zone` is free text, not an enum: zones are an operational grouping that changes more
-- often than the schema should. There is deliberately no FK from sightings.camera_id to
-- this table - ingest accepts a sighting from a camera that has not been registered yet
-- (a camera deployed ahead of a config change, or a spoofed topic), and rejecting it
-- would lose a real sighting over a missing label. Unregistered cameras surface as NULL.
CREATE TABLE IF NOT EXISTS cameras (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    zone        text,
    lat         double precision NOT NULL CHECK (lat >= -90.0 AND lat <= 90.0),
    lon         double precision NOT NULL CHECK (lon >= -180.0 AND lon <= 180.0),
    location    geometry(Point, 4326)
        GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED
);
