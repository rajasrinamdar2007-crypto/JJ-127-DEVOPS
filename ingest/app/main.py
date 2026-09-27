import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from app import mqtt_listener, storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    mqtt_listener.start()
    yield
    mqtt_listener.stop()


app = FastAPI(title="ingest", lifespan=lifespan)


# Sync def on purpose: the checks are blocking, so FastAPI runs this in a threadpool
# instead of stalling the event loop. 503 makes the container's own healthcheck fail
# when we are up but cannot actually store anything, which a static response cannot do.
@app.get("/health")
def health(response: Response):
    db_ok = storage.ping()
    mqtt_ok = mqtt_listener.is_connected()
    healthy = db_ok and mqtt_ok
    if not healthy:
        response.status_code = 503
    return {
        "status": "ok" if healthy else "degraded",
        "service": "ingest",
        "db": db_ok,
        "mqtt": mqtt_ok,
    }
