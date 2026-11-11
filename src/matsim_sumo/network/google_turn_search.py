"""Repair turns on the corrected Zürich network using Google Routes and a SUMO search.

The corrected network is only read. Google decides whether a turn at a stuck
approach exists. Lane-change bans imported from OpenStreetMap ``change:lanes``
near a wrong-lane stop are added as further edits. The evolutionary search
then keeps the subset of those edits that reduces the recorded gridlocks. The
API key is read from
``GOOGLE_MAPS_API_KEY`` and is never written into the repository.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import sumolib

from matsim_sumo.network.osm_connectivity import (
    connection_component_count,
    connectivity_problems,
    fetch_highway_ways,
    junction_node_ids,
    nowhere_links,
    passenger_component_count,
    unresolved_edge_ids,
)
from matsim_sumo.network.osm_zurich import find_netconvert, find_sumo_home, strip_newer_vclasses
from matsim_sumo.network.turn_evolution import (
    Maneuver,
    TurnEdit,
    edits_from_readings,
    evolve,
    parse_gridlocks,
    reading_from_route,
    summarise,
)
from matsim_sumo.network.zurich_correction import direction_band, signed_deflection_degrees

APPROACH_OFFSET_M = 40.0
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


def point_along(shape: list[tuple[float, float]], distance: float, from_end: bool = False) -> tuple[float, float]:
    """Return the point ``distance`` metres along a polyline, or its far end."""
    points = list(reversed(shape)) if from_end else list(shape)
    if not points:
        raise ValueError("A polyline needs at least one point")
    if len(points) == 1 or distance <= 0:
        return points[0]
    remaining = distance
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length >= remaining and length > 0:
            scale = remaining / length
            return (x1 + dx * scale, y1 + dy * scale)
        remaining -= length
    return points[-1]


def _passenger_lane_indexes(edge) -> list[int]:
    return [lane.getIndex() for lane in edge.getLanes() if lane.allows("passenger")]


@dataclass(frozen=True)
class TurnCandidate:
    maneuver: Maneuver
    origin: tuple[float, float]
    destination: tuple[float, float]
    direct_m: float
    lane_pairs: tuple[tuple[int, int], ...]


def candidates_for_edge(net, edge_id: str) -> list[TurnCandidate]:
    """List usual turns at one stuck approach. U-turns are left unchanged."""
    edge = net.getEdge(edge_id)
    if edge.getFunction() or not edge.allows("passenger"):
        return []
    node = edge.getToNode()
    outgoing = {dest_edge.getID(): connections for dest_edge, connections in edge.getOutgoing().items()}
    found: list[TurnCandidate] = []
    for dest in node.getOutgoing():
        if dest.getFunction() or not dest.allows("passenger") or dest.getID() == edge.getID():
            continue
        delta = signed_deflection_degrees(edge.getShape(), dest.getShape())
        if delta is None or direction_band(delta) == "t":
            continue
        present = dest.getID() in outgoing
        if present:
            pairs = tuple(
                (conn.getFromLane().getIndex(), conn.getToLane().getIndex()) for conn in outgoing[dest.getID()]
            )
        else:
            from_lanes = _passenger_lane_indexes(edge)
            to_lanes = _passenger_lane_indexes(dest)
            if not from_lanes or not to_lanes:
                continue
            band = direction_band(delta)
            chosen = [min(from_lanes)] if band == "r" else [max(from_lanes)] if band == "l" else from_lanes
            pairs = tuple(
                (index, min(to_lanes, key=lambda lane: abs(lane - index))) for index in chosen
            )
        if not pairs:
            continue
        origin_xy = point_along(edge.getShape(), APPROACH_OFFSET_M, from_end=True)
        destination_xy = point_along(dest.getShape(), APPROACH_OFFSET_M, from_end=False)
        junction_xy = node.getCoord()
        direct_m = math.hypot(origin_xy[0] - junction_xy[0], origin_xy[1] - junction_xy[1])
        direct_m += math.hypot(destination_xy[0] - junction_xy[0], destination_xy[1] - junction_xy[1])
        origin = _lat_lon(net, origin_xy)
        destination = _lat_lon(net, destination_xy)
        found.append(
            TurnCandidate(
                Maneuver(node.getID(), edge.getName() or "", edge.getID(), dest.getID(), present),
                origin,
                destination,
                direct_m,
                pairs,
            )
        )
    return found


def _lat_lon(net, xy: tuple[float, float]) -> tuple[float, float]:
    lon, lat = net.convertXY2LonLat(xy[0], xy[1])
    return (lat, lon)


def google_route_metres(origin: tuple[float, float], destination: tuple[float, float], api_key: str) -> float | None:
    """Return the driving distance in metres, or None when Google has no route."""
    body = json.dumps(
        {
            "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
            "destination": {"location": {"latLng": {"latitude": destination[0], "longitude": destination[1]}}},
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_UNAWARE",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        ROUTES_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "routes.distanceMeters",
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
    if not routes:
        return None
    return float(routes[0].get("distanceMeters", 0))


def connection_patch(edits: list[tuple[TurnEdit, TurnCandidate]]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<connections>"]
    for edit, candidate in edits:
        tag = "delete" if edit.action == "delete" else "connection"
        for from_lane, to_lane in candidate.lane_pairs:
            lines.append(
                f'    <{tag} from="{edit.maneuver.from_edge}" to="{edit.maneuver.to_edge}" '
                f'fromLane="{from_lane}" toLane="{to_lane}"/>'
            )
    lines.append("</connections>")
    lines.append("")
    return "\n".join(lines)


def apply_patch(source_net: Path, patch: str, output_net: Path) -> None:
    output_net.parent.mkdir(parents=True, exist_ok=True)
    patch_path = output_net.with_suffix(".con.xml")
    patch_path.write_text(patch, encoding="utf-8")
    staged = output_net.with_suffix(".staged.xml")
    command = [
        find_netconvert(find_sumo_home()),
        "--sumo-net-file",
        str(source_net),
        "--connection-files",
        str(patch_path),
        "--output-file",
        str(staged),
        "--output.street-names",
        "--output.original-names",
    ]
    completed = subprocess.run(command, text=True, capture_output=True, timeout=600)
    if completed.returncode != 0 or not staged.is_file():
        raise RuntimeError(completed.stderr[-1500:])
    output_net.write_text(strip_newer_vclasses(staged.read_text(encoding="utf-8")), encoding="utf-8")


def gridlock_count(
    net_path: Path,
    routes: Path,
    log_path: Path,
    end_s: int = 1800,
    teleport_s: int = 120,
    sumo_options: tuple[str, ...] = (),
    statistics: bool = False,
) -> int:
    """Count stuck vehicles after repairing routes on the edited network.

    Deleting a turn invalidates routes that used it. Those trips are repaired
    with ``duarouter``. A trip that still has no path is counted with the
    gridlocks, so removing a used movement cannot improve the score by
    dropping cars. A vehicle counts as stuck once it has not moved for
    ``teleport_s`` seconds. With ``statistics`` SUMO also writes
    ``<log>.stats.xml``, which holds the number of inserted vehicles.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    repaired = log_path.with_suffix(".rou.xml")
    routing = subprocess.run(
        [
            "duarouter",
            "-n",
            str(net_path),
            "-r",
            str(routes),
            "--repair",
            "true",
            "--ignore-errors",
            "true",
            "--no-step-log",
            "true",
            "-o",
            str(repaired),
        ],
        text=True,
        capture_output=True,
        timeout=3600,
    )
    routing_log = routing.stdout + routing.stderr
    if routing.returncode != 0 or not repaired.is_file():
        raise RuntimeError(routing_log[-1500:])
    unroutable = routing_log.count("has no valid route")
    log_path.with_suffix(".routing.txt").write_text(routing_log, encoding="utf-8")
    command = [
        "sumo",
        "-n",
        str(net_path),
        "-r",
        str(repaired),
        "-b",
        "0",
        "-e",
        str(end_s),
        "--time-to-teleport",
        str(teleport_s),
        "--time-to-teleport.highways",
        str(teleport_s),
        "--ignore-route-errors",
        "true",
        "--no-step-log",
        "true",
        "--log",
        str(log_path),
        "--duration-log.disable",
        "true",
        *sumo_options,
    ]
    if statistics:
        command += ["--statistic-output", str(log_path.with_suffix(".stats.xml"))]
    completed = subprocess.run(command, text=True, capture_output=True, timeout=3600)
    if completed.returncode != 0:
        raise RuntimeError((completed.stdout + completed.stderr)[-1500:])
    return len(parse_gridlocks(log_path.read_text(encoding="utf-8", errors="replace"))) + unroutable


def inserted_vehicles(log_path: Path) -> int | None:
    """Vehicles SUMO inserted in the run that wrote ``log_path``, from its statistics output."""
    stats = log_path.with_suffix(".stats.xml")
    if not stats.is_file():
        return None
    match = re.search(r'<vehicles\b[^>]*\binserted="(\d+)"', stats.read_text(encoding="utf-8", errors="replace"))
    return int(match.group(1)) if match else None


def stuck_edge_ids(log_text: str, kind: str | None = None) -> list[str]:
    """Edges with stuck vehicles, the most stuck vehicles first."""
    counts: dict[str, int] = {}
    for item in parse_gridlocks(log_text):
        if item.edge.startswith(":") or (kind is not None and item.kind != kind):
            continue
        counts[item.edge] = counts.get(item.edge, 0) + 1
    return sorted(counts, key=lambda edge: -counts[edge])


LANE_TAG = re.compile(r'<lane id="([^"]+)_\d+"[^>]*>')
LANE_CHANGE_ATTR = re.compile(r' change(?:Left|Right)="[^"]*"')
BAN_LOOKAHEAD_M = 500.0


def banned_edges(net_text: str) -> set[str]:
    """Edges with at least one lane that forbids changing lanes."""
    return {match.group(1) for match in LANE_TAG.finditer(net_text) if LANE_CHANGE_ATTR.search(match.group(0))}


def lane_ban_groups(net, banned: set[str], wrong_lane_edges: list[str]) -> list[tuple[str, ...]]:
    """Stretches of lane-change bans that a wrong-lane vehicle was waiting in or driving towards.

    From each edge where a vehicle waited for the wrong lane, the edges up to
    ``BAN_LOOKAHEAD_M`` downstream are searched. Every banned edge found there
    is joined with the banned edges that touch it, so one gene lifts the
    whole stretch of solid lines.
    """
    groups: list[tuple[str, ...]] = []
    claimed: set[str] = set()
    for edge_id in wrong_lane_edges:
        if not net.hasEdge(edge_id):
            continue
        seeds: list[str] = []
        frontier = [(net.getEdge(edge_id), 0.0)]
        visited: set[str] = set()
        while frontier:
            edge, travelled = frontier.pop()
            if edge.getID() in visited:
                continue
            visited.add(edge.getID())
            if edge.getID() in banned:
                seeds.append(edge.getID())
            reach = travelled + edge.getLength()
            if reach < BAN_LOOKAHEAD_M:
                frontier.extend((other, reach) for other in edge.getOutgoing())
        group: set[str] = set()
        stack = [seed for seed in seeds if seed not in claimed]
        while stack:
            current = stack.pop()
            if current in group:
                continue
            group.add(current)
            edge = net.getEdge(current)
            neighbours = list(edge.getOutgoing()) + list(edge.getIncoming())
            stack.extend(other.getID() for other in neighbours if other.getID() in banned and other.getID() not in group)
        if group:
            claimed |= group
            groups.append(tuple(sorted(group)))
    return groups


def lift_lane_bans(net_text: str, edges: set[str]) -> str:
    """Allow lane changes on every lane of ``edges``. Nothing else in the file changes."""

    def lift(match: re.Match) -> str:
        if match.group(1) not in edges:
            return match.group(0)
        return LANE_CHANGE_ATTR.sub("", match.group(0))

    return LANE_TAG.sub(lift, net_text)


WRONG_LANE_RE = re.compile(r"Teleporting vehicle '([^']+)'; waited too long \(wrong lane\), lane='([^']+)'")
VEHICLE_ROUTE_RE = re.compile(r'<vehicle id="([^"]+)"[^>]*>\s*(?:<routeDistribution[^>]*>\s*)?<route[^>]*edges="([^"]+)"', re.S)
MOVEMENT_LOOKAHEAD_M = 1000.0
MAX_LANE_GENES = 12
# A jammed full-population run can stop vehicles on thousands of approaches;
# only the worst are sent to Google so a round stays within ~120 requests.
MAX_STUCK_APPROACHES = 40
EDIT_PENALTY = 0.001


def vehicle_routes(route_file: Path, wanted: set[str]) -> dict[str, list[str]]:
    """Edge lists of ``wanted`` vehicles in a ``duarouter`` output file."""
    found: dict[str, list[str]] = {}
    for vehicle, edges in VEHICLE_ROUTE_RE.findall(route_file.read_text(encoding="utf-8", errors="replace")):
        if vehicle in wanted and vehicle not in found:
            found[vehicle] = edges.split()
    return found


def blocked_movement(net, route: list[str], lane_id: str, banned: set[str]) -> tuple[str, int, str] | None:
    """Where the lane a vehicle waited in stops leading along its route.

    The lane is followed through its connections. On an edge without a
    lane-change ban the vehicle may use any passenger lane; on a banned edge it
    keeps the lanes it arrived on. The result is the edge, the lane on it
    nearest to the vehicle's lanes, and the next route edge that none of
    those lanes reaches. ``None`` means the lane does lead along the route
    within ``MOVEMENT_LOOKAHEAD_M``.
    """
    edge_id, _, index = lane_id.rpartition("_")
    if edge_id not in route or not net.hasEdge(edge_id):
        return None
    position = route.index(edge_id)
    lanes = {int(index)}
    travelled = 0.0
    for current_id, next_id in zip(route[position:], route[position + 1 :]):
        current = net.getEdge(current_id)
        if current_id not in banned and travelled > 0:
            lanes = {lane.getIndex() for lane in current.getLanes() if lane.allows("passenger")}
        reached: set[int] = set()
        for lane in current.getLanes():
            if lane.getIndex() not in lanes:
                continue
            for connection in lane.getOutgoing():
                if connection.getTo().getID() == next_id:
                    reached.add(connection.getToLane().getIndex())
        if not reached:
            serving = [
                connection.getFromLane().getIndex()
                for lane in current.getLanes()
                for connection in lane.getOutgoing()
                if connection.getTo().getID() == next_id
            ]
            if not serving:
                return None
            from_lane = min(lanes, key=lambda own: min(abs(own - other) for other in serving))
            return current_id, from_lane, next_id
        lanes = reached
        travelled += current.getLength()
        if travelled > MOVEMENT_LOOKAHEAD_M:
            return None
    return None


def lane_movement_candidates(net, log_text: str, route_file: Path | None, banned: set[str]) -> list[tuple[TurnCandidate, int]]:
    """One candidate lane connection per blocked movement, with the number of vehicles it stopped.

    The target lane is the one the nearest serving lane already uses, so the
    new connection joins the existing movement instead of creating a new one.
    """
    events = WRONG_LANE_RE.findall(log_text)
    if not events or route_file is None or not route_file.is_file():
        return []
    routes = vehicle_routes(route_file, {vehicle for vehicle, _lane in events})
    counts: dict[tuple[str, int, str], int] = {}
    for vehicle, lane_id in events:
        if vehicle not in routes or lane_id.startswith(":"):
            continue
        movement = blocked_movement(net, routes[vehicle], lane_id, banned)
        if movement is not None:
            counts[movement] = counts.get(movement, 0) + 1
    found: list[tuple[TurnCandidate, int]] = []
    for (from_id, from_lane, to_id), vehicles in sorted(counts.items(), key=lambda item: -item[1]):
        turn = next((item for item in candidates_for_edge(net, from_id) if item.maneuver.to_edge == to_id), None)
        if turn is None:
            continue
        serving = [
            (connection.getFromLane().getIndex(), connection.getToLane().getIndex())
            for lane in net.getEdge(from_id).getLanes()
            for connection in lane.getOutgoing()
            if connection.getTo().getID() == to_id
        ]
        to_lane = min(serving, key=lambda pair: abs(pair[0] - from_lane))[1]
        maneuver = Maneuver(turn.maneuver.junction, turn.maneuver.street, from_id, to_id, True)
        found.append((TurnCandidate(maneuver, turn.origin, turn.destination, turn.direct_m, ((from_lane, to_lane),)), vehicles))
    return found


def query_candidates(candidates: list[TurnCandidate], api_key: str, cache_path: Path, route_fn=None) -> list:
    route_fn = route_fn or google_route_metres
    cache = {}
    if cache_path.is_file():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    readings = []
    for index, candidate in enumerate(candidates, start=1):
        key = f"{candidate.origin[0]:.5f},{candidate.origin[1]:.5f}:{candidate.destination[0]:.5f},{candidate.destination[1]:.5f}"
        if key not in cache or cache[key].get("error"):
            try:
                cache[key] = {"metres": route_fn(candidate.origin, candidate.destination, api_key), "error": ""}
            except RuntimeError as exc:
                cache[key] = {"metres": None, "error": str(exc)}
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            time.sleep(0.12)
        stored = cache[key]
        route_m = stored["metres"]
        readings.append(reading_from_route(route_m, candidate.direct_m, stored.get("error", "")))
        print(
            f"google {index}/{len(candidates)} {candidate.maneuver.street} "
            f"{candidate.maneuver.from_edge}->{candidate.maneuver.to_edge} "
            f"present={candidate.maneuver.present} route={route_m} direct={candidate.direct_m:.0f}",
            flush=True,
        )
    return readings


def search_network(
    source_net: Path,
    routes: Path,
    log_text: str,
    work_dir: Path,
    api_key: str,
    rng: random.Random,
    end_s: int = 1800,
    route_fn=None,
    baseline_score: float | None = None,
    route_file: Path | None = None,
    teleport_s: int = 120,
) -> dict:
    """Query Google at stuck approaches and search the supported edits.

    The genes are the turn edits Google supports, followed by the stretches
    of lane-change bans near wrong-lane stops. ``baseline_score`` is the
    score of the unchanged network, including unroutable trips. Every
    individual is compared with OpenStreetMap after it is simulated. It is
    rejected when it has a link to nowhere that the source network did not
    have, or when it splits the drivable network into more pieces.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    net = sumolib.net.readNet(str(source_net))
    candidates = []
    for edge_id in stuck_edge_ids(log_text)[:MAX_STUCK_APPROACHES]:
        if net.hasEdge(edge_id):
            candidates.extend(candidates_for_edge(net, edge_id))
    readings = query_candidates(candidates, api_key, work_dir / "google-cache.json", route_fn)
    maneuvers = [candidate.maneuver for candidate in candidates]
    supported = edits_from_readings(maneuvers, readings)
    by_key = {(candidate.maneuver.from_edge, candidate.maneuver.to_edge): candidate for candidate in candidates}
    paired = [(edit, by_key[(edit.maneuver.from_edge, edit.maneuver.to_edge)]) for edit in supported]
    wrong_lane_edges = stuck_edge_ids(log_text, "wrong lane")[:MAX_STUCK_APPROACHES]
    source_text = source_net.read_text(encoding="utf-8")
    banned = banned_edges(source_text)
    ban_groups = lane_ban_groups(net, banned, wrong_lane_edges)
    for group in ban_groups:
        print(f"lane-change ban gene: {' '.join(group)}", flush=True)
    moves = lane_movement_candidates(net, log_text, route_file, banned)[:MAX_LANE_GENES]
    move_readings = query_candidates([candidate for candidate, _count in moves], api_key, work_dir / "google-cache.json", route_fn)
    lane_moves: list[dict] = []
    for (candidate, vehicles), reading in zip(moves, move_readings):
        (from_lane, to_lane), = candidate.lane_pairs
        record = {
            "street": candidate.maneuver.street,
            "from": candidate.maneuver.from_edge,
            "from_lane": from_lane,
            "to": candidate.maneuver.to_edge,
            "to_lane": to_lane,
            "vehicles": vehicles,
            "google_m": reading.route_m,
            "direct_m": round(candidate.direct_m),
            "google_drives_it": reading.possible,
        }
        lane_moves.append(record)
        print(
            f"lane movement {candidate.maneuver.from_edge}_{from_lane} -> {candidate.maneuver.to_edge}: "
            f"{vehicles} vehicles, google {reading.route_m} m, {'gene' if reading.possible else 'not a gene'}",
            flush=True,
        )
        if reading.possible:
            paired.append((TurnEdit("add lane", candidate.maneuver), candidate))
    gene_count = len(paired) + len(ban_groups)
    baseline = float(len(parse_gridlocks(log_text)) if baseline_score is None else baseline_score)
    cache: dict[tuple[bool, ...], float] = {tuple(False for _ in range(gene_count)): baseline}

    def split(chromosome: tuple[bool, ...]) -> tuple[list, list[tuple[str, ...]]]:
        turns = [paired[index] for index in range(len(paired)) if chromosome[index]]
        bans = [ban_groups[index] for index in range(len(ban_groups)) if chromosome[len(paired) + index]]
        return turns, bans

    def build(chromosome: tuple[bool, ...], net_path: Path) -> None:
        turns, bans = split(chromosome)
        if turns:
            apply_patch(source_net, connection_patch(turns), net_path)
        else:
            shutil.copyfile(source_net, net_path)
        if bans:
            text = source_text if not turns else net_path.read_text(encoding="utf-8")
            net_path.write_text(lift_lane_bans(text, {edge for group in bans for edge in group}), encoding="utf-8")

    def fitness(chromosome: tuple[bool, ...]) -> float:
        if chromosome in cache:
            return cache[chromosome]
        label = "".join("1" if bit else "0" for bit in chromosome)
        net_path = work_dir / f"{label}.net.xml"
        try:
            if any(chromosome):
                build(chromosome, net_path)
            else:
                net_path = source_net
            count = gridlock_count(net_path, routes, work_dir / f"{label}.log", end_s, teleport_s)
        except (RuntimeError, OSError) as exc:
            print(f"fitness {label} failed: {exc}", flush=True)
            count = 10**9
        score = count + EDIT_PENALTY * sum(chromosome)
        cache[chromosome] = score
        print(f"fitness {label} = {count}", flush=True)
        return score

    source = sumolib.net.readNet(str(source_net))
    baseline_components = passenger_component_count(source)
    osm_cache: dict[str, list[int]] = {}
    osm_cache_path = work_dir / "osm-nodes.json"
    if osm_cache_path.is_file():
        osm_cache = json.loads(osm_cache_path.read_text(encoding="utf-8"))

    def unresolved_of(candidate) -> list[str]:
        node_ids: list[int] = []
        for edge, _exits in nowhere_links(candidate):
            node_ids.extend(junction_node_ids(edge.getToNode().getID()))

        def save_cache() -> None:
            osm_cache_path.write_text(json.dumps(osm_cache), encoding="utf-8")

        return unresolved_edge_ids(candidate, fetch_highway_ways(node_ids, osm_cache, on_batch=save_cache))

    baseline_unresolved = set(unresolved_of(source))
    baseline_drive = connection_component_count(source)

    def osm_accepts(chromosome: tuple[bool, ...]) -> bool:
        """Reject this individual when its network still leads nowhere or is disconnected.

        Fitness has already scored the individual. The review reads the network
        that score was measured on. A chromosome that selects edits is rejected
        when that network was not written.
        """
        label = "".join("1" if bit else "0" for bit in chromosome)
        patched = work_dir / f"{label}.net.xml"
        if any(chromosome):
            if not patched.is_file():
                print(f"osm review {label}: candidate network was not written", flush=True)
                return False
            net_path = patched
        else:
            net_path = source_net
        candidate = sumolib.net.readNet(str(net_path))
        new_nowhere = [edge for edge in unresolved_of(candidate) if edge not in baseline_unresolved]
        drive = connection_component_count(candidate)
        problems = connectivity_problems(
            unresolved=new_nowhere,
            components=passenger_component_count(candidate),
            baseline_components=baseline_components,
        )
        if drive > baseline_drive:
            problems.append(f"the drivable network split from {baseline_drive} pieces into {drive}")
        print(f"osm review {label or 'baseline'}: {problems or 'accepted'}", flush=True)
        return not problems

    if not gene_count:
        result = {
            "best": tuple(),
            "best_fitness": baseline,
            "generations": [{"best": baseline, "accepted": osm_accepts(tuple())}],
            "stopped": "no Google disagreements and no lane-change ban near a wrong-lane stop",
        }
    else:
        result = evolve(gene_count, fitness, rng, review_fn=osm_accepts)
    selected, lifted = split(tuple(result["best"])) if gene_count else ([], [])
    best_net = work_dir / "best.net.xml"
    if selected or lifted:
        build(tuple(result["best"]), best_net)
    report = {
        "source_network": str(source_net),
        "gridlocks_before": summarise(parse_gridlocks(log_text)),
        "candidates": len(candidates),
        "google_disagreements": [
            {"action": edit.action, "street": edit.maneuver.street, "from": edit.maneuver.from_edge, "to": edit.maneuver.to_edge}
            for edit, _candidate in paired
        ],
        "applied": [
            {
                "action": edit.action,
                "street": edit.maneuver.street,
                "from": edit.maneuver.from_edge if edit.action != "add lane" else f"{edit.maneuver.from_edge}_{candidate.lane_pairs[0][0]}",
                "to": edit.maneuver.to_edge if edit.action != "add lane" else f"{edit.maneuver.to_edge}_{candidate.lane_pairs[0][1]}",
            }
            for edit, candidate in selected
        ],
        "lane_movements": lane_moves,
        "lane_ban_candidates": [list(group) for group in ban_groups],
        "lane_bans_lifted": [list(group) for group in lifted],
        "best_fitness": result["best_fitness"],
        "generations": result["generations"],
        "stopped": result["stopped"],
        "best_network": str(best_net if selected or lifted else source_net),
    }
    (work_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Search Google-supported turn edits on the corrected Zürich network.")
    parser.add_argument("--net", type=Path, required=True)
    parser.add_argument("--routes", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GOOGLE_MAPS_API_KEY is not set")
    report = search_network(
        args.net,
        args.routes,
        args.log.read_text(encoding="utf-8", errors="replace"),
        args.work_dir,
        api_key,
        random.Random(42),
    )
    keys = ("stopped", "best_fitness", "applied", "lane_bans_lifted", "google_disagreements")
    print(json.dumps({key: report[key] for key in keys}, indent=2))


if __name__ == "__main__":
    main()
