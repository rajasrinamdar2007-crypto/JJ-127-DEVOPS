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

    A non-dict payload (a bare string, a list, null) comes back as a single error
    rather than raising, so the caller can treat every rejection the same way.
    """
    if not isinstance(event, dict):
        return [f"<root>: expected a JSON object, got {type(event).__name__}"]
    return [
        f"{'.'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in VALIDATOR.iter_errors(event)
    ]


def camera_id_from_topic(topic: str):
    """Pull {camera_id} out of a concrete topic by matching it against topic_pattern.

    Deriving it from the pattern instead of hardcoding topic.split("/")[1] means a
    future pattern such as anpr/{camera_id}/{road}/sightings keeps working without a
    code change. Returns None if the topic does not match the pattern at all.
    """
    pattern_parts = TOPIC_PATTERN.split("/")
    topic_parts = topic.split("/")
    if len(pattern_parts) != len(topic_parts):
        return None

    captured = None
    for pattern_part, topic_part in zip(pattern_parts, topic_parts):
        if pattern_part.startswith("{") and pattern_part.endswith("}"):
            if captured is not None:
                return None
            captured = topic_part
        elif pattern_part != topic_part:
            return None
    return captured
