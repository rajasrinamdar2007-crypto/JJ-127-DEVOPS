#!/usr/bin/env bash
# Seeds the cameras table from the CAM*_ID/_NAME/_ZONE/_LAT/_LON vars in .env.
#
# WHY THE "zz" PREFIX: the postgres entrypoint runs everything in
# /docker-entrypoint-initdb.d/ in sorted order, so this must sort AFTER init.sql,
# which creates the table. 'z' > 'i' does that. Renaming this to something starting
# with a letter before 'i' would make it run first and fail on a missing table, and
# postgres would then refuse to initialise the volume at all.
#
# WHY A SCRIPT AND NOT MORE SQL: SQL cannot read container env vars. The camera
# config already lives in .env so docker-compose.yml can hand it to the edge agents;
# sourcing it from here keeps one source of truth instead of a camera list that drifts.
#
# Only runs on an empty volume, like init.sql.

set -euo pipefail

if [ -z "${CAM1_ID:-}${CAM2_ID:-}${CAM3_ID:-}${CAM4_ID:-}" ]; then
  echo "cameras: no CAM*_ID in the environment, skipping camera seed"
  exit 0
fi

# Upper bound rather than an open-ended loop, so adding CAM5_* to .env without
# touching this file fails loudly (no cameras seeded) instead of silently working.
MAX_CAM=6
seeded=0

for n in $(seq 1 "$MAX_CAM"); do
  eval "id=\${CAM${n}_ID:-}"
  eval "name=\${CAM${n}_NAME:-}"
  eval "zone=\${CAM${n}_ZONE:-}"
  eval "lat=\${CAM${n}_LAT:-}"
  eval "lon=\${CAM${n}_LON:-}"

  [ -n "$id" ] || continue

  # lat/lon are NOT NULL. A camera with no surveyed position is a config error worth
  # shouting about, not a row with a null coordinate that silently breaks the map.
  if [ -z "$lat" ] || [ -z "$lon" ]; then
    echo "cameras: CAM${n}_ID is set but CAM${n}_LAT/CAM${n}_LON are not, skipping" >&2
    continue
  fi

  # psql -v plus :'name' quoting, so a name containing a quote cannot break the SQL.
  psql -v ON_ERROR_STOP=1 -q \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v "cid=$id" -v "cname=$name" -v "czone=$zone" -v "clat=$lat" -v "clon=$lon" <<'SQL'
INSERT INTO cameras (id, name, zone, lat, lon)
VALUES (
  :'cid',
  COALESCE(NULLIF(:'cname', ''), :'cid'),
  NULLIF(:'czone', ''),
  :'clat'::double precision,
  :'clon'::double precision
)
ON CONFLICT (id) DO UPDATE
  SET name = EXCLUDED.name, zone = EXCLUDED.zone, lat = EXCLUDED.lat, lon = EXCLUDED.lon;
SQL

  echo "cameras: seeded $id"
  seeded=$((seeded + 1))
done

echo "cameras: $seeded camera(s) registered"
