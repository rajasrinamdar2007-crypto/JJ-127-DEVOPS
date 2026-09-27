# JJ-127-DEVOPS

A geo-distributed ANPR (automatic number plate recognition) platform: reads vehicle plates off
roadside cameras, streams the sightings over MQTT, and puts every one of them on a live map.

**OCR / detection is out of scope.** No plate reader or object-detection model lives in this repo.
The upstream detection component is treated as a black box that publishes JSON and nothing more;
until a real detector is plugged in, that JSON is **synthetic**, and the rest of the pipeline is
exercised end to end against those fakes.

## Architecture at a glance

```
                    ┌──────────────────────── browser ────────────────────────┐
                    │  dashboard (nginx: static SPA + /api reverse proxy)     │
                    └───────────────┬──────────────────────────┬──────────────┘
                                    │ /api/...  (same-origin)    │ /assets/...
                       nginx strips /api                    nginx serves dist/
                                    │                          │
edge  ──MQTT──▶  ingest  ──▶  PostGIS  ──▶  GIS API  ◀───────┘
  ▲               validate      sightings      GeoJSON /
  │               + store       geometry       trajectory
  └── spool/outbox              (4326)         + alerts
     (durable)                                  + cameras
```

- **edge** — one agent container per camera, publishing to `anpr/{camera_id}/sightings` at QoS 1.
  Watches a filesystem inbox for synthetic detections, normalises them into contract sightings, and
  spools them to disk. **An event leaves the outbox only on broker ACK**, so a broker outage delays
  sightings but never loses them.
- **MQTT** — mosquitto broker, the only coupling between the edge and the rest of the platform.
- **ingest** — subscribes, validates each payload against the contract's `json_schema`, drops
  anything malformed, and upserts. Blacklisted plates raise a row in `alerts` in the same
  transaction — the MVP alerting flow, no separate alert service.
- **PostGIS** — `sightings` in `geometry(Point, 4326)`, so bbox and radius queries are the
  database's job rather than ours. `cameras` is the registry the edge agents and the API share.
- **GIS API** — read-only HTTP over those sightings: a GeoJSON feed of recent sightings, a
  per-plate trajectory ready to animate, the alert feed, the camera registry, and a whitelisted
  table viewer. Everything a status looks like on the map (band, colour, marker radius) comes from
  `gis/app/plate_status_types.py`, so the legend is a deploy of the API rather than a rebuild of
  the front end. Full endpoint reference: [`gis/API.md`](gis/API.md).
- **dashboard** — the React/MapLibre/deck.gl front end, built from the **sibling checkout**
  `../anpr-frontend` (see [Frontend](#frontend)). It runs behind nginx, which serves the built
  bundle and reverse-proxies `/api/*` to the `gis` service, stripping the `/api` prefix. Because
  the browser only ever makes same-origin requests, **CORS is not involved on this path** and the
  API's `CORS_ORIGINS` only matters for direct non-proxied clients.

## Frontend

The dashboard is a **separate git repository**, checked out as a sibling of this one:

```
SIH127/
├── JJ-127-DEVOPS/     ← this repo: broker, database, ingest, GIS API, edge agents
└── anpr-frontend/     ← the dashboard (Vite + React + TypeScript + deck.gl)
```

They stay separate repositories, but compose builds the frontend from `context: ../anpr-frontend`
so one `docker compose up` still brings up the whole stack. If you keep the frontend somewhere
else, change that one path in `docker-compose.yml` and `FRONTEND_DIR` in the `Makefile`.

It talks to the API over a **relative** `/api` base, never an absolute host:port. That is what lets
the same bundle work in three places without a rebuild.

| Where you run it | How `/api` resolves |
| --- | --- |
| `npm run dev` (host) | Vite dev proxy → the host-published GIS port (`VITE_DEV_PROXY_TARGET`, default `http://localhost:8003`) |
| Docker (`make up`) | nginx in the `frontend` container → `http://gis:8000/` |
| Static build elsewhere | whatever you reverse-proxy `/api` to — the app itself is unchanged |

`npm run dev` and the container both strip the `/api` prefix, because the GIS service's own routes
are unprefixed (`/sightings/geojson`, `/plate-status-types`, …). The `/api` prefix exists purely to
keep the browser same-origin.

## Simulating the detection component

OCR and detection are out of scope, so the edge agents are fed by dropping JSON files into their
inbox. One file per detection:

```
edge-agent/data/<camera>/inbox/anything.json
```

```json
{
  "plate_string": "mh 12 ab 1234",
  "confidence": 0.91,
  "image_path": "/data/frames/f1.jpg",
  "direction": "S",
  "lane": 3
}
```

Only `plate_string` and `confidence` are required. `plate_string` is normalised (`mh 12 ab 1234`
becomes `MH12AB1234`); `camera_id`, `lat`, `lon` and `timestamp` are supplied by the agent from its
own configuration. `direction` and `lane` are optional and pass through when present. Anything the
agent cannot turn into a valid sighting is moved to `processed/` with the reason logged.

`make seed` below does this by hand for one file per camera. To generate sustained,
self-consistent traffic — plates that reappear along a route, so trajectories are real —
use the [test harness](#development--test-harness) instead.

## Running it

```
cp .env.example .env    # then edit if you want different credentials
make passwd             # generate the broker password file from .env (gitignored)
make up                 # broker, database, ingest, GIS API, dashboard and one agent per camera
make seed               # drop one synthetic detection per camera (quick smoke test)
make psql               # inspect the schema
make ingest-logs        # follow the subscriber
make gis-logs           # follow the GIS API
make frontend-logs      # follow nginx, including any /api proxy errors
make edge-logs          # follow every agent
```

Then open **http://localhost:8080** (`FRONTEND_HOST_PORT`, default 8080).

> **A note on `src/Login.tsx`.** That file imports `./components/LoginForm` and
> `./components/ForgotPasswordModal`, and the `components/` directory has never existed in that
> repository. It is an orphaned page, not a wired one: `App.tsx` routes only `/` to the dashboard.
> It is excluded in the front-end `tsconfig.json` so its two unresolved imports cannot fail the
> image build; the file itself is untouched. See [Known limits](#known-limits).

The GIS API listens on `GIS_HOST_PORT` (default 8001) **as well as** being reachable through the
dashboard at `http://localhost:8080/api/`. Both work; the API port is there for curl and for the
dev server, the proxied path is what the browser uses.

```
curl localhost:8001/sightings/geojson?limit=5
curl localhost:8001/plates/MH12AB1234/trajectory
curl localhost:8001/alerts
curl localhost:8001/cameras
curl localhost:8001/plate-status-types
curl localhost:8001/tables
curl localhost:8001/table/sightings

# the same requests through nginx, which is what the browser does:
curl localhost:8080/api/sightings/geojson?limit=5
curl localhost:8080/api/plate-status-types
```

| Endpoint | Returns |
| --- | --- |
| `GET /sightings/geojson` | recent sightings as a `FeatureCollection`; `?since=<ISO>` and `?limit=` |
| `GET /plates/{plate}/trajectory` | one plate's sightings in time order, as timestamped points plus a `LineString`; `?limit=` |
| `GET /alerts` | recent blacklist matches, with their reasons; `?since=` and `?limit=` |
| `GET /cameras` | the camera registry as a `FeatureCollection`; `?limit=` |
| `GET /plate-status-types` | the presentation registry (label / colour / radius per status) |
| `GET /tables`, `GET /table/{name}` | read-only row viewer over a whitelisted table; `?limit=` |

Plate lookups are normalised the same way the edge agent normalises them, so
`/plates/mh 12 ab 1234/trajectory` and `/plates/MH12AB1234/trajectory` are the same plate. The
trajectory is an exact match today; fuzzy matching is a documented TODO, and the comment in
`gis/app/main.py` says why the query and its GIN trigram index have to land together.

`gis` runs with no MQTT credentials — the map reads sightings, it does not publish them. It issues
only `SELECT`s, and `/table/{name}` is additionally gated on the `ALLOWED_TABLES` whitelist, so the
one endpoint that puts a request path into SQL is not reachable with anything else. `gis/API.md` is
the full reference: response shapes, status codes, and the limits the API does *not* cover.

`make help` lists every target. `make reset` drops the `pgdata` volume, which re-runs
`db/init.sql` on the next `make up` — needed after any schema change, since the init script
only runs against an empty volume.

Each simulated camera is an MQTT account whose username equals its `camera_id`, granted write
access to exactly one topic — `anpr/{camera_id}/sightings` — so a camera can never publish under
another camera's identity. `ingest` may read `anpr/+/sightings` and write nothing. Adding a camera
means five edits, all listed in `.env.example`: the `CAM4_*` vars, an ACL user block, the
`MQTT_USERS` line in the Makefile, a copied `edge-agent-*` block in `docker-compose.yml`, then
`make passwd`.

## Development / test harness

`sim/` and `server/` are tools you run on purpose. They sit behind the compose `sim`
profile, so `make up` never starts them — a synthetic traffic generator and a bus sniffer
have no business publishing into a real deployment.

```
sim/generate_detections.py   the publisher: writes detections into the camera inboxes
sim/seed_blacklist.py        inserts the test plates that drive the alerting test
sim/plates.py                plate/blacklist data, shared by both so they cannot drift
server/subscriber.py         independent bus subscriber, for when a sighting goes missing
```

`make seed` (above) drops a single file into each camera and is the quickest way to check
the pipeline is alive. The generator is the thing to reach for when the question is what
the system does under load, or whether a plate actually produces a trajectory — it models
vehicles rather than dumping one-offs.

### The usual loop

```
make up                 # the platform
make seed-blacklist     # arm the alerting test
make sim-run            # publish synthetic traffic (Ctrl-C to stop)
make debug-subscribe    # in a second terminal: watch every message on the bus
```

Then open http://localhost:8080 and the map fills with moving trajectories and real alerts.

| Target | What it does |
| --- | --- |
| `make sim-run` | continuous traffic until interrupted |
| `make sim-shot` | one bounded round (`DURATION=20` by default) and exit |
| `make seed-blacklist` | insert the test blacklist plates (idempotent; `--clear` via `SEED_ARGS`) |
| `make debug-subscribe` | log every `anpr/+/sightings` message, independent of ingest |

Both `sim-run` and `sim-shot` take `SIM_ARGS`, `seed-blacklist` takes `SEED_ARGS`, and
`debug-subscribe` takes `DEBUG_ARGS`:

```
make sim-run SIM_ARGS="--hop-seconds 1 --concurrent 8"
make sim-run SIM_ARGS="--cameras hinjewadi,baner --no-blacklist"
make sim-shot DURATION=10 SIM_ARGS="--blacklist-share 1.0"
make seed-blacklist SEED_ARGS="--clear"   # disarm the alerting test
```

### What the generator models

A sighting is not a random plate at a random camera. It simulates **vehicles driving a
route**: each vehicle keeps its plate and its class and reappears at the next camera
along, which is what produces a multi-point trajectory for `/plates/{plate}/trajectory`.
A share of vehicles keep looping rather than retiring, so the map does not slowly empty.
A share carry no `vehicle_type` at all, to exercise the "detector could not classify it"
path — the API stores `NULL` and the map renders them with the unclassified pin rather
than dropping them. Blacklisted vehicles are otherwise completely ordinary; only the
plate differs, so an alert can only be caused by the plate.

The camera list is discovered from the directories under `edge-agent/data`, so a new
edge-agent is picked up without editing the generator. Only the *order* is fixed:
cameras are visited in a hand-checked north-west to south-east sequence, because that
order is what makes a journey read as a drive across the city rather than a random walk.
An unrecognised camera is appended with a warning — pass `--cameras` to place it.

### One thing to know: timestamps are real, not simulated

`edge-agent/app/normalize.py` stamps every event with the time it *processed* the file
and ignores any timestamp in the detection, so **a detection cannot be backdated** by
writing one into an inbox. That is deliberate — a detector should not get to dictate when
a sighting happened — and the harness does not weaken it.

The consequence is that the gap between a vehicle's sightings is set by `--hop-seconds`,
not by a field in the file. A 3-hop trajectory takes `3 * --hop-seconds` to build. Use a
small interval when you want the city to feel busy.

### The debug subscriber

`server/subscriber.py` exists for one question: when a sighting is missing from the
database, was it never published, or was it dropped on the way in? It subscribes on its
own `debug` account (same read scope as ingest, separate identity in the broker's access
log) at QoS 0, so observing the bus cannot perturb the delivery path it is watching. It
prints the raw payload when it will not parse, and flags the two things that actually
break a pipeline: a missing plate and an out-of-range confidence.

## Contract

`contract/` is the single source of truth for everything above. Change the event there first; every
service is written against it.

- [`contract/event_contract.json`](contract/event_contract.json) — the plate sighting event: field
  types, required set, transport details, worked example, and the `json_schema` block that
  `ingest` actually validates against.
- [`contract/OD_INTERFACE.md`](contract/OD_INTERFACE.md) — the handshake with the upstream
  detection component: topic convention, delivery rules, and what each side owns.

## Status

Working end to end, verified live: `mosquitto` (MQTT, per-camera ACL), `db` (PostGIS, `sightings` +
`cameras` + `blacklist` + `alerts`), `ingest` (contract validation, dedup, blacklist alerting), `gis`
(GeoJSON feed, trajectories, alerts, camera registry, read-only viewer), and three `edge-agent`
instances generating synthetic traffic.

Tested: valid sightings store and reach PostGIS; QoS 1 redeliveries dedup; blacklisted plates alert
exactly once; malformed payloads are dropped at the edge and again at ingest without taking anything
down; a broker outage spools to disk and delivers with zero loss once the broker returns; a camera
cannot publish under another camera's identity.

Tested on the GIS API, against seeded data: sightings, alerts and trajectories come back as valid
`FeatureCollection`s with `[lon, lat]` ordering and timestamps in chronological order; a plate
blacklisted for two reasons still appears once per sighting; a single-sighting plate returns a point
and no one-position `LineString`; confidence lands in the right band at the 0.85/0.60/0.59 edges;
`?since=` filters correctly; a bad timestamp is a 400 and an out-of-range `?limit=` a 422; and
`/table/blacklist%3Bdrop%20table%20sightings` is a 404.

Also tested after adding `vehicle_type` and the camera registry: valid classes (`car`/`bike`/`truck`/
`bus`) persist, an unrecognised class and an absent one both store as `NULL` rather than failing the
event, `/cameras` returns the three seeded cameras, sightings carry the joined camera name and zone,
and the registry serves the `vehicle:*` styles.

The dashboard is wired to that API and **bundles and builds successfully**, with all mock data
removed — it no longer generates fake sightings when the API is unreachable, and says so on
screen instead. Its nginx proxy config, Dockerfile and Compose service are in place, and the
`frontend` image builds and serves the app.

## Continuous integration and deployment

Three workflows live in `.github/workflows/`, all triggered against `dev` (production
deploys against `main`).

| workflow | trigger | what it does |
| --- | --- | --- |
| `ci.yml` | push/PR to `dev` | compose validation, Python byte-compile, dashboard build |
| `deploy-staging.yml` | push to `dev` | env-only deploy to the staging host |
| `deploy-prod.yml` | push to `main` | env-only deploy to the production host |

`ci.yml` runs three job groups. The five Python modules get **one job each**, with
`fail-fast` off, so a syntax error in one module cannot mask the other four and a reviewer
sees one red check per broken component. The check is `py_compile`, which only parses — no
pip install, no database, no broker — with the bytecode cache redirected to `$RUNNER_TEMP`
so the job cannot leave `.pyc` files in the checkout. The dashboard job is a hard gate: no
`continue-on-error`, no `|| true`. Since its build script is `tsc -b && vite build`, a type
error fails the job as surely as a bundler error. Lint runs only when `package.json`
defines a `lint` script, because `npm run lint` on a project without one exits non-zero
with `Missing script`.

The compose job validates with `--env-file .env.example`. The real `.env` is gitignored, so
a fresh runner has none, and `docker-compose.yml` marks variables like `${CAM2_ID:?...}` as
required — without the flag the job fails on missing variables and never checks the compose
file at all. Validating against the committed template also catches drift: a required
variable added to `docker-compose.yml` but forgotten in `.env.example` fails in CI rather
than at deploy time.

### The dashboard is a separate repository

`docker-compose.yml` refers to the dashboard as the sibling directory `../anpr-frontend`,
and it lives in its own GitHub repository. The CI job therefore checks it out explicitly
rather than expecting it in this checkout. Two repository **variables** on this repo control
it, both optional:

| variable | default | purpose |
| --- | --- | --- |
| `FRONTEND_REPO` | `miloslava92/anpr-frontend` | which repository holds the dashboard |
| `FRONTEND_REF` | `main` | which branch to build |

`FRONTEND_REF` is the one to watch. The dashboard's API integration (`src/api.ts`, the
nginx proxy, the `tsconfig` exclude) has to exist on the branch being built — against a
revision without it, `npm run build` fails on `Login.tsx` and `MapDashboard.tsx`. Point it
at whatever branch actually carries the work rather than editing the workflow.

### Deploys are env-only, and mostly do not rebuild

Both deploy workflows push `.env.example`, write the per-environment secrets on the server,
and restart with `make deploy` — which is `docker compose up -d --no-build`. Nothing about
an environment change alters the images, so rebuilding on every deploy is pure cost.
`make up` still rebuilds, which is the right default for local work.

Two Makefile targets exist for this:

```
make deploy           docker compose up -d --no-build
make deploy-frontend  docker compose up -d --build frontend
```

**The dashboard is the exception, and this is not a detail.** Vite inlines `VITE_API_URL`
into the bundle at *build* time, so a container restart keeps serving the old base URL. No
amount of restarting updates it. Rather than trust a flag, each workflow reads the bundle
the target is actually serving, looks for the expected base URL, and rebuilds only on a
genuine mismatch. Staging exposes a `force_frontend_rebuild` input on manual dispatch;
production deliberately does not, so a production rebuild has to be a conscious change.

Configure the `staging` and `production` GitHub **environments** with:

| secret | purpose |
| --- | --- |
| `SSH_HOST` | deploy target |
| `SSH_PRIVATE_KEY` | deploy key, written to `~/.ssh` at run time |
| `SSH_KNOWN_HOSTS` | pinned host key — never `StrictHostKeyChecking=no` |
| `STAGING_POSTGRES_PASSWORD` / `PROD_POSTGRES_PASSWORD` | written to `POSTGRES_PASSWORD` |
| `STAGING_MQTT_PASSWORD` / `PROD_MQTT_PASSWORD` | written to `MQTT_SERVER_PASSWORD` |

plus repository **variables** `DEPLOY_PATH`, `VITE_API_URL_STAGING` and
`VITE_API_URL_PROD`. Nothing is hardcoded, and the real `.env` never enters version
control — it is assembled on the server from these secrets. Set required reviewers on the
`production` environment to gate production behind a human. Both workflows use
`cancel-in-progress: false`; cancelling a deploy midway would leave the target in a state
nobody asked for, and each one fails if the dashboard or the API proxy is not actually
answering.

## Known limits

- `src/Login.tsx` in the front-end repo imports `./components/LoginForm` and
  `./components/ForgotPasswordModal`, and that `components/` directory has never existed in the
  repository — the import was broken in its first and only commit, and nothing was ever deleted.
  The page is also unreachable (`App.tsx` routes only `/` to the dashboard, and it navigates to
  `/map`, which is not a route either). Rather than invent those components, `Login.tsx` is
  excluded in the front-end `tsconfig.json`: Vite never bundled it anyway, but `tsc -b` compiles
  all of `src/` and its two unresolved imports were failing the image build. The file is kept
  untouched as the design record. Remove the exclude in the same commit if the login page is
  ever built.
- The dashboard is a single page with no authentication in front of it. nginx serves it to anyone
  who can reach port 8080, and the API behind it is unauthenticated too. Fine for a LAN demo,
  not fine for anything exposed.
- The dashboard's blacklist column is **read-only**. The old build had a toggle button calling a
  `PUT /detections/{id}/toggle_flag` that the GIS API never implemented — the request would have
  404'd while the UI optimistically showed the new state. Flagging now means editing the
  `blacklist` table, which ingest picks up on the next sighting.
- `ingest` opens a new Postgres connection per message. Fine at this volume, wrong at real camera
  throughput — it needs a pool before the load test that will eventually break it.
- The edge agent republishes an event if no ACK arrives within `ACK_TIMEOUT_SECONDS` (30s), so a
  sighting can occasionally be delivered twice. Ingest dedup makes that harmless.
- `gis` opens a new Postgres connection per request, like `ingest`. Same verdict: fine now, wrong at
  camera throughput, needs pooling.
- `alerts` has no `location` column, only `lat`/`lon`, and no `reason`. The GIS API papers over both
  at read time — it aggregates the reason from `blacklist` and builds the point itself — but a
  radius or heatmap query over alerts would want a real generated geometry column like
  `sightings.location`.
- `/sightings/geojson` has no `bbox` or radius filter, so a wide map refetches everything and the
  GIST index on `location` goes unused. `ST_Intersects` or `ST_DWithin` is the obvious next endpoint;
  `pg_trgm` is enabled for the same reason but no GIN trigram index exists yet.

