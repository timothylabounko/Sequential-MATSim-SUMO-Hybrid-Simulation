"""Trips that cross the network boundary, started or ended past the junction where they branch off.

A trip that enters the SUMO network from outside starts on the edge matched to
the first MATSim link inside the network (``plans_to_sumo.py``). A MATSim
motorway link can be kilometres long, and the edge matched to it may lie after
the junction where the MATSim route turned off. SUMO then has to send the
vehicle on to the next exit and back: in Zürich, 4,392 trips from the A1 of
Winterthur started just past the Aubrugg exit and reached the city through a
U-turn at the Seebach exit, whose queue stopped vehicles from entering at all.

For every trip that enters from outside (``start_free`` 0 in ``ends.csv``), the
road is followed upstream from its first edge, at a merge along the incoming
edge with the most lanes and then the highest priority, up to ``UPSTREAM_M``
or the boundary of the network. The walk stays on the same road: it never
turns back onto the opposite carriageway, never turns by more than
``MAX_TURN_DEG``, and stops when the straight distance to the old start falls
below ``MIN_STRAIGHTNESS`` times the distance driven. Every edge on the way that ends at a diverge
is a candidate start. ``duarouter`` routes the trip from each candidate; the
candidate is used if its route is at least ``SAVING_M`` shorter than driving
on to the old start and its route, so the trip would have branched off before
its old start. The largest saving wins. Trips that leave the network
(``end_free`` 0) are treated the same way downstream, with the edges after a
merge as candidates. Ends at activity locations are never moved, and the
demand keeps every trip and departure time.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import sumolib

UPSTREAM_M = 1000.0
SAVING_M = 500.0
MAX_TURN_DEG = 45.0
MIN_STRAIGHTNESS = 0.7
CLONE_RE = re.compile(r"_x\d+$")


def _drivable(edge) -> bool:
    return edge.getFunction() != "internal" and edge.allows("passenger")


def _bearing(a, b) -> float:
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360


def _turn(into, out) -> float:
    """Angle between the end of edge ``into`` and the start of edge ``out``, in degrees."""
    first, second = into.getShape(), out.getShape()
    change = abs(_bearing(first[-2], first[-1]) - _bearing(second[0], second[1])) % 360
    return min(change, 360 - change)


def _same_road(into, out) -> bool:
    return into.getFromNode() != out.getToNode() and _turn(into, out) <= MAX_TURN_DEG


def _main(edges):
    return max(edges, key=lambda edge: (edge.getLaneNumber(), edge.getPriority(), edge.getID()))


def _in_line(start, end, driven: float) -> bool:
    return math.dist(start, end) >= MIN_STRAIGHTNESS * driven


def upstream_candidates(edge, limit: float = UPSTREAM_M) -> list[tuple[str, float]]:
    """Edges of the same road upstream of ``edge`` that end at a diverge, with the distance driven from them to ``edge``."""
    found, seen, driven, current = [], {edge.getID()}, 0.0, edge
    origin = edge.getShape()[0]
    while driven < limit:
        incoming = [e for e in current.getIncoming() if _drivable(e) and _same_road(e, current)]
        if not incoming:
            break
        previous = _main(incoming)
        if previous.getID() in seen:
            break
        seen.add(previous.getID())
        driven += previous.getLength()
        if not _in_line(previous.getShape()[0], origin, driven):
            break
        if sum(1 for e in previous.getOutgoing() if _drivable(e)) > 1:
            found.append((previous.getID(), driven))
        current = previous
    return found


def downstream_candidates(edge, limit: float = UPSTREAM_M) -> list[tuple[str, float]]:
    """Edges of the same road downstream of ``edge`` that start at a merge, with the distance driven to them from ``edge``."""
    found, seen, driven, current = [], {edge.getID()}, 0.0, edge
    end = edge.getShape()[-1]
    while driven < limit:
        outgoing = [e for e in current.getOutgoing() if _drivable(e) and _same_road(current, e)]
        if not outgoing:
            break
        following = _main(outgoing)
        if following.getID() in seen:
            break
        seen.add(following.getID())
        driven += following.getLength()
        if not _in_line(end, following.getShape()[-1], driven):
            break
        if sum(1 for e in following.getIncoming() if _drivable(e)) > 1:
            found.append((following.getID(), driven))
        current = following
    return found


def route_lengths(net_file: Path, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], float]:
    """Length of duarouter's route for every (from, to) pair; pairs without a route are missing."""
    with tempfile.TemporaryDirectory() as tmp:
        trips = Path(tmp) / "pairs.trips.xml"
        routes = Path(tmp) / "pairs.rou.xml"
        with trips.open("w", encoding="utf-8") as out:
            out.write("<routes>\n")
            for index, (frm, to) in enumerate(pairs):
                out.write(f'    <trip id="p{index}" depart="0" from="{frm}" to="{to}"/>\n')
            out.write("</routes>\n")
        completed = subprocess.run(
            ["duarouter", "-n", str(net_file), "-r", str(trips), "-o", str(routes), "--ignore-errors", "true",
             "--route-length", "true", "--no-step-log", "true", "--no-warnings", "true"],
            text=True, capture_output=True, check=False, timeout=7200,
        )
        if completed.returncode != 0 or not routes.is_file():
            raise RuntimeError((completed.stdout + completed.stderr)[-1500:])
        lengths = {}
        for vehicle in ET.parse(routes).getroot().iter("vehicle"):
            route = vehicle.find("route")
            if route is not None and route.get("routeLength"):
                lengths[pairs[int(vehicle.get("id")[1:])]] = float(route.get("routeLength"))
        return lengths


def read_ends(path: Path) -> dict[str, tuple[bool, bool]]:
    with path.open(encoding="utf-8") as handle:
        return {row["id"]: (row["start_free"] == "1", row["end_free"] == "1") for row in csv.DictReader(handle)}


def choose(original: float | None, candidates: list[tuple[str, float]], lengths: dict, pair_of) -> tuple[str, float] | None:
    """The candidate with the largest saving of at least ``SAVING_M``, or None."""
    if original is None:
        return None
    best = None
    for edge_id, driven in candidates:
        length = lengths.get(pair_of(edge_id))
        if length is None:
            continue
        saving = original + driven - length
        if saving >= SAVING_M and (best is None or saving > best[1]):
            best = (edge_id, saving)
    return best


def repair(net_file: Path, trips_file: Path, ends_file: Path, output: Path, moves_file: Path | None = None) -> dict:
    net = sumolib.net.readNet(str(net_file))
    ends = read_ends(ends_file)
    tree = ET.parse(trips_file)
    trips = [trip for trip in tree.getroot().iter("trip") if net.hasEdge(trip.get("from")) and net.hasEdge(trip.get("to"))]

    def fixed(trip) -> tuple[bool, bool]:
        start_free, end_free = ends.get(CLONE_RE.sub("", trip.get("id")), (True, True))
        return not start_free, not end_free

    starts: dict[str, list] = {}
    stops: dict[str, list] = {}
    pairs = set()
    for trip in trips:
        frm, to = trip.get("from"), trip.get("to")
        fixed_start, fixed_end = fixed(trip)
        if fixed_start and frm not in starts:
            starts[frm] = upstream_candidates(net.getEdge(frm))
        if fixed_end and to not in stops:
            stops[to] = downstream_candidates(net.getEdge(to))
        if (fixed_start and starts[frm]) or (fixed_end and stops[to]):
            pairs.add((frm, to))
            if fixed_start:
                pairs.update((edge_id, to) for edge_id, _d in starts[frm])
            if fixed_end:
                pairs.update((frm, edge_id) for edge_id, _d in stops[to])
    lengths = route_lengths(net_file, sorted(pairs)) if pairs else {}

    moves, counts = [], {"trips": len(trips), "starts_moved": 0, "ends_moved": 0}
    for trip in trips:
        frm, to = trip.get("from"), trip.get("to")
        fixed_start, fixed_end = fixed(trip)
        original = lengths.get((frm, to))
        new_from = choose(original, starts.get(frm, []), lengths, lambda e: (e, to)) if fixed_start else None
        new_to = choose(original, stops.get(to, []), lengths, lambda e: (frm, e)) if fixed_end else None
        if new_from:
            trip.set("from", new_from[0])
            counts["starts_moved"] += 1
            moves.append({"id": trip.get("id"), "end": "start", "old": frm, "new": new_from[0], "saving_m": round(new_from[1])})
        if new_to:
            trip.set("to", new_to[0])
            counts["ends_moved"] += 1
            moves.append({"id": trip.get("id"), "end": "end", "old": to, "new": new_to[0], "saving_m": round(new_to[1])})
    tree.write(output, encoding="utf-8", xml_declaration=True)
    if moves_file:
        with moves_file.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["id", "end", "old", "new", "saving_m"])
            writer.writeheader()
            writer.writerows(moves)
    counts["saving_km"] = round(sum(move["saving_m"] for move in moves) / 1000, 1)
    return counts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--net", type=Path, required=True)
    parser.add_argument("--trips", type=Path, required=True)
    parser.add_argument("--ends", type=Path, required=True, help="ends.csv of plans_to_sumo.py")
    parser.add_argument("--output", type=Path, required=True, help="May be the same file as --trips")
    parser.add_argument("--moves", type=Path, help="CSV of every moved start and end")
    args = parser.parse_args(argv)
    counts = repair(args.net, args.trips, args.ends, args.output, args.moves)
    print(
        f"{counts['starts_moved']} starts and {counts['ends_moved']} ends of {counts['trips']} trips moved to before the "
        f"junction where they branch off ({counts['saving_km']} km of detours saved) -> {args.output}"
    )


if __name__ == "__main__":
    main()
