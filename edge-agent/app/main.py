import logging
import threading
import time

from app import mqtt_publisher, settings, watcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def main():
    settings.fatal_if_unconfigured()

    log = logging.getLogger("app.main")
    log.info(
        "edge-agent up: camera=%s at %.4f,%.4f (jitter %.1fm) -> %s",
        settings.CAMERA_ID,
        settings.CAMERA_LAT,
        settings.CAMERA_LON,
        settings.CAMERA_JITTER_M,
        settings.event_topic(),
    )

    threading.Thread(target=watcher.run, name="watcher", daemon=True).start()
    threading.Thread(target=mqtt_publisher.start, name="publisher", daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
