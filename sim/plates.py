"""Canonical synthetic plate data, shared by the sim scripts.

Kept in one place so `generate_detections.py` and `seed_blacklist.py` cannot drift:
the generator only ever emits plates that are actually on the blacklist, instead of
two hand-maintained lists that quietly stop agreeing.

Nothing here is random at import time, so importing this module has no side effects.
"""

from __future__ import annotations

import random
import string

# Real RTO codes, so the synthetic plates look like something a Pune camera would
# actually read. The number is the RTO/district registration, the letters the series.
STATE_CODES = (
    "MH12",  # Pune
    "MH13",  # Pune (Aurangabad HSM)
    "MH14",  # Pimpri-Chinchwad
    "KA05",  # Bengaluru (South)
    "DL01",  # Delhi (South)
    "GJ01",  # Ahmedabad
    "TN09",  # Chennai
    "TS07",  # Hyderabad
    "KL07",  # Thiruvananthapuram
    "WB06",  # Kolkata
)

VEHICLE_TYPES = ("car", "bike", "truck", "bus")

# Weighted so a map full of fake traffic looks like real traffic: mostly cars, a
# scattering of two-wheelers, few heavy vehicles.
VEHICLE_TYPE_WEIGHTS = (0.58, 0.24, 0.11, 0.07)

# Confidence is not uniform. Real plate readers are very good at a clean, well-lit
# plate and much worse at a mud-splattered one, so a normal-shaped beta skewed low
# reproduces that: mostly 0.80-0.99 with a visible tail below 0.60.
CONFIDENCE_SKEW = 1.6

# Number plates are issued in series; using the full A-Z range for both letter blocks
# produces plates like "MH12QX9901" that no real vehicle would carry. Omitting I, O
# and Q avoids the classic 1/0/I/O and O/0/D ambiguities and looks far more plausible.
PLATE_LETTERS = "".join(c for c in string.ascii_uppercase if c not in "IOQ")


def random_plate(rng: random.Random) -> str:
    """One plausible Indian plate, e.g. MH12AB1234.

    Shape is `SS NN LL NNNN`, which is the real format the edge agent's normaliser
    strips back down to 10 alphanumeric characters.
    """
    state = rng.choice(STATE_CODES)
    letters = rng.choice(PLATE_LETTERS) + rng.choice(PLATE_LETTERS)
    return f"{state[:2]}{state[2:]}{letters}{rng.randint(0, 9999):04d}"


def confidence(rng: random.Random) -> float:
    """A confidence in 0.0-1.0, skewed towards high.

    Rounded to 2dp because the contract's own worked example is 0.91, and a detector
    reporting 17 significant digits of a 0-100 style score is not a realistic fixture.
    """
    raw = rng.betavariate(4.0, CONFIDENCE_SKEW)
    return round(min(max(raw, 0.05), 0.99), 2)


# The plates the seeder puts on the blacklist. Every reason is intentionally different
# so the /alerts response can be checked for reason aggregation - a plate blacklisted
# for two reasons must come back with both, and the only way to test that is to give
# one plate two reasons.
#
# IMPORTANT: these are stored in NORMALISED form (uppercase, no spaces or dashes).
# Ingest matches the blacklist on the edge-normalised plate exactly, so seeding
# "mh 12 ab 1234" here would insert a row that can never match a real sighting.
TEST_BLACKLIST: tuple[tuple[str, str], ...] = (
    ("MH12CD5678", "stolen-vehicle"),
    ("MH12CD5678", "unpaid-toll"),  # second reason, same plate: tests aggregation
    ("KA05EE4321", "court-order"),
    ("DL01CA9999", "reported-stolen"),
    ("MH14GH7788", "police-wanted"),
    ("GJ01RS5566", "tax-evasion"),
)


def blacklisted_plates() -> list[str]:
    """The distinct blacklisted plates, in seed order."""
    seen: dict[str, None] = {}
    for plate, _reason in TEST_BLACKLIST:
        seen.setdefault(plate, None)
    return list(seen)


def is_blacklisted(plate: str) -> bool:
    return any(plate == p for p, _ in TEST_BLACKLIST)
