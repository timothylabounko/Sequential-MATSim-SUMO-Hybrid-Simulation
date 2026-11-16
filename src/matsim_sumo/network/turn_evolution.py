"""Evolutionary repair of turning movements on an already corrected SUMO network.

The input is the network written by ``zurich_correction``. That file is read
and is not modified. A stress simulation supplies the gridlock log. Each stuck
approach becomes a set of candidate turns. TomTom's routing API decides
whether a candidate is a missing movement or an extra one. The evolutionary
search then tries subsets of those TomTom-supported edits. A turn is never
added or deleted from geometry alone, and the search does not start when
``TOMTOM_API_KEY`` is unset.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

TELEPORT_RE = re.compile(
    r"Teleporting vehicle '[^']+'; waited too long \(([^)]+)\), lane='([^']+)', time=([0-9]+(?:\.[0-9]+)?)"
)
COLLISION_RE = re.compile(
    r"collision with vehicle '[^']+', lane='([^']+)', .*?time=([0-9]+(?:\.[0-9]+)?)"
)
NO_CONNECTION_RE = re.compile(
    r"emergency stop at the end of lane '([^']+)' because there is no connection to the next edge"
)
DIRECT_DETOUR_FACTOR = 2.5
DIRECT_DETOUR_METRES = 250.0


@dataclass(frozen=True)
class Gridlock:
    kind: str
    lane: str
    time: float | None = None

    @property
    def edge(self) -> str:
        lane = self.lane
        if lane.startswith(":"):
            return lane
        return lane.rsplit("_", 1)[0]


@dataclass(frozen=True)
class Maneuver:
    """One turning movement at a junction that produced a gridlock."""

    junction: str
    street: str
    from_edge: str
    to_edge: str
    present: bool


@dataclass(frozen=True)
class TomTomReading:
    possible: bool | None
    route_m: float | None = None
    direct_m: float | None = None
    error: str = ""


@dataclass(frozen=True)
class TurnEdit:
    action: str
    maneuver: Maneuver

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.action, self.maneuver.from_edge, self.maneuver.to_edge)


def parse_gridlocks(log_text: str) -> list[Gridlock]:
    """Read jam, yield, wrong-lane, collision, and missing-connection events."""
    found: list[Gridlock] = []
    for reason, lane, time in TELEPORT_RE.findall(log_text):
        found.append(Gridlock(reason, lane, float(time)))
    for lane, time in COLLISION_RE.findall(log_text):
        found.append(Gridlock("collision", lane, float(time)))
    for lane in NO_CONNECTION_RE.findall(log_text):
        found.append(Gridlock("no connection", lane, None))
    return found


def direct_movement_possible(route_m: float, direct_m: float) -> bool:
    """A short TomTom route uses the movement. A long detour does not."""
    if direct_m <= 0:
        return False
    limit = max(direct_m * DIRECT_DETOUR_FACTOR, direct_m + DIRECT_DETOUR_METRES)
    return route_m <= limit


def reading_from_route(route_m: float | None, direct_m: float, error: str = "") -> TomTomReading:
    if route_m is None:
        return TomTomReading(None, None, direct_m, error or "no route")
    return TomTomReading(direct_movement_possible(route_m, direct_m), route_m, direct_m, error)


def edits_from_readings(maneuvers: list[Maneuver], readings: list[TomTomReading]) -> list[TurnEdit]:
    """Keep only the edits TomTom supports. An unread maneuver is left unchanged."""
    if len(maneuvers) != len(readings):
        raise ValueError("Each maneuver needs one TomTom reading")
    edits: list[TurnEdit] = []
    for maneuver, reading in zip(maneuvers, readings):
        if reading.possible is None:
            continue
        if maneuver.present and not reading.possible:
            edits.append(TurnEdit("delete", maneuver))
        elif not maneuver.present and reading.possible:
            edits.append(TurnEdit("add", maneuver))
    return edits


def _fitness_of(chromosome: tuple[bool, ...], fitness_fn) -> float:
    return float(fitness_fn(chromosome))


def evolve(
    edit_count: int,
    fitness_fn,
    rng: random.Random,
    population_size: int = 8,
    generations: int = 6,
    patience: int = 2,
    review_fn=None,
) -> dict:
    """Search subsets of the supported edits. Lower fitness is better.

    The chromosome is a tuple of booleans, one per edit, in the order supplied
    by the caller. ``fitness_fn`` receives that tuple and returns the gridlock
    count of the network produced by the selected edits. After every
    individual in an iteration, ``review_fn`` compares that network with
    OpenStreetMap. It returns false when the network still has a link to
    nowhere or is disconnected. A rejected individual is not kept and cannot
    become the best of the iteration. The search keeps the two best accepted
    individuals, crosses the rest uniformly, and flips one gene. It stops
    after ``patience`` iterations without a better accepted fitness, or after
    ``generations``, or when the best fitness is zero.
    """
    if edit_count < 0:
        raise ValueError("edit_count must be non-negative")
    if edit_count == 0:
        baseline = _fitness_of(tuple(), fitness_fn)
        accepted = review_fn is None or bool(review_fn(tuple()))
        if not accepted:
            baseline = 10**12
        return {
            "best": tuple(),
            "best_fitness": baseline,
            "generations": [{"best": baseline, "accepted": accepted, "checked": 1, "rejected": 0 if accepted else 1}],
            "stopped": "no edits",
        }

    def fresh() -> tuple[bool, ...]:
        return tuple(bool(rng.randrange(2)) for _ in range(edit_count))

    def evaluate(person: tuple[bool, ...]) -> tuple[tuple[bool, ...], float, bool]:
        score = _fitness_of(person, fitness_fn)
        accepted = review_fn is None or bool(review_fn(person))
        if not accepted:
            score = 10**12
        return person, score, accepted

    population = [tuple(False for _ in range(edit_count)), fresh()]
    while len(population) < population_size:
        population.append(fresh())
    scored = [evaluate(person) for person in population]

    def best_accepted(rows: list[tuple[tuple[bool, ...], float, bool]]):
        kept = [row for row in rows if row[2]]
        if not kept:
            return None
        return min(kept, key=lambda item: item[1])

    def generation_record(rows: list[tuple[tuple[bool, ...], float, bool]]) -> dict:
        winner = best_accepted(rows)
        rejected = sum(1 for _person, _score, accepted in rows if not accepted)
        if winner is None:
            return {"best": 10**12, "accepted": False, "checked": len(rows), "rejected": rejected}
        return {"best": winner[1], "accepted": True, "checked": len(rows), "rejected": rejected}

    chosen = best_accepted(scored)
    if chosen is None:
        best_person, best_fitness = population[0], 10**12
    else:
        best_person, best_fitness = chosen[0], chosen[1]
    history = [generation_record(scored)]
    quiet = 0
    for _ in range(generations):
        if best_fitness == 0:
            break
        ranked = sorted(scored, key=lambda item: item[1])
        accepted_rows = [row for row in ranked if row[2]]
        elites = [person for person, _score, _accepted in accepted_rows[:2]]
        children: list[tuple[bool, ...]] = []
        while len(elites) + len(children) < population_size:
            if accepted_rows:
                left = rng.choice(accepted_rows)[0]
                right = rng.choice(accepted_rows)[0]
            else:
                left = fresh()
                right = fresh()
            child = tuple(left[i] if rng.randrange(2) else right[i] for i in range(edit_count))
            flip = rng.randrange(edit_count)
            genes = list(child)
            genes[flip] = not genes[flip]
            children.append(tuple(genes))
        population = elites + children
        scored = [evaluate(person) for person in population]
        record = generation_record(scored)
        history.append(record)
        winner = best_accepted(scored)
        if winner is not None and winner[1] < best_fitness:
            best_person, best_fitness = winner[0], winner[1]
            quiet = 0
        else:
            quiet += 1
            if quiet >= patience:
                break
    stopped = "zero gridlocks" if best_fitness == 0 else "no further improvement"
    return {
        "best": best_person,
        "best_fitness": best_fitness,
        "generations": history,
        "stopped": stopped,
    }


def tomtom_route_metres(origin: tuple[float, float], destination: tuple[float, float], api_key: str) -> float:
    """Drive from ``origin`` to ``destination`` and return the route length in metres.

    Coordinates are latitude, longitude. Traffic is off so the reply answers
    whether the movement exists, not how delayed it is.
    """
    locations = f"{origin[0]},{origin[1]}:{destination[0]},{destination[1]}"
    query = urllib.parse.urlencode(
        {"key": api_key, "travelMode": "car", "traffic": "false", "routeType": "fastest"}
    )
    url = f"https://api.tomtom.com/routing/1/calculateRoute/{locations}/json?{query}"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"TomTom routing failed with HTTP {exc.code}: {detail}") from exc
    summary = payload["routes"][0]["summary"]
    return float(summary["lengthInMeters"])


def search_turns(maneuvers: list[Maneuver], readings: list[TomTomReading], fitness_fn, rng: random.Random) -> dict:
    """Run the evolutionary search on TomTom-supported edits only."""
    edits = edits_from_readings(maneuvers, readings)
    if any(reading.possible is None for reading in readings):
        return {
            "edits": [],
            "applied": [],
            "best_fitness": None,
            "generations": [],
            "stopped": "TomTom key missing",
        }
    result = evolve(len(edits), fitness_fn, rng)
    selected = [edit for edit, keep in zip(edits, result["best"]) if keep]
    result["edits"] = [{"action": edit.action, "from": edit.maneuver.from_edge, "to": edit.maneuver.to_edge} for edit in edits]
    result["applied"] = [
        {"action": edit.action, "from": edit.maneuver.from_edge, "to": edit.maneuver.to_edge} for edit in selected
    ]
    result["best"] = list(result["best"])
    return result


def summarise(gridlocks: list[Gridlock]) -> dict:
    counts: dict[str, int] = {}
    for item in gridlocks:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    return {"events": len(gridlocks), "by_kind": counts}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Record gridlocks on the already corrected Zürich network and search "
            "TomTom-supported turn edits. The corrected network is not modified. "
            "Without TOMTOM_API_KEY the search records the gridlocks and stops."
        )
    )
    parser.add_argument("--log", type=Path, required=True, help="SUMO log from the corrected network")
    parser.add_argument("--output", type=Path, required=True, help="JSON report to write")
    args = parser.parse_args(argv)
    log_text = args.log.read_text(encoding="utf-8", errors="replace")
    gridlocks = parse_gridlocks(log_text)
    key = os.environ.get("TOMTOM_API_KEY", "").strip()
    report = {
        "network": "corrected network from zurich_correction, before this search",
        "gridlocks": summarise(gridlocks),
        "tomtom_key_present": bool(key),
        "edits_applied": [],
        "stopped": "TomTom key missing" if not key else "no routing requests submitted",
        "note": (
            "No turn was added or deleted. An edit is accepted only after a TomTom "
            "routing reply, and this run did not submit routing requests."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["gridlocks"]))
    print(report["stopped"])


if __name__ == "__main__":
    main()
