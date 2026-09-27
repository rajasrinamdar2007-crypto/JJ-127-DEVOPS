#!/usr/bin/env python3
"""Drop synthetic plate detections into the edge agents' inboxes, on a schedule.

This is the "publisher" role: it stands in for the OCR/detection component that is
out of scope, and it is the only thing in the harness that creates traffic. It writes
one JSON file per detection into each edge agent's inbox directory; the agent picks it
up on its next poll, normalises it, and publishes it to MQTT. Nothing here talks to
MQTT or to the database.

Modelling a vehicle, not a plate
-------------------------------
A sighting is not a random plate at a random camera. A real ANPR feed is dominated by
*the same vehicle being seen repeatedly*, so this simulates vehicles that drive a
route through the city, reappearing at the next camera along it. That is what makes a
trajectory worth looking at: one `plate_string` spread across several `camera_id`s at
increasing timestamps.

Why the hops are paced in real time
-----------------------------------
`edge-agent/app/normalize.py` stamps every event with `datetime.now()` and ignores any
timestamp in the file, so a detection cannot be backdated by writing one into the
inbox. That is deliberate on the agent's part - a detector should not get to dictate
when a sighting happened - and it is not worth weakening for a test harness.

The consequence is that the *spacing* between a vehicle's sightings is set by how long
this script sleeps, not by a field in the file. `--hop-seconds` therefore controls the
apparent speed of travel across the city. To build a 5-hop trajectory, run it for
5 * --hop-seconds.

Only `plate_string`, `confidence` and (optionally) `vehicle_type` are written. The
agent supplies `camera_id`, `lat`, `lon` and `timestamp` from its own configuration,
which is the whole point of the split: the detector knows a plate, the camera knows
where it is.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from plates import (  # noqa: E402  (path shim above must run first)
    VEHICLE_TYPES,
    VEHICLE_TYPE_WEIGHTS,
    blacklisted_plates,
    confidence,
    random_plate,
)

log = logging.getLogger("sim.generate")

# The camera keys are the directory names under edge-agent/data/, which is what the
# compose file bind-mounts. They are deliberately NOT the camera_ids: the agent derives
# its own camera_id from configuration, and writing one into the detection file would be
# both redundant and, if it disagreed with the agent's config, silently wrong.
#
# Ordered roughly north-west to south-east across Pune, so a route reads as a plausible
# drive across the city rather than a random walk.
DEFAULT_CAMERAS = ("hinjewadi", "baner", "kothrud")

# A share of detections that carry no vehicle_type at all. The contract makes it
# optional and the agent forwards it only when it is one of the four known classes, so
# this exercises the "detector could not classify it" path end to end - the API stores
# NULL, and the map has to cope.
UNCLASSIFIED_SHARE = 0.12

# How often a departing vehicle is one we know is on the blacklist. Low enough that
# alerts are clearly distinguishable from normal traffic, high enough that a short run
# reliably produces some.
BLACKLIST_SHARE = 0.08

# Fraction of vehicles that keep driving their route after reaching the end, rather
# than being retired. Real commuter traffic repeats; without this the map slowly
# empties as the run continues.
LOOP_SHARE = 0.45

_stop = False


def _handle_stop(signum, _frame) -> None:
    """Finish the current sleep, then exit cleanly.

    Without this, Ctrl-C during --hop-seconds would leave a half-written file in an
    inbox; the watcher would then pick it up as malformed JSON and log a rejection,
    which is noise that looks like a real bug.
    """
    global _stop
    _stop = True
    log.info("stop requested; finishing the current hop then exiting")


@dataclass
class Vehicle:
    """One simulated vehicle driving a route."""

    plate: str
    vehicle_type: str | None
    route: list[str]
    loops: bool
    # Index of the next camera to visit. Starts at 1: index 0 is where it "already was",
    # and emitting a sighting for the first hop from the first camera is fine too, so
    # the first sighting of a route is emitted at index 0 before advancing.
    next_stop: int = 0
    hops: int = 0
    blacklisted: bool = field(default=False, compare=False)

    def advance(self) -> str | None:
        """Return the camera this vehicle is next seen at, or None once retired."""
        if self.next_stop >= len(self.route):
            if not self.loops:
                return None
            self.next_stop = 0
        camera = self.route[self.next_stop]
        self.next_stop += 1
        self.hops += 1
        return camera


@dataclass(frozen=True)
class Tuning:
    """Traffic mix, so the shape of a run is controllable without editing this file.

    These are probabilities per *newly created vehicle*, not per detection: a vehicle
    keeps whatever class it was born with for its whole route, the way a real one does.
    """

    unclassified_share: float = UNCLASSIFIED_SHARE
    blacklist_share: float = BLACKLIST_SHARE
    loop_share: float = LOOP_SHARE


def build_route(rng: random.Random, cameras: list[str], tuning: Tuning) -> tuple[list[str], bool]:
    """Pick a plausible route through the camera set.

    Two options dominate real traffic: a short hop between neighbours, or the full
    cross-city run. A vehicle that visits all three is the interesting case, because it
    is what produces a multi-point trajectory.
    """
    if len(cameras) <= 2 or rng.random() < 0.35:
        start = rng.randrange(len(cameras))
        route = [cameras[start]]
    else:
        start = rng.randrange(len(cameras))
        route = list(cameras[start:]) + list(cameras[:start])
        if rng.random() < 0.4:
            # A partial route: joins the flow partway through.
            route = route[: max(2, len(route) - 1)]
    return route, rng.random() < tuning.loop_share


def make_vehicle(
    rng: random.Random,
    cameras: list[str],
    blacklisted_plates_pool: list[str],
    tuning: Tuning = Tuning(),
) -> Vehicle:
    route, loops = build_route(rng, cameras, tuning)

    if blacklisted_plates_pool and rng.random() < tuning.blacklist_share:
        # A blacklisted vehicle is otherwise completely ordinary: same route, same
        # pacing, same fields. The only difference is the plate, which is the point -
        # the alert has to be caused by the plate and nothing else in the pipeline.
        return Vehicle(
            plate=rng.choice(blacklisted_plates_pool),
            vehicle_type=rng.choices(VEHICLE_TYPES, VEHICLE_TYPE_WEIGHTS)[0],
            route=route,
            loops=loops,
            blacklisted=True,
        )

    vehicle_type = rng.choices(VEHICLE_TYPES, VEHICLE_TYPE_WEIGHTS)[0]
    if rng.random() < tuning.unclassified_share:
        # Omit the field entirely rather than sending null: the agent treats "absent"
        # and "unrecognised" identically, and absent is what a real detector sends when
        # its classifier abstains.
        vehicle_type = None

    return Vehicle(plate=random_plate(rng), vehicle_type=vehicle_type, route=route, loops=loops)


def detection_payload(vehicle: Vehicle, rng: random.Random) -> dict:
    """The JSON body of one detection file.

    Deliberately minimal - see the module docstring. `vehicle_type` is included only
    when we have one, because omitting it is a case the pipeline needs to handle.
    """
    payload: dict = {
        "plate_string": vehicle.plate,
        "confidence": confidence(rng),
    }
    if vehicle.vehicle_type:
        payload["vehicle_type"] = vehicle.vehicle_type
    return payload


def write_detection(inbox: Path, vehicle: Vehicle, rng: random.Random) -> Path:
    """Write one detection file, atomically.

    The temp-file-then-rename matters: the watcher globs `*.json` on a 1s poll, and a
    partially written file that happened to match the glob would be parsed as broken
    JSON and logged as a rejection. rename() within a directory is atomic, so the
    watcher only ever sees the complete file or nothing.
    """
    inbox.mkdir(parents=True, exist_ok=True)
    body = json.dumps(detection_payload(vehicle, rng), indent=2)
    # The unique token keeps two detections of the same plate within the same
    # millisecond from colliding on filename.
    name = f"sim-{vehicle.plate}-{uuid.uuid4().hex[:8]}.json"
    tmp = inbox / f".{name}.tmp"
    final = inbox / name
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, final)
    return final


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drop synthetic detections into the edge agents' inboxes.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        # Host default is the repo-relative path, so `python sim/generate_detections.py`
        # works from the repo root. The container sets SIM_DATA_DIR to its mount point
        # instead, which is why this is an env var and not a hard-coded path.
        default=Path(os.getenv("SIM_DATA_DIR", "edge-agent/data")),
        help="directory holding one subdirectory per edge agent",
    )
    p.add_argument(
        "--cameras",
        default=None,
        help=(
            "comma-separated inbox names to use, in geographic order; "
            "default = every directory under --data-dir, with known cameras kept in their "
            "north-west to south-east order"
        ),
    )
    p.add_argument(
        "--hop-seconds",
        type=float,
        default=3.0,
        help="seconds between a vehicle's sightings; also sets apparent travel speed",
    )
    p.add_argument(
        "--concurrent",
        type=int,
        default=4,
        help="vehicles in flight at once; each sleeps independently between hops",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="stop after this many seconds (0 = run until interrupted)",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=None,
        help="RNG seed, for a reproducible run",
    )
    p.add_argument(
        "--no-blacklist",
        action="store_true",
        help="never emit a blacklisted plate (to test the clean path only)",
    )
    p.add_argument(
        "--blacklist-share",
        type=float,
        default=BLACKLIST_SHARE,
        help="probability that a new vehicle gets a blacklisted plate (0-1); 1.0 floods alerts",
    )
    p.add_argument(
        "--unclassified-share",
        type=float,
        default=UNCLASSIFIED_SHARE,
        help="probability that a new vehicle is emitted with no vehicle_type (0-1)",
    )
    p.add_argument(
        "--loop-share",
        type=float,
        default=LOOP_SHARE,
        help="probability that a vehicle keeps driving its route instead of retiring",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="log every detection")
    return p.parse_args(argv)


def discover_cameras(data_dir: Path) -> list[str]:
    """Every camera directory under `data_dir`, in a sensible order.

    Discovered rather than hard-coded, so adding a camera to docker-compose.yml does not
    also mean remembering to edit this file - a new edge-agent that silently got no
    synthetic traffic would look like a broken agent rather than a missed config line.

    Order still matters, because it is the order a route is built in and that is what
    makes a journey read as a drive across the city instead of a random walk. Known
    cameras keep their hand-checked north-west to south-east order; anything new goes at
    the end, where its position is unknown, ordered by name so runs stay reproducible.
    """
    if not data_dir.is_dir():
        return []

    # A camera counts if it already has an inbox, or if its name is one we know and the
    # inbox is simply not created yet - the agent makes it on first run, and `main`
    # creates it too, so there is no reason to exclude it.
    found = {
        p.name
        for p in data_dir.iterdir()
        if p.is_dir() and ((p / "inbox").is_dir() or p.name in DEFAULT_CAMERAS)
    }
    if not found:
        return []

    ordered = [name for name in DEFAULT_CAMERAS if name in found]
    extra = sorted(found - set(ordered))
    if extra:
        log.info(
            "camera(s) not in the known route order, appended: %s - pass --cameras to place them",
            ", ".join(extra),
        )
    return ordered + extra


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    rng = random.Random(args.seed)
    if args.cameras:
        cameras = [c.strip() for c in args.cameras.split(",") if c.strip()]
    else:
        cameras = discover_cameras(args.data_dir)
    if not cameras:
        log.error(
            "no camera directories under %s - pass --cameras, or is the stack laid out as expected?",
            args.data_dir,
        )
        return 2

    inboxes = {}
    for name in cameras:
        path = args.data_dir / name / "inbox"
        if not path.parent.is_dir():
            log.error("no edge agent data directory at %s - is the stack laid out as expected?", path.parent)
            return 2
        path.mkdir(parents=True, exist_ok=True)
        inboxes[name] = path

    pool = [] if args.no_blacklist else blacklisted_plates()

    for name, value in (
        ("--blacklist-share", args.blacklist_share),
        ("--unclassified-share", args.unclassified_share),
        ("--loop-share", args.loop_share),
    ):
        if not 0.0 <= value <= 1.0:
            log.error("%s must be between 0 and 1, got %r", name, value)
            return 2

    tuning = Tuning(
        unclassified_share=args.unclassified_share,
        blacklist_share=args.blacklist_share,
        loop_share=args.loop_share,
    )

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    log.info("publishing to %d camera(s): %s", len(inboxes), ", ".join(cameras))
    log.info(
        "inboxes: %s",
        ", ".join(f"{n}={p}" for n, p in inboxes.items()),
    )
    log.info(
        "%d vehicle(s) in flight, %.1fs per hop%s",
        args.concurrent,
        args.hop_seconds,
        ", blacklist hits enabled" if pool else ", blacklist hits disabled",
    )
    log.info(
        "mix: %.0f%% unclassified, %.0f%% blacklisted, %.0f%% looping routes",
        tuning.unclassified_share * 100,
        tuning.blacklist_share * 100 if pool else 0.0,
        tuning.loop_share * 100,
    )
    if args.duration:
        log.info("stopping after %.0fs", args.duration)

    started = time.monotonic()
    vehicles: list[Vehicle] = [
        make_vehicle(rng, cameras, pool, tuning) for _ in range(max(1, args.concurrent))
    ]
    emitted = 0
    blacklisted_emitted = 0

    try:
        while not _stop:
            if args.duration and time.monotonic() - started >= args.duration:
                log.info("duration reached")
                break

            # Advance every vehicle by one hop, then wait once. The wait is shared so
            # the whole fleet steps together: a per-vehicle sleep with no barrier would
            # let one slow vehicle drift arbitrarily far behind the others.
            for index, vehicle in enumerate(vehicles):
                camera = vehicle.advance()
                if camera is None:
                    # Retired; replace it so the traffic rate stays constant.
                    vehicles[index] = make_vehicle(rng, cameras, pool, tuning)
                    vehicle = vehicles[index]
                    camera = vehicle.advance()
                    if camera is None:  # pragma: no cover - build_route always returns one
                        continue

                path = write_detection(inboxes[camera], vehicle, rng)
                emitted += 1
                if vehicle.blacklisted:
                    blacklisted_emitted += 1
                log.log(
                    logging.DEBUG if not args.verbose else logging.INFO,
                    "%-12s hop %d -> %-10s %s%s",
                    vehicle.plate,
                    vehicle.hops,
                    camera,
                    "BLACKLISTED " if vehicle.blacklisted else "",
                    path.name,
                )

            # Sleep in slices so a stop request is honoured promptly instead of after a
            # full --hop-seconds.
            deadline = time.monotonic() + args.hop_seconds
            while not _stop and time.monotonic() < deadline:
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    finally:
        log.info(
            "emitted %d detection(s), %d of them blacklisted, over %.0fs",
            emitted,
            blacklisted_emitted,
            time.monotonic() - started,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
