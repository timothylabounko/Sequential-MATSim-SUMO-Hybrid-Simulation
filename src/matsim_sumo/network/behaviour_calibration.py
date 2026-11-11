"""Calibrate driver behaviour on the repaired network at full traffic.

Runs after the network repair and the signal timing, so a behaviour setting
cannot hide a lane or connection error. Only how drivers follow, yield and
change lanes is changed: the network, its lanes and the signal programs given
with ``--additional`` are never edited, and no setting lets a driver ignore a
red light.

The genes are SUMO car-following, junction and lane-change parameters, each
with a range around its SUMO default (``GENES``). An evolutionary search
(population ``--population``, two elites, tournament selection, uniform
crossover, one gene redrawn per child) minimises

    fitness = teleport weight * teleported share (%) + not-inserted share (%)
              + max(0, |travel-time bias| (%) - tolerance)
              + collision weight * max(0, collisions - collisions of the SUMO default driver)

``--genes`` limits the search to some genes; the others keep their SUMO
default. ``--collision-weight`` (0 by default) stops a setting from trading
jams for collisions; collisions the default driver also has are network
errors, not behaviour, and cost nothing. Wrong-lane teleports are left out of
the penalty for the same reason. ``--teleport-weight`` (1 by default) makes
the teleported share count more than vehicles that could not enter.
``--seeds`` simulates every individual once per seed and scores the mean, so
one lucky seed cannot pick the best setting; with two or more seeds the
second seed's run of the best setting is the seed check.

Both shares come from SUMO's statistic output: teleported vehicles per
inserted vehicle, and vehicles that could not enter the network before the
end per loaded vehicle. The travel-time bias compares SUMO with Google Routes
(traffic-aware, at the same weekday time) on a fixed sample of the simulated
trips, as Baggenstos (2026) did with TomTom. It stops the search from removing
congestion that the real network has. The SUMO defaults and the micro settings
of Baggenstos (``ignore-junction-blocker`` 1 s, ``time-to-impatience`` 30 s)
start in the first population.

The trips are routed once with ``duarouter``; every run then uses the
settings of ``simulation.py``, and ``best.sumocfg`` repeats the best run
exactly. Afterwards the best setting is simulated again with a second random
seed. Approaches that still stop vehicles in both runs are written to
``hotspots.csv``: they are the capacity limits left for the discussion.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import json
import math
import os
import random
import re
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import sumolib

from matsim_sumo.network.google_turn_search import ROUTES_URL, google_route_metres
from matsim_sumo.network.national_repair import KEY_CHECK, NETWORK_ERROR_KINDS, describe_gridlocks, split_kinds
from matsim_sumo.network.simulation import route_trips, simulate, write_config
from matsim_sumo.network.turn_evolution import parse_gridlocks, summarise


@dataclass(frozen=True)
class Gene:
    name: str
    low: float
    high: float
    default: float
    where: str  # "vType" attribute or SUMO "option"
    digits: int = 2
    zero_is_off: bool = False  # 0 is written as SUMO's -1 (never)


GENES: tuple[Gene, ...] = (
    Gene("tau", 0.8, 1.2, 1.0, "vType"),
    Gene("sigma", 0.1, 0.5, 0.5, "vType"),
    Gene("impatience", 0.0, 1.0, 0.0, "vType"),
    Gene("jmTimegapMinor", 0.5, 1.5, 1.0, "vType"),
    Gene("jmIgnoreKeepClearTime", 0, 60, 0, "vType", 0, zero_is_off=True),
    Gene("lcStrategic", 0.5, 5.0, 1.0, "vType"),
    Gene("lcCooperative", 0.5, 1.0, 1.0, "vType"),
    Gene("lcAssertive", 1.0, 5.0, 1.0, "vType"),
    Gene("lcLookaheadLeft", 1.0, 4.0, 2.0, "vType"),
    Gene("time-to-impatience", 30, 300, 180, "option", 0),
    Gene("ignore-junction-blocker", 0, 60, 0, "option", 0, zero_is_off=True),
)


def sumo_value(gene: Gene, value: float) -> str:
    if gene.zero_is_off and value <= 0:
        return "-1"
    return str(int(value)) if gene.digits == 0 else f"{value:g}"
BAGGENSTOS = {"time-to-impatience": 30, "ignore-junction-blocker": 1}
SUMO_SEED = 23
CHECK_SEED = 42
HOTSPOT_MIN = 5

Individual = tuple[float, ...]


def default_individual() -> Individual:
    return tuple(gene.default for gene in GENES)


def baggenstos_individual() -> Individual:
    return tuple(BAGGENSTOS.get(gene.name, gene.default) for gene in GENES)


def draw(gene: Gene, rng: random.Random) -> float:
    return round(rng.uniform(gene.low, gene.high), gene.digits)


def as_dict(individual: Individual) -> dict[str, float]:
    return {gene.name: value for gene, value in zip(GENES, individual)}


def sumo_options(individual: Individual) -> list[str]:
    options = []
    for gene, value in zip(GENES, individual):
        if gene.where == "option":
            options += [f"--{gene.name}", sumo_value(gene, value)]
    return options


VTYPE = re.compile(r"<vType\b[^>]*?/>")
ATTRIBUTE = re.compile(r'(\w+)="([^"]*)"')


def with_behaviour(routes_text: str, individual: Individual) -> str:
    """The routes text with the vType genes set on every vehicle type."""
    values = {gene.name: sumo_value(gene, value) for gene, value in zip(GENES, individual) if gene.where == "vType"}

    def rewrite(match: re.Match) -> str:
        attributes = dict(ATTRIBUTE.findall(match.group(0)))
        attributes.update(values)
        return "<vType " + " ".join(f'{key}="{value}"' for key, value in attributes.items()) + "/>"

    text, count = VTYPE.subn(rewrite, routes_text)
    if not count:
        raise ValueError("the routes file defines no vType to calibrate")
    return text


def next_weekday_morning(
    now: dt.datetime | None = None, timezone: str = "Europe/Zurich", start: dt.time = dt.time(7, 0)
) -> dt.datetime:
    """The next Tuesday at ``start`` in ``timezone``, at least one day ahead, as Google needs a future time."""
    zone = ZoneInfo(timezone)
    now = (now or dt.datetime.now(zone)).astimezone(zone)
    day = now.date() + dt.timedelta(days=1)
    while day.weekday() != 1:
        day += dt.timedelta(days=1)
    return dt.datetime.combine(day, start, tzinfo=zone)


def _end_point(edge, at_end: bool) -> tuple[tuple[float, float], float]:
    shape = edge.getShape()
    (ax, ay), (bx, by) = (shape[-2], shape[-1]) if at_end else (shape[0], shape[1])
    heading = (math.degrees(math.atan2(bx - ax, by - ay)) + 360) % 360
    return ((bx, by) if at_end else (ax, ay)), heading


def sample_trips(routes: Path, net, size: int, before_s: float, seed: int) -> list[dict]:
    """Fixed random sample of trips that depart early enough to arrive in the window."""
    candidates = []
    for trip in ET.parse(routes).getroot().iter("trip"):
        depart = float(trip.get("depart", "0"))
        frm, to = trip.get("from"), trip.get("to")
        if depart < before_s and net.hasEdge(frm) and net.hasEdge(to) and frm != to:
            candidates.append((trip.get("id"), depart, frm, to))
    rng = random.Random(seed)
    picked = rng.sample(candidates, min(size, len(candidates)))
    sample = []
    for vid, depart, frm, to in picked:
        origin_xy, origin_heading = _end_point(net.getEdge(frm), at_end=False)
        destination_xy, destination_heading = _end_point(net.getEdge(to), at_end=True)
        olon, olat = net.convertXY2LonLat(*origin_xy)
        dlon, dlat = net.convertXY2LonLat(*destination_xy)
        sample.append(
            {
                "id": vid,
                "depart": depart,
                "origin": [olat, olon, origin_heading],
                "destination": [dlat, dlon, destination_heading],
            }
        )
    return sample


def google_travel_time(trip: dict, departure: dt.datetime, api_key: str) -> float | None:
    """Traffic-aware Google driving time in seconds for one sampled trip."""

    def waypoint(point: list[float]) -> dict:
        return {"location": {"latLng": {"latitude": point[0], "longitude": point[1]}, "heading": int(point[2])}}

    body = json.dumps(
        {
            "origin": waypoint(trip["origin"]),
            "destination": waypoint(trip["destination"]),
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE",
            "departureTime": departure.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        ROUTES_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Google Routes failed with HTTP {exc.code}: {detail}") from exc
    routes = payload.get("routes") or []
    if not routes or "duration" not in routes[0]:
        return None
    return float(str(routes[0]["duration"]).rstrip("s"))


def google_reference(
    sample: list[dict],
    cache_path: Path,
    api_key: str,
    timezone: str = "Europe/Zurich",
    start: dt.time = dt.time(7, 0),
) -> dict[str, float]:
    """Google times for the sample, asked once and kept in ``cache_path``."""
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
    morning = cache.get("_morning") or next_weekday_morning(timezone=timezone, start=start).isoformat()
    cache["_morning"] = morning
    start = dt.datetime.fromisoformat(morning)
    for index, trip in enumerate(sample, 1):
        if trip["id"] in cache:
            continue
        seconds = google_travel_time(trip, start + dt.timedelta(seconds=trip["depart"]), api_key)
        cache[trip["id"]] = seconds
        print(f"google travel time {index}/{len(sample)} {trip['id']}: {seconds} s", flush=True)
        cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return {trip["id"]: cache[trip["id"]] for trip in sample if cache.get(trip["id"])}


def sumo_travel_times(tripinfo: Path, ids: set[str]) -> dict[str, float]:
    """Trip durations; a vehicle still driving at the end counts with the time it has driven so far."""
    times = {}
    if not tripinfo.is_file():
        return times
    for _event, element in ET.iterparse(tripinfo, events=("end",)):
        if element.tag == "tripinfo" and element.get("id") in ids:
            times[element.get("id")] = float(element.get("duration", "0"))
        element.clear()
    return times


def travel_time_error(sumo: dict[str, float], google: dict[str, float]) -> dict:
    pairs = [(sumo[key], google[key]) for key in google if key in sumo and google[key] > 0]
    if not pairs:
        return {"trips": 0, "bias_percent": None, "mre_percent": None, "rmse_s": None}
    mean_sumo = sum(s for s, _g in pairs) / len(pairs)
    mean_google = sum(g for _s, g in pairs) / len(pairs)
    return {
        "trips": len(pairs),
        "missing": len(google) - len(pairs),
        "mean_sumo_s": round(mean_sumo, 1),
        "mean_google_s": round(mean_google, 1),
        "bias_percent": round(100 * (mean_sumo - mean_google) / mean_google, 2),
        "mre_percent": round(100 * sum((s - g) / g for s, g in pairs) / len(pairs), 2),
        "rmse_s": round(math.sqrt(sum((s - g) ** 2 for s, g in pairs) / len(pairs)), 1),
    }


def score(
    teleported: float | None,
    not_inserted: float | None,
    bias: float | None,
    tolerance: float,
    collisions: float = 0.0,
    collision_weight: float = 0.0,
    collision_baseline: float = 0.0,
    teleport_weight: float = 1.0,
) -> float:
    if teleported is None or not_inserted is None:
        return float("inf")
    penalty = max(0.0, abs(bias) - tolerance) if bias is not None else 100.0
    extra_collisions = collision_weight * max(0.0, collisions - collision_baseline)
    return round(teleport_weight * teleported + not_inserted + penalty + extra_collisions, 3)


def mean_result(runs: list[dict]) -> dict:
    """Numbers of several runs averaged; text and nested results of the first run, averaged where numeric."""
    first = runs[0]
    combined = {}
    for key, value in first.items():
        if key in ("label", "seed", "genes", "fitness"):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, dict)):
            combined[key] = value
        elif isinstance(value, dict):
            values = [run.get(key, {}) for run in runs]
            nested = {}
            for name in set().union(*values):
                numbers = [v.get(name) for v in values]
                if all(isinstance(number, (int, float)) and not isinstance(number, bool) for number in numbers):
                    nested[name] = round(sum(numbers) / len(numbers), 2)
                else:
                    nested[name] = None
            combined[key] = nested
        else:
            numbers = [run.get(key, 0) or 0 for run in runs]
            combined[key] = round(sum(numbers) / len(numbers), 2)
    return combined


def active_genes(names: str | None) -> tuple[int, ...]:
    """Indices of the genes the search may change; all of them when ``names`` is empty."""
    if not names:
        return tuple(range(len(GENES)))
    known = {gene.name: index for index, gene in enumerate(GENES)}
    wanted = [name.strip() for name in names.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in known]
    if unknown:
        raise ValueError(f"unknown genes {', '.join(unknown)}; known: {', '.join(known)}")
    return tuple(sorted(known[name] for name in wanted))


class Search:
    """Simulates individuals once per seed in ``seeds``; an individual's result is the mean of its runs."""

    def __init__(self, args, google: dict[str, float], routed: Path, seeds: tuple[int, ...] = (SUMO_SEED,)):
        self.args = args
        self.google = google
        self.routes_text = routed.read_text(encoding="utf-8")
        self.additional = tuple(args.additional)
        self.seeds = tuple(seeds)
        self.runs: dict[tuple[Individual, int], dict] = {}
        self.results: dict[Individual, dict] = {}
        self.labels: dict[Individual, str] = {}
        self.collision_baseline = 0.0
        self.lock = threading.Lock()
        self.count = 0

    def label_for(self, individual: Individual) -> str:
        with self.lock:
            if individual not in self.labels:
                self.count += 1
                self.labels[individual] = f"ind{self.count:03d}"
            return self.labels[individual]

    def fitness(self, result: dict) -> float:
        if "error" in result:
            return float("inf")
        return score(
            result["teleported_percent"],
            result["not_inserted_percent"],
            result["travel_time"]["bias_percent"],
            self.args.tolerance,
            result.get("collisions", 0),
            getattr(self.args, "collision_weight", 0.0),
            self.collision_baseline,
            getattr(self.args, "teleport_weight", 1.0),
        )

    def set_collision_baseline(self, individual: Individual) -> float:
        """Collisions of ``individual`` (the SUMO default) are free; only more than that cost fitness."""
        self.collision_baseline = float(self.results[individual].get("collisions", 0) or 0)
        for result in self.results.values():
            result["fitness"] = self.fitness(result)
        return self.collision_baseline

    def evaluate(self, individual: Individual, seed: int = SUMO_SEED, label: str | None = None) -> dict:
        key = (individual, seed)
        with self.lock:
            if key in self.runs:
                return self.runs[key]
        if label is None:
            base = self.label_for(individual)
            label = base if seed == SUMO_SEED else f"{base}-seed{seed}"
        run_dir = self.args.work_dir / label
        run_dir.mkdir(parents=True, exist_ok=True)
        routes = run_dir / "behaviour.rou.xml"
        routes.write_text(with_behaviour(self.routes_text, individual), encoding="utf-8")
        log = run_dir / "sim.log"
        tripinfo = run_dir / "tripinfo.xml"
        extra = (
            *sumo_options(individual),
            "--tripinfo-output",
            str(tripinfo),
            "--tripinfo-output.write-unfinished",
            "true",
            "--vehroute-output",
            str(run_dir / "vehroutes.xml.gz"),
            "--vehroute-output.last-route",
            "true",
            "--vehroute-output.write-unfinished",
            "true",
        )
        try:
            stats = simulate(
                self.args.net, routes, log, self.args.end, self.args.teleport, self.additional, extra, seed
            )
        except (RuntimeError, OSError) as exc:
            print(f"{label} failed: {exc}", flush=True)
            result = {"label": label, "genes": as_dict(individual), "fitness": float("inf"), "error": str(exc)}
        else:
            error = travel_time_error(sumo_travel_times(tripinfo, set(self.google)), self.google)
            by_kind = summarise(parse_gridlocks(log.read_text(encoding="utf-8", errors="replace")))["by_kind"]
            network, capacity = split_kinds(by_kind)
            result = {
                "label": label,
                "seed": seed,
                "genes": as_dict(individual),
                **stats,
                "by_kind": by_kind,
                "network_errors": network,
                "capacity": capacity,
                "travel_time": error,
            }
            result["fitness"] = self.fitness(result)
            print(f"{label}: {describe(result)} -> fitness {result['fitness']}", flush=True)
            tripinfo.unlink(missing_ok=True)
        routes.unlink(missing_ok=True)
        with self.lock:
            self.runs[key] = result
        return result

    def combine(self, individual: Individual) -> dict:
        runs = [self.runs[(individual, seed)] for seed in self.seeds]
        label = self.label_for(individual)
        failed = [run for run in runs if "error" in run]
        if failed:
            return {"label": label, "genes": as_dict(individual), "fitness": float("inf"), "error": failed[0]["error"]}
        if len(runs) == 1:
            result = dict(runs[0])
        else:
            result = {"label": label, "genes": as_dict(individual), "seeds": list(self.seeds), **mean_result(runs)}
        result["fitness"] = self.fitness(result)
        if len(runs) > 1:
            print(f"{label} mean of seeds {', '.join(map(str, self.seeds))}: {describe(result)} -> fitness {result['fitness']}", flush=True)
        return result

    def evaluate_all(self, individuals: list[Individual]) -> list[dict]:
        todo = [individual for individual in dict.fromkeys(individuals) if individual not in self.results]
        for individual in todo:
            self.label_for(individual)
        tasks = [(individual, seed) for individual in todo for seed in self.seeds]
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.args.workers) as pool:
            list(pool.map(lambda task: self.evaluate(*task), tasks))
        for individual in todo:
            self.results[individual] = self.combine(individual)
        return [self.results[individual] for individual in individuals]


def describe(result: dict) -> str:
    tt = result.get("travel_time", {})
    return (
        f"teleported {result['teleported_percent']} % ({result['teleports']} of {result['inserted']}: "
        f"jam {result['teleports_jam']}, yield {result['teleports_yield']}, wrong lane {result['teleports_wrongLane']}, "
        f"collision {result['teleports_collision']}), not inserted {result['not_inserted_percent']} % "
        f"({result['waiting']} of {result['loaded']}), travel-time bias {tt.get('bias_percent')} %"
    )


def evolve(
    search: Search,
    rng: random.Random,
    population: int,
    generations: int,
    elites: int = 2,
    active: tuple[int, ...] | None = None,
) -> list[dict]:
    active = tuple(range(len(GENES))) if active is None else active

    def only_active(individual: Individual) -> Individual:
        return tuple(value if index in active else GENES[index].default for index, value in enumerate(individual))

    seeds = [default_individual(), only_active(baggenstos_individual())]
    individuals = list(dict.fromkeys(seeds))
    tries = 0
    while len(individuals) < population and tries < 50 * population:
        tries += 1
        drawn = only_active(tuple(draw(gene, rng) for gene in GENES))
        if drawn not in individuals:
            individuals.append(drawn)
    history = []
    for generation in range(1, generations + 1):
        print(f"generation {generation}: {len(individuals)} individuals", flush=True)
        results = search.evaluate_all(individuals)
        ranked = sorted(zip(individuals, results), key=lambda pair: (pair[1]["fitness"], pair[1]["label"]))
        history.append({"generation": generation, "best": ranked[0][1], "fitness": [r["fitness"] for _i, r in ranked]})
        print(f"generation {generation}: best {ranked[0][1]['label']} fitness {ranked[0][1]['fitness']}", flush=True)
        if generation == generations:
            break

        def pick() -> Individual:
            a, b = rng.sample(ranked, 2)
            return a[0] if a[1]["fitness"] <= b[1]["fitness"] else b[0]

        children = [individual for individual, _r in ranked[:elites]]
        tries = 0
        while len(children) < population and tries < 50 * population:
            tries += 1
            mother, father = pick(), pick()
            child = [m if rng.random() < 0.5 else f for m, f in zip(mother, father)]
            index = rng.choice(active)
            child[index] = draw(GENES[index], rng)
            child = tuple(child)
            if child not in children and child not in search.results:
                children.append(child)
        individuals = children
    return history


def hotspots(net, first_log: Path, second_log: Path) -> list[dict]:
    """Approaches with at least HOTSPOT_MIN stuck vehicles in both runs."""
    first = {row["edge"]: row for row in describe_gridlocks(net, first_log.read_text(encoding="utf-8", errors="replace"))}
    second = {row["edge"]: row for row in describe_gridlocks(net, second_log.read_text(encoding="utf-8", errors="replace"))}
    rows = []
    for edge, row in first.items():
        other = second.get(edge)
        if row["vehicles"] < HOTSPOT_MIN or not other or other["vehicles"] < HOTSPOT_MIN:
            continue
        kinds = row["kinds"] + " | " + other["kinds"]
        network = any(kind in kinds for kind in NETWORK_ERROR_KINDS)
        rows.append(
            {
                **{key: row[key] for key in ("approach", "junction_with", "edge", "junction", "lat", "lon")},
                "traffic_light": net.getNode(row["junction"]).getType().startswith("traffic_light"),
                "vehicles_seed_a": row["vehicles"],
                "vehicles_seed_b": other["vehicles"],
                "kinds_seed_a": row["kinds"],
                "kinds_seed_b": other["kinds"],
                "type": "includes network error" if network else "capacity limit",
            }
        )
    rows.sort(key=lambda row: -(row["vehicles_seed_a"] + row["vehicles_seed_b"]))
    return rows


def write_gui_config(work_dir: Path, net: Path, routes_text: str, individual: Individual, args) -> Path:
    """``best.sumocfg``: the best run with the same routes, signals, options and seed."""
    routes = work_dir / "best.rou.xml"
    routes.write_text(with_behaviour(routes_text, individual), encoding="utf-8")
    return write_config(
        work_dir / "best.sumocfg",
        net,
        routes,
        args.end,
        args.teleport,
        tuple(args.additional),
        tuple(sumo_options(individual)),
        SUMO_SEED,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--net", type=Path, required=True, help="Repaired network (read only)")
    parser.add_argument("--routes", type=Path, required=True, help="Scaled trips with vType definitions")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--additional", type=Path, action="append", default=[], help="Signal programs (signals.add.xml)")
    parser.add_argument("--end", type=int, default=3600)
    parser.add_argument("--teleport", type=int, default=300, help="Seconds without moving before a vehicle counts as stuck")
    parser.add_argument("--population", type=int, default=6)
    parser.add_argument("--generations", type=int, default=3)
    parser.add_argument("--workers", type=int, default=2, help="Simulations run at the same time")
    parser.add_argument("--sample", type=int, default=60, help="Trips compared with Google")
    parser.add_argument("--tolerance", type=float, default=10.0, help="Travel-time bias allowed without penalty, %%")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--genes", default="", help="Comma-separated genes to search, e.g. impatience,jmTimegapMinor")
    parser.add_argument(
        "--collision-weight",
        type=float,
        default=0.0,
        help="Fitness points per collision above the SUMO default driver's collisions",
    )
    parser.add_argument("--teleport-weight", type=float, default=1.0, help="Weight of the teleported share in the fitness")
    parser.add_argument("--timezone", default="Europe/Zurich", help="Time zone of the simulated morning, for Google")
    parser.add_argument("--morning", default="07:00", help="Local time at which the simulation starts, for Google")
    parser.add_argument(
        "--seeds",
        default=str(SUMO_SEED),
        help=f"Comma-separated SUMO seeds; each individual is the mean of one run per seed (must include {SUMO_SEED})",
    )
    args = parser.parse_args(argv)
    try:
        active = active_genes(args.genes)
        seeds = tuple(dict.fromkeys(int(item) for item in args.seeds.split(",") if item.strip()))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if SUMO_SEED not in seeds:
        raise SystemExit(f"--seeds must include {SUMO_SEED}, the seed of best.sumocfg")
    seeds = (SUMO_SEED, *[seed for seed in seeds if seed != SUMO_SEED])
    args.work_dir.mkdir(parents=True, exist_ok=True)

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GOOGLE_MAPS_API_KEY is not set")
    try:
        google_route_metres(*KEY_CHECK, api_key)
    except (RuntimeError, OSError) as exc:
        raise SystemExit(f"Google rejected GOOGLE_MAPS_API_KEY: {exc}") from exc

    print("routing the trips once (duarouter)", flush=True)
    routed = args.work_dir / "routes" / "routed.rou.xml"
    unroutable = route_trips(args.net, args.routes, routed)
    print(f"  {unroutable} trips without any path are left out", flush=True)

    print("reading the network for the travel-time sample", flush=True)
    net = sumolib.net.readNet(str(args.net))
    sample = sample_trips(args.routes, net, args.sample, args.end / 2, args.seed)
    google = google_reference(
        sample, args.work_dir / "google-times.json", api_key, args.timezone, dt.time.fromisoformat(args.morning)
    )
    print(f"google travel times for {len(google)} of {len(sample)} sampled trips", flush=True)

    search = Search(args, google, routed, seeds)
    print(
        f"searching {', '.join(GENES[index].name for index in active)} with seeds {', '.join(map(str, seeds))}; "
        f"teleport weight {args.teleport_weight:g}, {args.collision_weight:g} fitness points per extra collision",
        flush=True,
    )
    if args.collision_weight > 0:
        search.evaluate_all([default_individual()])
        baseline = search.set_collision_baseline(default_individual())
        print(f"SUMO default driver: {baseline:g} collisions; only more than that cost fitness", flush=True)
    history = evolve(search, random.Random(args.seed), args.population, args.generations, active=active)
    ranked = sorted(search.results.values(), key=lambda r: (r["fitness"], r["label"]))
    best = ranked[0]
    default = search.results.get(default_individual())
    best_individual = tuple(best["genes"][gene.name] for gene in GENES)

    if len(seeds) > 1:
        check_seed = seeds[1]
        check = search.runs[(best_individual, check_seed)]
        second_log = args.work_dir / f"{best['label']}-seed{check_seed}" / "sim.log"
    else:
        check_seed = CHECK_SEED
        print(f"best {best['label']}: checking it again with seed {check_seed}", flush=True)
        check = search.evaluate(best_individual, seed=check_seed, label="best-seed-check")
        second_log = args.work_dir / "best-seed-check" / "sim.log"
    spots = hotspots(net, args.work_dir / best["label"] / "sim.log", second_log)
    with (args.work_dir / "hotspots.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = list(spots[0].keys()) if spots else ["approach"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(spots)
    config = write_gui_config(args.work_dir, args.net, search.routes_text, best_individual, args)

    summary = {
        "teleport_after_s": args.teleport,
        "tolerance_percent": args.tolerance,
        "genes_searched": [GENES[index].name for index in active],
        "seeds": list(seeds),
        "teleport_weight": args.teleport_weight,
        "collision_weight": args.collision_weight,
        "collision_baseline": search.collision_baseline,
        "unroutable_trips": unroutable,
        "signals": [str(path) for path in args.additional],
        "sumo_default": default,
        "best": best,
        "best_seed_check": check,
        "generations": history,
        "evaluated": len(search.results),
        "hotspots": spots,
        "gui_config": str(config),
    }
    (args.work_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    def line(name: str, result: dict | None) -> None:
        if not result or "travel_time" not in result:
            print(f"{name}: no result")
            return
        tt = result["travel_time"]
        print(
            f"{name}: {describe(result)}; network errors {result['network_errors']}, capacity {result['capacity']}; "
            f"travel time SUMO {tt.get('mean_sumo_s')} s vs Google {tt.get('mean_google_s')} s "
            f"(MRE {tt['mre_percent']} %, RMSE {tt['rmse_s']} s, {tt['trips']} trips)"
        )

    print()
    averaged = f" (mean of seeds {', '.join(map(str, seeds))})" if len(seeds) > 1 else ""
    line(f"SUMO default behaviour{averaged}", default)
    line(f"calibrated behaviour{averaged}", best)
    line(f"calibrated, seed {check_seed}", check)
    print("calibrated parameters: " + ", ".join(f"{k} {v:g}" for k, v in best["genes"].items()))
    at_signals = sum(1 for row in spots if row["traffic_light"])
    print(f"hotspots in both runs (at least {HOTSPOT_MIN} stuck vehicles each): {len(spots)}, {at_signals} at traffic lights")
    for row in spots[:25]:
        print(
            f"  {row['vehicles_seed_a']:4d} {row['vehicles_seed_b']:4d}  {row['approach']} -> {row['junction_with']}"
            f"  [{row['type']}{', signal' if row['traffic_light'] else ''}] ({row['kinds_seed_a']})"
        )
    print(f"open in the GUI: sumo-gui -c {config}")


if __name__ == "__main__":
    main()
