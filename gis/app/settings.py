import os

from dotenv import load_dotenv

load_dotenv()

# GIS API server
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))

# CORS: allow the frontend dev/preview origins to reach the API.
CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8001,http://127.0.0.1:8001",
).split(",")

# Postgres / PostGIS
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "db")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_USER = os.getenv("POSTGRES_USER", "anpruser")
# No default on purpose. A baked-in fallback would let the service silently authenticate
# with a password that is published in this repository, and would keep a credential
# literal in version control. docker-compose.yml marks the variable required too, so a
# missing value fails at startup with the variable's name rather than at query time.
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]
POSTGRES_DB = os.getenv("POSTGRES_DB", "anpr")

# Tables the read-only database viewer may expose. Whitelist only, so the generic
# /table/{name} endpoint cannot be pointed at anything else in the database.
ALLOWED_TABLES = tuple(
    t.strip() for t in os.getenv("ALLOWED_TABLES", "sightings,alerts,blacklist,cameras").split(",") if t.strip()
)

# Defaults for the GeoJSON feeds. Both are caps: a map client that forgets to pass
# them should get a bounded payload, not the whole table.
DEFAULT_SIGHTING_LIMIT = int(os.getenv("DEFAULT_SIGHTING_LIMIT", "500"))
MAX_SIGHTING_LIMIT = int(os.getenv("MAX_SIGHTING_LIMIT", "5000"))
DEFAULT_ALERT_LIMIT = int(os.getenv("DEFAULT_ALERT_LIMIT", "200"))
MAX_TRAJECTORY_POINTS = int(os.getenv("MAX_TRAJECTORY_POINTS", "2000"))
