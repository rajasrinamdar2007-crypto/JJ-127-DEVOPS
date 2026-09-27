#!/usr/bin/env python3
"""Seed the blacklist with test plates, so the alerting path has something to hit.

Writes straight to the `blacklist` table. It does not publish anything: an alert is
raised by `ingest` when it stores a sighting whose plate is already listed, so
seeding the table is the only thing needed to arm the test. The generator
(`generate_detections.py`) then occasionally emits one of these plates and the alert
appears on its own.

Why this talks to the database directly, and not through the API
---------------------------------------------------------------
The GIS API is read-only by design and there is deliberately no endpoint to flag a
plate. Adding one just so a test script can use it would put a write path in front of
the one service that is supposed to have none.

Idempotency
-----------
`blacklist` is keyed on (plate_string, reason), so re-running this is a no-op apart
from the ON CONFLICT. That matters because the natural way to use it is: seed, watch
alerts fire, seed again after adding a plate by hand.

--clear removes only the plates this script seeded (matched by reason), so it will not
delete a blacklist entry someone added by hand.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))

from plates import TEST_BLACKLIST  # noqa: E402  (path shim above must run first)

log = logging.getLogger("sim.seed_blacklist")

# Same DSN variables the rest of the stack uses, so there is one set of names to learn.
# POSTGRES_PASSWORD deliberately has no fallback. A baked-in default would make this script
# succeed against a database whose real password is something else, and it would put a
# password literal in version control - which is how a credential ends up published in a
# public repository. Fail here instead, with a message that says what to do.
_db_password = os.getenv("POSTGRES_PASSWORD")
if not _db_password:
    sys.exit(
        "POSTGRES_PASSWORD is not set. Copy .env.example to .env, replace the "
        "CHANGEME-postgres placeholder with a password of your own, and source it "
        "(or run this through `make seed-blacklist`, which loads .env for you)."
    )

DSN = (
    f"host={os.getenv('POSTGRES_HOST', 'db')} "
    f"port={os.getenv('POSTGRES_PORT', '5432')} "
    f"user={os.getenv('POSTGRES_USER', 'anpruser')} "
    f"password={_db_password} "
    f"dbname={os.getenv('POSTGRES_DB', 'anpr')}"
)

INSERT = """
INSERT INTO blacklist (plate_string, reason)
VALUES (%s, %s)
ON CONFLICT (plate_string, reason) DO NOTHING
"""

# Only rows carrying one of our seeded reasons, so a hand-added entry survives --clear.
DELETE = "DELETE FROM blacklist WHERE reason = ANY(%s)"

COUNT = "SELECT count(*) FROM blacklist"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Insert test blacklist entries for the alerting test.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--clear", action="store_true", help="remove the seeded entries instead of adding them")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    reasons = sorted({reason for _plate, reason in TEST_BLACKLIST})

    try:
        with psycopg.connect(DSN) as conn:
            with conn.cursor() as cur:
                if args.clear:
                    cur.execute(DELETE, (reasons,))
                    removed = cur.rowcount
                    conn.commit()
                    log.info("removed %d seeded blacklist row(s)", removed)
                else:
                    added = 0
                    for plate, reason in TEST_BLACKLIST:
                        cur.execute(INSERT, (plate, reason))
                        # rowcount is 0 when the ON CONFLICT swallowed a re-run.
                        added += cur.rowcount
                    conn.commit()
                    log.info(
                        "seeded %d row(s) covering %d plate(s); %d already present",
                        added,
                        len({p for p, _ in TEST_BLACKLIST}),
                        len(TEST_BLACKLIST) - added,
                    )

                cur.execute(COUNT)
                total = cur.fetchone()[0]
    except psycopg.OperationalError as e:
        # The common case by far: run before `make up`, or the db is still starting.
        log.error("cannot reach the database at %s: %s", os.getenv("POSTGRES_HOST", "db"), e)
        log.error("is the stack up? try: docker compose up -d db")
        return 1

    log.info("blacklist now holds %d row(s) in total", total)
    for plate, reason in TEST_BLACKLIST:
        log.info("  %-12s %s", plate, reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
