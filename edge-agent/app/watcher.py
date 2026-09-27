import json
import logging
import time
from pathlib import Path

from app import normalize, settings, spool

log = logging.getLogger(__name__)


def _set_aside(path: Path, processed: Path) -> None:
    """Move a file we will not act on out of the inbox, without clobbering a namesake."""
    processed.mkdir(parents=True, exist_ok=True)
    target = processed / path.name
    if target.exists():
        target = processed / f"{path.stem}-{int(time.time() * 1000)}{path.suffix}"
    path.rename(target)


def run_once(outbox: spool.Outbox) -> int:
    """Claim every detection currently sitting in the inbox. Returns how many."""
    inbox = Path(settings.INBOX_DIR)
    inbox.mkdir(parents=True, exist_ok=True)

    claimed = 0
    for path in sorted(inbox.glob("*.json")):
        try:
            detection = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Unreadable detection %s (%s); setting aside", path.name, e)
            _set_aside(path, Path(settings.PROCESSED_DIR))
            continue

        event, reason = normalize.to_event(detection)
        if event is None:
            log.warning("Rejecting %s: %s", path.name, reason)
            _set_aside(path, Path(settings.PROCESSED_DIR))
            continue

        outbox.add(event)
        _set_aside(path, Path(settings.PROCESSED_DIR))
        log.info(
            "Normalised %s -> %s at %s (conf=%.2f)",
            path.name,
            event["plate_string"],
            event["camera_id"],
            event["confidence"],
        )
        claimed += 1

    return claimed


def run():
    outbox = spool.Outbox()
    log.info(
        "watching %s for detections as %s (inbox poll %.1fs)",
        settings.INBOX_DIR,
        settings.CAMERA_ID,
        settings.POLL_SECONDS,
    )
    while True:
        try:
            claimed = run_once(outbox)
            if claimed:
                log.info("%d detection(s) -> outbox (%d awaiting ACK)", claimed, outbox.count())
        except Exception:
            # A watcher thread that dies silently stops the camera forever, and the
            # outbox still looks healthy. Never let one bad pass kill the loop.
            log.exception("watcher pass failed; continuing")
        time.sleep(settings.POLL_SECONDS)
