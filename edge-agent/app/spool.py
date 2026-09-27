import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app import settings

log = logging.getLogger(__name__)


class Outbox:
    """Disk-backed outbox: an event leaves only once the broker has ACKed it.

    The contract deliberately has no event_id (ingest assigns `id serial` on insert),
    so spool identity is local - a uuid in the filename - and peek() hands the path
    back alongside the event. That keeps bookkeeping out of the published payload and
    lets remove() delete exactly what was published, with no glob-by-id guesswork.
    """

    def __init__(self, directory: str = None):
        self.dir = Path(directory or settings.OUTBOX_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.corrupt_dir = self.dir / "corrupt"
        self._lock = threading.Lock()

    def add(self, event: dict) -> Path:
        with self._lock:
            # Re-assert the directory on every write, not just in __init__. A bind
            # mount can be replaced or cleaned out from under a long-running
            # container, and the outbox is the durability guarantee - it must not
            # depend on a directory surviving for the process lifetime. The call is
            # idempotent, so the steady-state cost is one failed stat.
            self.dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            name = f"{stamp}-{uuid.uuid4().hex}.json"
            tmp = self.dir / f".{name}.tmp"
            final = self.dir / name
            # Write then rename: peek() must never see a half-written event.
            tmp.write_text(json.dumps(event))
            tmp.rename(final)
            return final

    def peek(self) -> list:
        with self._lock:
            out = []
            for path in sorted(self.dir.glob("*.json")):
                try:
                    out.append((path, json.loads(path.read_text())))
                except (json.JSONDecodeError, OSError) as e:
                    # Quarantine rather than delete. An outbox that silently discards
                    # unreadable events is an outbox that loses sightings with no trace.
                    self.corrupt_dir.mkdir(parents=True, exist_ok=True)
                    path.rename(self.corrupt_dir / path.name)
                    log.error("quarantined unreadable outbox file %s: %s", path.name, e)
            return out

    def remove(self, path) -> None:
        with self._lock:
            Path(path).unlink(missing_ok=True)

    def count(self) -> int:
        with self._lock:
            return sum(1 for _ in self.dir.glob("*.json"))
