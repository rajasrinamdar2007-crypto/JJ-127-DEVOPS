DOCKER := docker compose
MOSQ_IMAGE := eclipse-mosquitto:2
MOSQ_CONFIG := $(CURDIR)/mosquitto/config

ifneq (,$(wildcard .env))
include .env
export
endif

# Users the broker password file is generated from: the healthcheck account, the
# ingest subscriber, and one account per simulated camera. Single source of truth is
# .env - the same CAM*_ID/CAM*_PASS pairs docker-compose.yml hands to each edge-agent.
# Empty if .env is absent, which `passwd` reports rather than silently producing nothing.
MQTT_USERS := $(MQTT_SERVER_USER)=$(MQTT_SERVER_PASSWORD) $(MQTT_INGEST_USER)=$(MQTT_INGEST_PASSWORD) $(MQTT_DEBUG_USER)=$(MQTT_DEBUG_PASSWORD) $(CAM1_ID)=$(CAM1_PASS) $(CAM2_ID)=$(CAM2_PASS) $(CAM3_ID)=$(CAM3_PASS)

.PHONY: help up down reset passwd psql health logs
.PHONY: ingest-logs ingest-shell ingest-rebuild
.PHONY: gis-logs gis-shell gis-rebuild
.PHONY: frontend-logs frontend-shell frontend-rebuild frontend-dev
.PHONY: sim-run sim-shot seed-blacklist debug-subscribe
.PHONY: edge-logs edge-shell edge-rebuild seed

# The dashboard lives in a sibling checkout, so the same "where is the frontend"
# question comes up constantly. FRONTEND_DIR is only used by the targets that need to
# touch the source tree directly; compose resolves its own context from
# docker-compose.yml, and the two must be kept in step if you move the checkout.
FRONTEND_DIR ?= ../anpr-frontend

help:
	@echo "Targets:"
	@echo "  up        build + start all services in the background"
	@echo "  down      stop all services (data kept)"
	@echo "  reset     stop and delete the pgdata volume (schema re-runs on next up)"
	@echo "  passwd    regenerate the mosquitto password file from .env"
	@echo "  psql      open a psql shell in the db container"
	@echo "  health    show container status and health"
	@echo "  logs      follow logs from all services"
	@echo ""
	@echo "  ingest-logs     follow the ingest subscriber's logs"
	@echo "  ingest-shell    open a shell inside the ingest container"
	@echo "  ingest-rebuild  rebuild the ingest image and recreate only ingest"
	@echo "  gis-logs        follow the GIS API's logs"
	@echo "  gis-shell       open a shell inside the gis container"
	@echo "  gis-rebuild     rebuild the gis image and recreate only gis"
	@echo "  frontend-logs      follow nginx's logs (app + /api proxy errors)"
	@echo "  frontend-shell     open a shell inside the frontend container"
	@echo "  frontend-rebuild   rebuild the frontend image and recreate only frontend"
	@echo "  frontend-dev        run the Vite dev server on the host against the GIS API"
	@echo ""
	@echo "  sim-run         publish synthetic detections into the camera inboxes (Ctrl-C to stop)"
	@echo "  sim-shot        publish one round of detections and exit (for a quick end-to-end check)"
	@echo "  seed-blacklist  insert the test blacklist plates that drive the alerting test"
	@echo "  debug-subscribe log every anpr/+/sightings message on the bus (Ctrl-C to stop)"
	@echo "  edge-logs       follow every edge-agent's logs"
	@echo "  edge-shell      open a shell inside one edge-agent (CAM=1|2|3)"
	@echo "  edge-rebuild    rebuild the edge-agent image and recreate the agents"
	@echo "  seed            drop synthetic detections into every camera inbox"

up:
	$(DOCKER) up -d --build

# Used by the deploy workflows. `up` always rebuilds, which is the right default for local
# work but wrong for an env-only deploy: nothing in the images changes when only .env moves,
# so the slow rebuild is pure cost. --no-build also fails loudly if an image is genuinely
# missing, rather than silently building it and hiding the mistake.
deploy:
	$(DOCKER) up -d --no-build

# The dashboard is the one image a restart can never bring up to date. Vite inlines
# VITE_API_URL into the bundle at BUILD time, so a container restart keeps serving the old
# base URL. Changing the API base therefore requires a rebuild, not just a restart.
deploy-frontend:
	$(DOCKER) up -d --build frontend

down:
	$(DOCKER) down

reset:
	$(DOCKER) down -v

# mosquitto_passwd -c refuses to touch an existing file, so each account is hashed
# into a scratch file and appended. Do not "simplify" this into one -c call.
passwd:
	@if [ -z "$(MQTT_USERS)" ]; then echo "no .env found - copy .env.example to .env first"; exit 1; fi
	@rm -f mosquitto/config/passwords mosquitto/config/.pw.tmp
	@for entry in $(MQTT_USERS); do \
		user=$${entry%%=*}; pass=$${entry##*=}; \
		rm -f mosquitto/config/.pw.tmp; \
		docker run --rm -v "$(MOSQ_CONFIG):/cfg" --entrypoint mosquitto_passwd $(MOSQ_IMAGE) \
			-b -c /cfg/.pw.tmp "$$user" "$$pass" >/dev/null; \
		cat mosquitto/config/.pw.tmp >> mosquitto/config/passwords; \
	done
	@rm -f mosquitto/config/.pw.tmp
	@echo "mosquitto password file regenerated from .env"

psql:
	$(DOCKER) exec db psql -U $${POSTGRES_USER:-anpruser} -d $${POSTGRES_DB:-anpr}

health:
	$(DOCKER) ps

logs:
	$(DOCKER) logs -f --tail=100

ingest-logs:
	$(DOCKER) logs -f --tail=100 ingest

ingest-shell:
	$(DOCKER) exec ingest sh

ingest-rebuild:
	$(DOCKER) up -d --build --no-deps ingest

gis-logs:
	$(DOCKER) logs -f --tail=100 gis

gis-shell:
	$(DOCKER) exec gis sh

gis-rebuild:
	$(DOCKER) up -d --build --no-deps gis

frontend-logs:
	$(DOCKER) logs -f --tail=100 frontend

frontend-shell:
	$(DOCKER) exec frontend sh

# No --no-deps here: the frontend is useless without gis resolvable, and a stale gis
# would leave nginx proxying to nothing.
frontend-rebuild:
	$(DOCKER) up -d --build frontend

# Dev server on the host, for iterating on the dashboard without a rebuild. The Vite
# proxy forwards /api to the host-published GIS port, so the stack below must be up.
# Runs in the foreground; Ctrl-C to stop.
frontend-dev:
	@if [ ! -f "$(FRONTEND_DIR)/package.json" ]; then \
		echo "no frontend at $(FRONTEND_DIR) - set FRONTEND_DIR=<path> or check out anpr-frontend alongside this repo"; \
		exit 1; \
	fi
	@echo "dev server on http://localhost:5173, proxying /api -> $(or $(VITE_DEV_PROXY_TARGET),http://localhost:$${GIS_HOST_PORT:-8001})"
	cd $(FRONTEND_DIR) && npm run dev

# --- Development / test harness -------------------------------------------------
#
# These need the stack up (make up) but are not part of it. Each is `run --rm`, so the
# container is created, does one job and disappears - there is no half-stopped generator
# left behind holding a shell open, and no `docker compose down` needed to clean up.

# Continuous synthetic traffic. SIM_ARGS lets you tune the run without editing this file:
#   make sim-run SIM_ARGS="--hop-seconds 1 --concurrent 8"
#   make sim-run SIM_ARGS="--cameras hinjewadi,baner --no-blacklist"
# The hop interval sets apparent travel speed across the city: the edge agent stamps
# every event with the time it processed the file, so a vehicle's sightings are spaced by
# this interval, not by anything in the detection file.
sim-run:
	$(DOCKER) --profile sim run --rm -T sim python generate_detections.py $(SIM_ARGS)

# One round and exit. This is the one to use for a scripted end-to-end check, because it
# terminates on its own and leaves a known, finite amount of traffic behind.
sim-shot:
	$(DOCKER) --profile sim run --rm -T sim python generate_detections.py --duration $(or $(DURATION),20) $(SIM_ARGS)

# Arm the alerting test. Safe to re-run: the blacklist is keyed on (plate_string, reason)
# and the insert is an upsert, so a second run adds nothing.
seed-blacklist:
	$(DOCKER) --profile sim run --rm -T sim python seed_blacklist.py $(SEED_ARGS)

# Watch the bus directly. If a sighting never reaches the database, run this alongside
# the pipeline: no line here means the detector published nothing, a line here plus an
# absent row means something downstream dropped it.
debug-subscribe:
	$(DOCKER) --profile sim run --rm debug-subscriber python subscriber.py $(DEBUG_ARGS)

EDGE_SERVICES := edge-agent-hinjewadi edge-agent-baner edge-agent-kothrud

edge-logs:
	$(DOCKER) logs -f --tail=100 $(EDGE_SERVICES)

edge-shell:
	$(DOCKER) exec edge-agent-$(or $(CAM),hinewadi) sh

edge-rebuild:
	$(DOCKER) up -d --build --no-deps $(EDGE_SERVICES)

# Stands in for the OCR/detection component, which is out of scope. Writes one
# synthetic detection file per camera; the watcher picks them up on its next poll.
seed:
	@for cam in $(EDGE_SERVICES); do \
		dir=$$(echo $$cam | sed 's/edge-agent-//'); \
		plate=$$(head -c 2 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-2 | tr 'a-f' 'A-Z'); \
		printf '{"plate_string": "%s%02dAB%04d", "confidence": 0.9, "image_path": "/data/frames/%s.jpg"}\n' \
			"$$plate" "$$(od -An -N1 -tu1 /dev/urandom | tr -d ' ')" "$$(od -An -N2 -tu2 /dev/urandom | tr -d ' ')" "$$dir" \
			> edge-agent/data/$$dir/inbox/seed-$$(date +%s%N).json; \
		echo "  seeded $$dir"; \
	done
