import json
import os
from pathlib import Path

from jsonschema import Draft7Validator

CONTRACT_FILE = os.getenv("CONTRACT_FILE", "/app/contract/event_contract.json")

CONTRACT = json.loads(Path(CONTRACT_FILE).read_text())

SCHEMA_VERSION = CONTRACT["schema_version"]
TRANSPORT = CONTRACT["transport"]
EVENT = CONTRACT["event"]
JSON_SCHEMA = CONTRACT["json_schema"]

REQUIRED_FIELDS = list(EVENT["required"])
TOPIC_PATTERN = TRANSPORT["topic_pattern"]

VALIDATOR = Draft7Validator(JSON_SCHEMA)


def validate(event) -> list:
    """Return a list of human-readable problems. Empty list means the event is valid.

    Deliberately the same logic as ingest/app/contract.py: if producer and consumer
    disagreed about what the contract means, the edge would happily build events that
    ingest silently throws away. Validating here means a bad detection is caught while
    the file is still on disk and still attributable to a camera.
    """
    if not isinstance(event, dict):
        return [f"<root>: expected a JSON object, got {type(event).__name__}"]
    return [
        f"{'.'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in VALIDATOR.iter_errors(event)
    ]
