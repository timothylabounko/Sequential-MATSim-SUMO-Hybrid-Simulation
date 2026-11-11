"""Correct unambiguous intersection errors on the City of Zürich SUMO network.

Every run reads the unedited source network and writes a new corrected network
beside a report. The source file is never modified, so the same command can be
repeated after the rules change and the new report is comparable with the last one.

Auto-edits are limited to cases with one reading of the local evidence:

- a U-turn is removed when that approach also has another exit
- an empty lane is connected to the single straight downstream edge already used
  by its sibling lanes
- an OSM prohibition is removed when one approach, one via node, and the turn
  direction all agree
- extra turns are removed for an OSM ``only_*`` restriction when the allowed
  turn is present on that same approach

Missing turns are reported and are not added from geometry. Cars are removed from
reserved tram track and kept on street-running track tagged ``embedded_rails``.
A small OpenStreetMap-based driving sample can be checked with OSRM. Google and
TomTom are used only when the matching API key is set, and this command does not
call them.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import quoteattr

from matsim_sumo.network.osm_zurich import (
    DEFAULT_OUTPUT,
    find_netconvert,
    find_sumo_home,
    strip_newer_vclasses,
)
from matsim_sumo.network.tram_access import (
    LaneDecision,
    apply_lane_permissions,
    decide_lane,
    exclusive_tram_crossing,
    final_lane_flags,
    orig_way_ids,
    parse_way_kinds,
    point_to_polyline_meters,
)

DEFAULT_OUTPUT_DIR = Path("results/zurich-correction")
CORRECTED_NAME = "zurich.corrected.net.xml"
MAX_ROUTE_MANEUVERS = 20
# A straight SUMO link whose headings differ by more than this is reported, not edited.
SHARP_STRAIGHT_DEGREES = 60.0
CAR_EXCEPTION_CLASSES = frozenset({"motorcar", "motor_vehicle", "vehicle", "passenger", "car"})
PROHIBITION_DIRS = {
    "no_left_turn": frozenset({"l", "L"}),
    "no_right_turn": frozenset({"r", "R"}),
    "no_straight_on": frozenset({"s"}),
    "no_u_turn": frozenset({"t"}),
}
ONLY_DIRS = {
    "only_left_turn": frozenset({"l", "L"}),
    "only_right_turn": frozenset({"r", "R"}),
    "only_straight_on": frozenset({"s"}),
    "only_u_turn": frozenset({"t"}),
}
RULES = (
    "Remove a U-turn when the same approach has another exit.",
    "Connect an empty lane to the single straight downstream edge used by its sibling lanes.",
    "Remove a turn that matches one OSM prohibition on from-way, via node, to-way, and direction.",
    "Remove extra turns when an OSM only_* restriction matches one approach and the allowed turn exists.",
    "Do not add a turn from geometry alone.",
    "Do not edit an OSM restriction when the approach or the direction is ambiguous.",
    "Remove cars from a reserved tram track, and keep cars on a road tagged embedded_rails=tram.",
    "Remove a connection between a car-only lane and a tram-only lane.",
    "Do not edit a tram lane when the OpenStreetMap access tag is ambiguous.",
)


@dataclass(frozen=True)
class Restriction:
    relation_id: str
    kind: str | None
    from_way: str | None
    to_way: str | None
    via_node: str | None
    via_ways: tuple[str, ...]
    except_classes: tuple[str, ...]


@dataclass(frozen=True)
class Edit:
    action: str
    from_edge: str
    to_edge: str
    from_lane: int
    to_lane: int
    reason: str
    junction: str
    street: str
    evidence: str

    @property
    def connection_key(self) -> tuple[str, str, int, int]:
        return (self.from_edge, self.to_edge, self.from_lane, self.to_lane)


@dataclass(frozen=True)
class Suspect:
    kind: str
    junction: str
    street: str
    from_edge: str
    to_edge: str
    detail: str


def assert_distinct_output(source: Path, output: Path) -> None:
    """Refuse to replace the network this command is supposed to keep re-reading."""
    if source.resolve() == output.resolve():
        raise SystemExit(
            f"Refusing to overwrite the source network {source}. "
            "Choose a different --output-dir. The unedited network stays in place "
            "so the next run starts from the same file."
        )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_osm_path(source: Path) -> Path:
    name = source.name
    if name.endswith(".net.xml"):
        osm_name = name[: -len(".net.xml")] + ".osm.xml"
    else:
        osm_name = f"{source.stem}.osm.xml"
    return source.with_name(osm_name)


def edge_base(edge_id: str) -> tuple[str, bool]:
    reversed_edge = edge_id.startswith("-")
    body = edge_id[1:] if reversed_edge else edge_id
    return body.split("#", 1)[0], reversed_edge


def is_opposite_edge(left: str, right: str) -> bool:
    left_base, left_reversed = edge_base(left)
    right_base, right_reversed = edge_base(right)
    return bool(left_base) and left_base == right_base and left_reversed != right_reversed


def node_contains_via(node_id: str, via_node: str) -> bool:
    if node_id == via_node:
        return True
    if node_id.startswith("cluster_"):
        return via_node in node_id[len("cluster_") :].split("_")
    return False


def straight_target_lane(from_lane: int, destination_lanes: int) -> int:
    """Keep the lane index, clamped to the downstream road.

    Lane 0 is the rightmost lane. A rightmost empty lane therefore stays on
    lane 0, and a leftmost empty lane uses the last downstream lane when the
    road ahead is narrower.
    """
    if destination_lanes < 1:
        raise ValueError("destination edge has no lanes")
    if from_lane < 0:
        raise ValueError("lane index must be non-negative")
    return min(from_lane, destination_lanes - 1)


def uturn_indexes_to_remove(directions: list[str]) -> list[int]:
    """Drop U-turns only when the approach has some other exit.

    A U-turn that is the only movement is the dead-end turnaround and stays.
    """
    if not directions or all(direction == "t" for direction in directions):
        return []
    return [index for index, direction in enumerate(directions) if direction == "t"]


def prohibited_indexes(directions: list[str], banned: frozenset[str]) -> list[int] | None:
    """Return every index when each matched turn has the prohibited direction.

    ``None`` means the match is ambiguous and must not be edited. More than
    four matched turns is treated as a bad match rather than a real ban.
    """
    if not directions or len(directions) > 4:
        return None
    if all(direction in banned for direction in directions):
        return list(range(len(directions)))
    return None


def only_extra_indexes(
    outgoing: list[tuple[str, str]],
    allowed_to: set[str],
    allowed_dirs: frozenset[str],
) -> list[int] | None:
    """Indexes of turns to remove for an only_* restriction.

    ``None`` means the allowed turn is missing, so other turns must stay.
    An empty list means the approach already complies.
    """
    allowed_present = any(to_edge in allowed_to and direction in allowed_dirs for to_edge, direction in outgoing)
    if not allowed_present:
        return None
    return [index for index, (to_edge, _direction) in enumerate(outgoing) if to_edge not in allowed_to]


def excepts_cars(except_value: str) -> bool:
    parts = {part.strip() for part in except_value.replace("|", ";").split(";") if part.strip()}
    return bool(parts & CAR_EXCEPTION_CLASSES)


def signed_deflection_degrees(from_shape: list[tuple[float, float]], to_shape: list[tuple[float, float]]) -> float | None:
    """Signed heading change in degrees. Positive is a left turn."""
    if len(from_shape) < 2 or len(to_shape) < 2:
        return None
    x1, y1 = from_shape[-2]
    x2, y2 = from_shape[-1]
    x3, y3 = to_shape[0]
    x4, y4 = to_shape[1]
    inbound = math.atan2(y2 - y1, x2 - x1)
    outbound = math.atan2(y4 - y3, x4 - x3)
    delta = (outbound - inbound + math.pi) % (2 * math.pi) - math.pi
    return math.degrees(delta)


def direction_band(signed_degrees: float) -> str:
    magnitude = abs(signed_degrees)
    if magnitude <= 35.0:
        return "s"
    if magnitude >= 150.0:
        return "t"
    return "r" if signed_degrees < 0 else "l"


def coarse_direction(sumo_direction: str) -> str:
    return {"R": "r", "L": "l"}.get(sumo_direction, sumo_direction)


def single_missing_usual_turn(
    exits: list[tuple[str, float]],
    connected_directions: set[str],
) -> str | None:
    """Return the one missing left, straight, or right exit, otherwise nothing.

    U-turns are ignored. Zero or several missing usual turns are left untouched.
    """
    missing: list[str] = []
    for edge_id, signed_degrees in exits:
        band = direction_band(signed_degrees)
        if band in {"r", "l", "s"} and band not in connected_directions:
            missing.append(edge_id)
    if len(missing) == 1:
        return missing[0]
    return None


def cover_routes(links: list[tuple[str, str]], max_length: int = MAX_ROUTE_MANEUVERS) -> list[list[tuple[str, str]]]:
    """Greedy path cover of maneuver links, each route at most ``max_length`` long."""
    if max_length < 1:
        raise ValueError("max_length must be positive")
    remaining = list(links)
    routes: list[list[tuple[str, str]]] = []
    while remaining:
        route = [remaining.pop(0)]
        while len(route) < max_length:
            tail = route[-1][1]
            follow = next((index for index, (start, _end) in enumerate(remaining) if start == tail), None)
            if follow is None:
                break
            route.append(remaining.pop(follow))
        routes.append(route)
    return routes


def cover_routes_on_graph(
    links: list[tuple[str, str]],
    successors: dict[str, tuple[str, ...]],
    max_length: int = MAX_ROUTE_MANEUVERS,
    max_hops: int = 12,
) -> list[list[tuple[str, str, str]]]:
    """Cover suspect maneuvers with routes of at most ``max_length`` suspects.

    Intermediate edges already in the network are inserted so the route can be
    driven from one suspect turn to the next. Each step is
    ``(from_edge, to_edge, role)`` and ``role`` is ``suspect`` or ``link``.
    """
    if max_length < 1 or max_hops < 0:
        raise ValueError("route limits must be non-negative, and max_length at least 1")
    by_start: dict[str, list[str]] = {}
    for start, end in links:
        by_start.setdefault(start, []).append(end)

    def take(start: str) -> str | None:
        ends = by_start.get(start)
        if not ends:
            return None
        end = ends.pop(0)
        if not ends:
            del by_start[start]
        return end

    def path_to_next_suspect(origin: str) -> list[str] | None:
        if origin in by_start:
            return [origin]
        seen = {origin}
        queue: deque[tuple[str, list[str]]] = deque([(origin, [origin])])
        while queue:
            current, path = queue.popleft()
            if len(path) - 1 >= max_hops:
                continue
            for nxt in successors.get(current, ()):
                if nxt in seen:
                    continue
                seen.add(nxt)
                extended = path + [nxt]
                if nxt in by_start:
                    return extended
                queue.append((nxt, extended))
        return None

    routes: list[list[tuple[str, str, str]]] = []
    while by_start:
        start = next(iter(by_start))
        end = take(start)
        assert end is not None
        route = [(start, end, "suspect")]
        suspects_in_route = 1
        while suspects_in_route < max_length:
            walked = path_to_next_suspect(route[-1][1])
            if not walked:
                break
            for left, right in zip(walked, walked[1:]):
                route.append((left, right, "link"))
            nxt = take(walked[-1])
            if nxt is None:
                break
            route.append((walked[-1], nxt, "suspect"))
            suspects_in_route += 1
        routes.append(route)
    return routes


def edited_connections(
    existing: set[tuple[str, str, int, int]],
    deletes: set[tuple[str, str, int, int]],
    adds: set[tuple[str, str, int, int]],
) -> set[tuple[str, str, int, int]]:
    return (existing - deletes) | adds


def parse_restrictions(osm_path: Path) -> list[Restriction]:
    restrictions: list[Restriction] = []
    for _event, elem in ET.iterparse(osm_path, events=("end",)):
        if elem.tag != "relation":
            continue
        tags = {tag.get("k"): tag.get("v") for tag in elem.findall("tag")}
        if tags.get("type") != "restriction":
            elem.clear()
            continue
        from_ways: list[str] = []
        to_ways: list[str] = []
        via_nodes: list[str] = []
        via_ways: list[str] = []
        for member in elem.findall("member"):
            role = member.get("role")
            member_type = member.get("type")
            ref = member.get("ref")
            if not ref:
                continue
            if role == "from" and member_type == "way":
                from_ways.append(ref)
            elif role == "to" and member_type == "way":
                to_ways.append(ref)
            elif role == "via" and member_type == "node":
                via_nodes.append(ref)
            elif role == "via" and member_type == "way":
                via_ways.append(ref)
        except_value = tags.get("except") or ""
        restrictions.append(
            Restriction(
                relation_id=elem.get("id") or "",
                kind=tags.get("restriction"),
                from_way=from_ways[0] if len(from_ways) == 1 else None,
                to_way=to_ways[0] if len(to_ways) == 1 else None,
                via_node=via_nodes[0] if len(via_nodes) == 1 else None,
                via_ways=tuple(via_ways),
                except_classes=tuple(part.strip() for part in except_value.replace("|", ";").split(";") if part.strip()),
            )
        )
        elem.clear()
    return restrictions


def connection_xml(edits: list[Edit], source: Path) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f"<!-- Generated from {source}. This file is an edit list, not a network. -->",
        "<connections>",
    ]
    for edit in edits:
        tag = "delete" if edit.action == "delete" else "connection"
        lines.append(
            f"    <{tag} from={quoteattr(edit.from_edge)} to={quoteattr(edit.to_edge)} "
            f'fromLane="{edit.from_lane}" toLane="{edit.to_lane}"/>'
        )
    lines.append("</connections>")
    lines.append("")
    return "\n".join(lines)


def _lane_orig_ids(edge) -> set[str]:
    found: set[str] = set()
    for lane in edge.getLanes():
        raw = lane.getParam("origId", "") or ""
        found.update(raw.split())
    return found


def _is_normal(edge) -> bool:
    return not edge.getFunction()


def _lane_keeps_cars(
    edge_id: str,
    lane,
    blocked: set[tuple[str, int]],
    opened: set[tuple[str, int]],
) -> bool:
    key = (edge_id, lane.getIndex())
    if key in blocked:
        return False
    if key in opened:
        return True
    return lane.allows("passenger")


def _passenger_lanes(edge, blocked: set[tuple[str, int]] | None = None, opened: set[tuple[str, int]] | None = None) -> list:
    blocked = blocked or set()
    opened = opened or set()
    return [lane for lane in edge.getLanes() if _lane_keeps_cars(edge.getID(), lane, blocked, opened)]


def inspect_network(net) -> dict[str, int]:
    """Count the intersection issues the rules look for."""
    uturns = 0
    uturns_with_exit = 0
    exclusive_uturns = 0
    empty_lanes = 0
    orphan_one_straight = 0
    orphan_other = 0
    fringe_empty = 0
    connections = 0
    for edge in net.getEdges():
        if not _is_normal(edge):
            continue
        outgoing = [conn for lane in edge.getLanes() for conn in lane.getOutgoing()]
        connections += len(outgoing)
        directions = [conn.getDirection() for conn in outgoing]
        uturns += sum(direction == "t" for direction in directions)
        removed = uturn_indexes_to_remove(directions)
        uturns_with_exit += len(removed)
        if directions and all(direction == "t" for direction in directions):
            exclusive_uturns += len(directions)
        empty = [lane for lane in _passenger_lanes(edge) if not lane.getOutgoing()]
        empty_lanes += len(empty)
        straight = {
            conn.getTo().getID()
            for conn in outgoing
            if conn.getDirection() == "s" and not is_opposite_edge(edge.getID(), conn.getTo().getID())
        }
        exits = _real_exit_ids(edge)
        if not empty:
            continue
        if not outgoing or not exits:
            fringe_empty += len(empty)
        elif len(straight) == 1:
            orphan_one_straight += len(empty)
        else:
            orphan_other += len(empty)
    junctions = [node for node in net.getNodes() if not node.getID().startswith(":")]
    joined = sum(1 for node in junctions if node.getID().startswith("cluster_"))
    edges = sum(1 for edge in net.getEdges() if _is_normal(edge))
    return {
        "edges": edges,
        "junctions": len(junctions),
        "joined_junctions": joined,
        "connections": connections,
        "uturn_connections": uturns,
        "uturn_connections_with_another_exit": uturns_with_exit,
        "exclusive_uturn_connections": exclusive_uturns,
        "empty_passenger_lanes": empty_lanes,
        "orphan_lanes_with_one_straight": orphan_one_straight,
        "orphan_lanes_without_one_straight": orphan_other,
        "fringe_empty_lanes": fringe_empty,
    }


def _real_exit_ids(edge) -> list[str]:
    exits: list[str] = []
    for outgoing in edge.getToNode().getOutgoing():
        if not _is_normal(outgoing):
            continue
        if is_opposite_edge(edge.getID(), outgoing.getID()):
            continue
        exits.append(outgoing.getID())
    return exits


def _connection_identity(edge_id: str, conn) -> tuple[str, str, int, int]:
    return (edge_id, conn.getTo().getID(), conn.getFromLane().getIndex(), conn.getToLane().getIndex())


def _edge_edits_and_suspects(
    edge,
    removed_keys: set[tuple[str, str, int, int]],
    blocked: set[tuple[str, int]] | None = None,
    opened: set[tuple[str, int]] | None = None,
) -> tuple[list[Edit], list[Suspect]]:
    blocked = blocked or set()
    opened = opened or set()
    edits: list[Edit] = []
    suspects: list[Suspect] = []
    outgoing = [conn for lane in edge.getLanes() for conn in lane.getOutgoing()]
    directions = [conn.getDirection() for conn in outgoing]
    street = edge.getName() or ""
    junction = edge.getToNode().getID()
    for index in uturn_indexes_to_remove(directions):
        conn = outgoing[index]
        edits.append(
            Edit(
                action="delete",
                from_edge=edge.getID(),
                to_edge=conn.getTo().getID(),
                from_lane=conn.getFromLane().getIndex(),
                to_lane=conn.getToLane().getIndex(),
                reason="remove_uturn",
                junction=junction,
                street=street,
                evidence="U-turn removed because this approach has another exit",
            )
        )
    removed_here = set(removed_keys)
    removed_here.update(edit.connection_key for edit in edits if edit.action == "delete")
    surviving = [conn for conn in outgoing if _connection_identity(edge.getID(), conn) not in removed_here]

    empty = [
        lane
        for lane in _passenger_lanes(edge, blocked, opened)
        if not any(_connection_identity(edge.getID(), conn) not in removed_here for conn in lane.getOutgoing())
    ]
    straight: dict[str, tuple[int, tuple[bool, ...]]] = {}
    for conn in surviving:
        if conn.getDirection() != "s" or is_opposite_edge(edge.getID(), conn.getTo().getID()):
            continue
        target = conn.getTo()
        allows = tuple(_lane_keeps_cars(target.getID(), lane, blocked, opened) for lane in target.getLanes())
        straight[target.getID()] = (len(allows), allows)
    exits = _real_exit_ids(edge)
    if empty and surviving and exits and len(straight) == 1:
        to_edge, (lane_count, allows) = next(iter(straight.items()))
        if lane_count < 1:
            to_edge = ""
        for lane in empty:
            if lane_count < 1:
                break
            to_lane = straight_target_lane(lane.getIndex(), lane_count)
            proposed = (edge.getID(), to_edge, lane.getIndex(), to_lane)
            if proposed in removed_here:
                suspects.append(
                    Suspect(
                        "orphan_lane_banned",
                        junction,
                        street,
                        edge.getID(),
                        to_edge,
                        f"lane {lane.getIndex()} has no remaining exit, and the straight continuation was removed",
                    )
                )
                continue
            if not allows[to_lane]:
                suspects.append(
                    Suspect(
                        "orphan_lane_disallowed",
                        junction,
                        street,
                        edge.getID(),
                        to_edge,
                        f"lane {lane.getIndex()} would join lane {to_lane}, which does not allow passenger",
                    )
                )
                continue
            edits.append(
                Edit(
                    action="add",
                    from_edge=edge.getID(),
                    to_edge=to_edge,
                    from_lane=lane.getIndex(),
                    to_lane=to_lane,
                    reason="reconnect_orphan_lane",
                    junction=junction,
                    street=street,
                    evidence=(
                        f"empty lane {lane.getIndex()} joined to {to_edge} lane {to_lane}; "
                        "sibling lanes already continue straight onto that edge"
                    ),
                )
            )
    elif empty and (not surviving or not exits):
        for lane in empty:
            suspects.append(
                Suspect(
                    "fringe_empty_lane",
                    junction,
                    street,
                    edge.getID(),
                    "",
                    f"lane {lane.getIndex()} has no exit and no sibling movement; no turn was invented",
                )
            )
    elif empty:
        targets = ", ".join(sorted(straight)) or "none"
        for lane in empty:
            suspects.append(
                Suspect(
                    "orphan_lane_ambiguous",
                    junction,
                    street,
                    edge.getID(),
                    "",
                    f"lane {lane.getIndex()} is empty and straight targets are [{targets}]",
                )
            )

    if len(exits) >= 2:
        incoming = [item for item in edge.getToNode().getIncoming() if _is_normal(item)]
        if 3 <= len(incoming) <= 4 and (surviving or edits):
            connected = {coarse_direction(conn.getDirection()) for conn in surviving}
            if any(edit.action == "add" for edit in edits):
                connected.add("s")
            missing_inputs: list[tuple[str, float]] = []
            connected_ids = {conn.getTo().getID() for conn in surviving}
            connected_ids.update(edit.to_edge for edit in edits if edit.action == "add")
            outgoing_by_id = {item.getID(): item for item in edge.getToNode().getOutgoing()}
            for exit_id in exits:
                if exit_id in connected_ids:
                    continue
                target = outgoing_by_id.get(exit_id)
                if target is None:
                    continue
                delta = signed_deflection_degrees(edge.getShape(), target.getShape())
                if delta is None:
                    continue
                missing_inputs.append((exit_id, delta))
            missing = single_missing_usual_turn(missing_inputs, connected)
            if missing is not None:
                delta = next(value for edge_id, value in missing_inputs if edge_id == missing)
                suspects.append(
                    Suspect(
                        "missing_turn",
                        junction,
                        street,
                        edge.getID(),
                        missing,
                        f"one usual turn is absent ({direction_band(delta)}, {delta:.0f} degrees); not added",
                    )
                )

    for conn in surviving:
        if conn.getDirection() != "s":
            continue
        delta = signed_deflection_degrees(edge.getShape(), conn.getTo().getShape())
        if delta is None or abs(delta) <= SHARP_STRAIGHT_DEGREES:
            continue
        suspects.append(
            Suspect(
                "sharp_straight",
                junction,
                street,
                edge.getID(),
                conn.getTo().getID(),
                f"SUMO direction is straight but the heading changes by {delta:.0f} degrees",
            )
        )
    return edits, suspects


def _index_edges(net) -> dict[str, list]:
    by_way: dict[str, list] = {}
    for edge in net.getEdges():
        if not _is_normal(edge):
            continue
        for way_id in _lane_orig_ids(edge):
            by_way.setdefault(way_id, []).append(edge)
    return by_way


def restriction_edits(restrictions: list[Restriction], by_way: dict[str, list]) -> tuple[list[Edit], list[Suspect], dict[str, int]]:
    edits: list[Edit] = []
    suspects: list[Suspect] = []
    counts = {
        "relations": len(restrictions),
        "prohibitions_removed": 0,
        "only_connections_removed": 0,
        "already_respected": 0,
        "ignored_not_a_turn_ban": 0,
        "skipped_car_exception": 0,
    }
    for restriction in restrictions:
        if restriction.kind in {None, ""}:
            counts["ignored_not_a_turn_ban"] += 1
            continue
        if restriction.kind == "no_entry":
            counts["ignored_not_a_turn_ban"] += 1
            suspects.append(
                Suspect(
                    "osm_no_entry",
                    restriction.via_node or "",
                    "",
                    restriction.from_way or "",
                    restriction.to_way or "",
                    f"relation {restriction.relation_id} is no_entry and was not edited",
                )
            )
            continue
        if restriction.kind not in PROHIBITION_DIRS and restriction.kind not in ONLY_DIRS:
            counts["ignored_not_a_turn_ban"] += 1
            continue
        if excepts_cars(";".join(restriction.except_classes)):
            counts["skipped_car_exception"] += 1
            suspects.append(
                Suspect(
                    "osm_exception_may_allow_cars",
                    restriction.via_node or "",
                    "",
                    restriction.from_way or "",
                    restriction.to_way or "",
                    f"relation {restriction.relation_id} excepts {', '.join(restriction.except_classes)}",
                )
            )
            continue
        if restriction.from_way is None or restriction.to_way is None:
            suspects.append(
                Suspect(
                    "osm_incomplete",
                    restriction.via_node or "",
                    "",
                    restriction.from_way or "",
                    restriction.to_way or "",
                    f"relation {restriction.relation_id} does not have one from way and one to way",
                )
            )
            continue
        if restriction.via_node is None:
            suspects.append(
                Suspect(
                    "osm_via_way",
                    "",
                    "",
                    restriction.from_way,
                    restriction.to_way,
                    f"relation {restriction.relation_id} uses a via way ({', '.join(restriction.via_ways) or 'none'})",
                )
            )
            continue
        from_edges = [
            edge
            for edge in by_way.get(restriction.from_way, [])
            if node_contains_via(edge.getToNode().getID(), restriction.via_node)
        ]
        to_edges = [
            edge
            for edge in by_way.get(restriction.to_way, [])
            if node_contains_via(edge.getFromNode().getID(), restriction.via_node)
        ]
        to_ids = {edge.getID() for edge in to_edges}
        if len(from_edges) != 1 or not to_edges:
            suspects.append(
                Suspect(
                    "osm_unmatched",
                    restriction.via_node,
                    "",
                    restriction.from_way,
                    restriction.to_way,
                    (
                        f"relation {restriction.relation_id} ({restriction.kind}) matched "
                        f"{len(from_edges)} from edges and {len(to_edges)} to edges"
                    ),
                )
            )
            continue
        from_edge = from_edges[0]
        outgoing = [conn for lane in from_edge.getLanes() for conn in lane.getOutgoing()]
        street = from_edge.getName() or ""
        junction = from_edge.getToNode().getID()
        if restriction.kind in PROHIBITION_DIRS:
            matched = [conn for conn in outgoing if conn.getTo().getID() in to_ids]
            indexes = prohibited_indexes(
                [conn.getDirection() for conn in matched],
                PROHIBITION_DIRS[restriction.kind],
            )
            if not matched:
                counts["already_respected"] += 1
                continue
            if indexes is None:
                found = ", ".join(sorted({conn.getDirection() for conn in matched}))
                suspects.append(
                    Suspect(
                        "osm_direction_mismatch",
                        junction,
                        street,
                        from_edge.getID(),
                        restriction.to_way,
                        f"relation {restriction.relation_id} is {restriction.kind} but the turn direction is {found}",
                    )
                )
                continue
            for index in indexes:
                conn = matched[index]
                edits.append(
                    Edit(
                        action="delete",
                        from_edge=from_edge.getID(),
                        to_edge=conn.getTo().getID(),
                        from_lane=conn.getFromLane().getIndex(),
                        to_lane=conn.getToLane().getIndex(),
                        reason="osm_prohibition",
                        junction=junction,
                        street=street,
                        evidence=(
                            f"OSM relation {restriction.relation_id} {restriction.kind} "
                            f"from way {restriction.from_way} via {restriction.via_node} to way {restriction.to_way}"
                        ),
                    )
                )
                counts["prohibitions_removed"] += 1
            continue
        extras = only_extra_indexes(
            [(conn.getTo().getID(), conn.getDirection()) for conn in outgoing],
            to_ids,
            ONLY_DIRS[restriction.kind],
        )
        if extras is None:
            suspects.append(
                Suspect(
                    "osm_only_missing_allowed_turn",
                    junction,
                    street,
                    from_edge.getID(),
                    restriction.to_way,
                    f"relation {restriction.relation_id} is {restriction.kind} but the allowed turn is absent",
                )
            )
            continue
        if not extras:
            counts["already_respected"] += 1
            continue
        for index in extras:
            conn = outgoing[index]
            edits.append(
                Edit(
                    action="delete",
                    from_edge=from_edge.getID(),
                    to_edge=conn.getTo().getID(),
                    from_lane=conn.getFromLane().getIndex(),
                    to_lane=conn.getToLane().getIndex(),
                    reason="osm_only",
                    junction=junction,
                    street=street,
                    evidence=(
                        f"OSM relation {restriction.relation_id} {restriction.kind} "
                        f"keeps way {restriction.to_way} and removes this extra turn"
                    ),
                )
            )
            counts["only_connections_removed"] += 1
    return edits, suspects, counts


def _merge_edits(edits: list[Edit]) -> list[Edit]:
    merged: dict[tuple[str, str, str, int, int], Edit] = {}
    for edit in edits:
        key = (edit.action, *edit.connection_key)
        current = merged.get(key)
        if current is None:
            merged[key] = edit
            continue
        if edit.evidence not in current.evidence:
            merged[key] = replace(
                current,
                reason=f"{current.reason}+{edit.reason}",
                evidence=f"{current.evidence} | {edit.evidence}",
            )
    return list(merged.values())


def _edge_type_name(edge) -> str:
    getter = getattr(edge, "getType", None)
    if getter is None:
        return ""
    return getter() or ""


def _lane_kinds(lane, way_kinds: dict[str, str]) -> list[str]:
    kinds: list[str] = []
    for way_id in orig_way_ids(lane.getParam("origId", "") or ""):
        kind = way_kinds.get(way_id)
        if kind and kind not in kinds:
            kinds.append(kind)
    return kinds


def collect_tram_decisions(net, way_kinds: dict[str, str]) -> tuple[list[LaneDecision], list[Suspect], dict[str, int]]:
    """Lane permission edits for reserved track versus street-running track."""
    decisions: list[LaneDecision] = []
    suspects: list[Suspect] = []
    counts = {
        "exclusive_tram_lanes": 0,
        "exclusive_tram_lanes_allowing_cars": 0,
        "shared_lanes": 0,
        "shared_lanes_missing_tram": 0,
        "shared_lanes_missing_cars": 0,
        "ambiguous_lanes": 0,
        "car_to_exclusive_tram_connections": 0,
    }
    if not way_kinds:
        return decisions, suspects, counts
    described_by_edge: list[tuple] = []
    for edge in net.getEdges():
        if not _is_normal(edge):
            continue
        described = []
        for lane in edge.getLanes():
            kinds = _lane_kinds(lane, way_kinds)
            allows_passenger = lane.allows("passenger")
            allows_tram = lane.allows("tram")
            described.append((lane, kinds, allows_passenger, allows_tram))
            if "exclusive_tram" in kinds and "shared_road" not in kinds and "car_road" not in kinds:
                counts["exclusive_tram_lanes"] += 1
                if allows_passenger:
                    counts["exclusive_tram_lanes_allowing_cars"] += 1
            if "shared_road" in kinds:
                counts["shared_lanes"] += 1
                if allows_passenger and not allows_tram:
                    counts["shared_lanes_missing_tram"] += 1
                if allows_tram and not allows_passenger:
                    counts["shared_lanes_missing_cars"] += 1
        described_by_edge.append((edge, described))
        street = edge.getName() or ""
        junction = edge.getToNode().getID()
        for lane, kinds, allows_passenger, allows_tram in described:
            other_keeps_cars = any(
                other_passenger and "car_road" in other_kinds and "exclusive_tram" not in other_kinds
                for other_lane, other_kinds, other_passenger, _other_tram in described
                if other_lane is not lane
            )
            action = decide_lane(
                kinds,
                allows_passenger=allows_passenger,
                allows_tram=allows_tram,
                edge_type=_edge_type_name(edge),
                other_lane_keeps_cars=other_keeps_cars,
            )
            if action == "keep":
                continue
            evidence = f"ways {', '.join(kinds) or 'none'}; type {_edge_type_name(edge) or 'unset'}"
            if action == "suspect":
                counts["ambiguous_lanes"] += 1
                suspects.append(
                    Suspect(
                        "tram_access_ambiguous",
                        junction,
                        street,
                        edge.getID(),
                        "",
                        f"lane {lane.getIndex()} was not edited; {evidence}",
                    )
                )
                continue
            decisions.append(
                LaneDecision(
                    edge_id=edge.getID(),
                    lane_index=lane.getIndex(),
                    lane_id=lane.getID(),
                    action=action,
                    street=street,
                    evidence=evidence,
                )
            )
    actions = {(decision.edge_id, decision.lane_index): decision.action for decision in decisions}
    for edge, described in described_by_edge:
        for lane, _kinds, allows_passenger, allows_tram in described:
            from_passenger, from_tram = final_lane_flags(
                allows_passenger,
                allows_tram,
                actions.get((edge.getID(), lane.getIndex())),
            )
            for conn in lane.getOutgoing():
                to_edge = conn.getTo()
                to_lane = conn.getToLane()
                to_passenger, to_tram = final_lane_flags(
                    to_lane.allows("passenger"),
                    to_lane.allows("tram"),
                    actions.get((to_edge.getID(), to_lane.getIndex())),
                )
                if from_passenger and not from_tram and to_tram and not to_passenger:
                    counts["car_to_exclusive_tram_connections"] += 1
    return decisions, suspects, counts


def tram_crossing_edits(net, decisions: list[LaneDecision]) -> list[Edit]:
    """Delete connections that join a car-only lane to a tram-only lane."""
    actions = {(decision.edge_id, decision.lane_index): decision.action for decision in decisions}
    edits: list[Edit] = []

    def flags(edge, lane) -> tuple[bool, bool]:
        return final_lane_flags(
            lane.allows("passenger"),
            lane.allows("tram"),
            actions.get((edge.getID(), lane.getIndex())),
        )

    for edge in net.getEdges():
        if not _is_normal(edge):
            continue
        street = edge.getName() or ""
        junction = edge.getToNode().getID()
        for lane in edge.getLanes():
            from_passenger, from_tram = flags(edge, lane)
            for conn in lane.getOutgoing():
                to_edge = conn.getTo()
                to_lane = conn.getToLane()
                to_passenger, to_tram = flags(to_edge, to_lane)
                if not exclusive_tram_crossing(from_passenger, from_tram, to_passenger, to_tram):
                    continue
                edits.append(
                    Edit(
                        action="delete",
                        from_edge=edge.getID(),
                        to_edge=to_edge.getID(),
                        from_lane=lane.getIndex(),
                        to_lane=to_lane.getIndex(),
                        reason="tram_crossing",
                        junction=junction,
                        street=street,
                        evidence="connection joins a car-only lane and a tram-only lane",
                    )
                )
    return edits


def collect_corrections(
    net,
    restrictions: list[Restriction],
    way_kinds: dict[str, str] | None = None,
) -> tuple[list[Edit], list[Suspect], dict[str, int], list[LaneDecision], dict[str, int]]:
    way_kinds = way_kinds or {}
    lane_decisions, tram_suspects, tram_counts = collect_tram_decisions(net, way_kinds)
    crossing_edits = tram_crossing_edits(net, lane_decisions)
    blocked = {(decision.edge_id, decision.lane_index) for decision in lane_decisions if decision.action == "remove_cars"}
    opened = {(decision.edge_id, decision.lane_index) for decision in lane_decisions if decision.action == "add_cars"}
    restriction_list, restriction_suspects, restriction_counts = restriction_edits(restrictions, _index_edges(net))
    removed = {edit.connection_key for edit in restriction_list if edit.action == "delete"}
    removed.update(edit.connection_key for edit in crossing_edits)
    edits: list[Edit] = list(restriction_list)
    edits.extend(crossing_edits)
    suspects: list[Suspect] = list(restriction_suspects)
    suspects.extend(tram_suspects)
    for edge in net.getEdges():
        if not _is_normal(edge):
            continue
        edge_edits, edge_suspects = _edge_edits_and_suspects(edge, removed, blocked, opened)
        edits.extend(edge_edits)
        suspects.extend(edge_suspects)
    return _merge_edits(edits), suspects, restriction_counts, lane_decisions, tram_counts


def _connection_keys(net) -> set[tuple[str, str, int, int]]:
    keys: set[tuple[str, str, int, int]] = set()
    for edge in net.getEdges():
        if not _is_normal(edge):
            continue
        for lane in edge.getLanes():
            for conn in lane.getOutgoing():
                keys.add(
                    (
                        edge.getID(),
                        conn.getTo().getID(),
                        conn.getFromLane().getIndex(),
                        conn.getToLane().getIndex(),
                    )
                )
    return keys


def _load_net(path: Path, sumo_home: Path):
    os.environ["SUMO_HOME"] = str(sumo_home)
    tools = str(sumo_home / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import sumolib

    return sumolib.net.readNet(str(path), withInternal=False)


def _normal_edge_ids(net) -> set[str]:
    return {edge.getID() for edge in net.getEdges() if _is_normal(edge)}


def _run_sumo(binary: str, net_path: Path, routes: Path | None, end: int) -> subprocess.CompletedProcess[str]:
    command = [binary, "-n", str(net_path), "--no-step-log", "--end", str(end), "--ignore-route-errors"]
    if routes is not None:
        command.extend(["-r", str(routes)])
    return subprocess.run(command, text=True, capture_output=True, timeout=600)


def _vehicle_route_failures(sumo_bin: str, net_path: Path, edits: list[Edit], output_dir: Path) -> dict[str, int | list[str]]:
    adds = [edit for edit in edits if edit.action == "add"]
    if not adds:
        return {"tested": 0, "rejected": 0, "rejected_ids": []}
    routes_path = output_dir / "added-lanes.rou.xml"
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<routes>", '    <vType id="car" vClass="passenger" length="2.0"/>']
    for index, edit in enumerate(adds):
        lines.append(
            f'    <vehicle id="added-{index}" type="car" depart="0" departLane="{edit.from_lane}" departPos="base" departSpeed="0">'
        )
        lines.append(f"        <route edges={quoteattr(edit.from_edge + ' ' + edit.to_edge)}/>")
        lines.append("    </vehicle>")
    lines.append("</routes>")
    lines.append("")
    routes_path.write_text("\n".join(lines), encoding="utf-8")
    completed = _run_sumo(sumo_bin, net_path, routes_path, end=1)
    log_path = output_dir / "added-lanes.sumo.log"
    log_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    rejected: list[str] = []
    for line in (completed.stdout + completed.stderr).splitlines():
        if "no valid route" in line.lower() or "no connection" in line.lower():
            rejected.append(line.strip())
    return {
        "tested": len(adds),
        "rejected": len(rejected),
        "rejected_ids": rejected[:20],
        "exit_code": completed.returncode,
    }


def _write_table(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _successors(net) -> dict[str, tuple[str, ...]]:
    graph: dict[str, tuple[str, ...]] = {}
    for edge in net.getEdges():
        if not _is_normal(edge):
            continue
        targets: list[str] = []
        for lane in edge.getLanes():
            for conn in lane.getOutgoing():
                to_id = conn.getTo().getID()
                if to_id not in targets:
                    targets.append(to_id)
        graph[edge.getID()] = tuple(targets)
    return graph


def _suspect_links(suspects: list[Suspect]) -> list[tuple[str, str]]:
    return [
        (suspect.from_edge, suspect.to_edge)
        for suspect in suspects
        if suspect.kind in {"missing_turn", "osm_direction_mismatch", "osm_only_missing_allowed_turn"}
        and suspect.from_edge
        and suspect.to_edge
        and " " not in suspect.from_edge
        and " " not in suspect.to_edge
    ]


def _external_map_status(route_checks: list[dict[str, object]]) -> str:
    if os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("TOMTOM_API_KEY"):
        return (
            "A Google or TomTom key is set, but this command does not call those APIs. "
            "The driving sample uses the public OSRM car profile, which follows OpenStreetMap access tags."
        )
    if not route_checks:
        return "No driving-route sample was collected."
    if any(row.get("error") for row in route_checks):
        return "The OSRM driving sample could not be completed. Google and TomTom were not called."
    return (
        "A sample of reserved tracks and embedded streets was checked with the public OSRM car profile. "
        "OSRM follows OpenStreetMap, so this checks consistency with the tags. It is not a Google or TomTom survey. "
        "No GOOGLE_MAPS_API_KEY or TOMTOM_API_KEY is set."
    )


def _decision_counts(decisions: list[LaneDecision]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions:
        counts[decision.action] = counts.get(decision.action, 0) + 1
    return counts


def _permission_errors(net, decisions: list[LaneDecision]) -> list[str]:
    errors: list[str] = []
    for decision in decisions:
        try:
            edge = net.getEdge(decision.edge_id)
            lane = edge.getLane(decision.lane_index)
        except (KeyError, IndexError, AttributeError):
            errors.append(f"{decision.lane_id} is missing after correction")
            continue
        allows_passenger = lane.allows("passenger")
        allows_tram = lane.allows("tram")
        if decision.action == "remove_cars" and allows_passenger:
            errors.append(f"{decision.lane_id} still allows passenger")
        if decision.action == "remove_cars" and not allows_tram:
            errors.append(f"{decision.lane_id} lost tram permission")
        if decision.action == "add_tram" and not allows_tram:
            errors.append(f"{decision.lane_id} still refuses tram")
        if decision.action == "add_cars" and (not allows_passenger or not allows_tram):
            errors.append(f"{decision.lane_id} is not shared by cars and trams")
    return errors


def _tram_counts(net, way_kinds: dict[str, str]) -> dict[str, int]:
    _decisions, _suspects, counts = collect_tram_decisions(net, way_kinds)
    return counts


def _nearby_street(edge) -> str:
    if edge.getName():
        return edge.getName()
    names: list[str] = []
    for node in (edge.getFromNode(), edge.getToNode()):
        for other in list(node.getIncoming()) + list(node.getOutgoing()):
            name = other.getName()
            if name and name not in names:
                names.append(name)
    return " / ".join(names[:3])


def _shape_endpoints(net, edge) -> tuple[float, float, float, float, float, float] | None:
    shape = edge.getShape()
    if len(shape) < 2:
        return None
    lon1, lat1 = net.convertXY2LonLat(shape[0][0], shape[0][1])
    lon2, lat2 = net.convertXY2LonLat(shape[-1][0], shape[-1][1])
    mid = shape[len(shape) // 2]
    lon, lat = net.convertXY2LonLat(mid[0], mid[1])
    return lon1, lat1, lon2, lat2, lon, lat


def _osrm_offset_meters(lon1: float, lat1: float, lon2: float, lat2: float, lon: float, lat: float) -> float | None:
    url = (
        "https://router.project-osrm.org/route/v1/driving/"
        f"{lon1:.6f},{lat1:.6f};{lon2:.6f},{lat2:.6f}?overview=full&geometries=geojson"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "matsim-sumo-pipeline/tram-access-check"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("code") != "Ok":
        return None
    coordinates = payload["routes"][0]["geometry"]["coordinates"]
    return point_to_polyline_meters(lat, lon, [(item[0], item[1]) for item in coordinates])


def _driving_route_sample(net, way_kinds: dict[str, str], limit: int = 6) -> list[dict[str, object]]:
    """Ask OSRM whether a car route follows reserved track and embedded streets.

    At most ``limit`` edges of each kind are checked. A failure is recorded and
    does not discard the corrected network.
    """
    chosen: dict[str, list] = {"reserved": [], "embedded": []}
    seen_streets: set[str] = set()
    for edge in net.getEdges():
        if not _is_normal(edge):
            continue
        kinds: set[str] = set()
        allows_passenger = False
        allows_tram = False
        for lane in edge.getLanes():
            kinds.update(_lane_kinds(lane, way_kinds))
            allows_passenger = allows_passenger or lane.allows("passenger")
            allows_tram = allows_tram or lane.allows("tram")
        if "shared_road" in kinds and allows_passenger and allows_tram:
            role = "embedded"
        elif "exclusive_tram" in kinds and allows_tram and not allows_passenger and "shared_road" not in kinds:
            role = "reserved"
        else:
            continue
        street = _nearby_street(edge)
        if not street or street in seen_streets or len(chosen[role]) >= limit:
            continue
        try:
            endpoints = _shape_endpoints(net, edge)
        except Exception:
            continue
        if endpoints is None:
            continue
        seen_streets.add(street)
        chosen[role].append((edge, street, endpoints))
        if len(chosen["reserved"]) >= limit and len(chosen["embedded"]) >= limit:
            break
    rows: list[dict[str, object]] = []
    for role, items in chosen.items():
        for edge, street, endpoints in items:
            lon1, lat1, lon2, lat2, lon, lat = endpoints
            row: dict[str, object] = {
                "role": role,
                "street": street,
                "edge": edge.getID(),
                "junction": edge.getToNode().getID(),
                "lat": round(lat, 6),
                "lon": round(lon, 6),
            }
            try:
                offset = _osrm_offset_meters(lon1, lat1, lon2, lat2, lon, lat)
            except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
                row["error"] = str(exc)
                rows.append(row)
                continue
            row["driving_offset_m"] = None if offset is None else round(offset, 1)
            if offset is None:
                row["reading"] = "no driving route"
            elif role == "reserved" and offset > 25:
                row["reading"] = "driving route stays off the track"
            elif role == "reserved":
                row["reading"] = "driving route stays in the same corridor; this does not show that cars may use the track"
            elif offset <= 30:
                row["reading"] = "driving route follows the street"
            else:
                row["reading"] = "driving route leaves the street"
            rows.append(row)
    return rows


def _print_counts(title: str, counts: dict[str, int], keys: list[tuple[str, str]]) -> None:
    print(title, flush=True)
    for label, key in keys:
        print(f"  {label}: {counts[key]}", flush=True)


COUNT_ROWS = [
    ("edges", "edges"),
    ("junctions", "junctions"),
    ("connections", "connections"),
    ("U-turns that have another exit", "uturn_connections_with_another_exit"),
    ("U-turns that are the only exit", "exclusive_uturn_connections"),
    ("empty lanes with one straight continuation", "orphan_lanes_with_one_straight"),
    ("empty lanes with no single straight continuation", "orphan_lanes_without_one_straight"),
    ("empty lanes at a dead end", "fringe_empty_lanes"),
]


def correct_zurich_network(
    source: Path,
    output_dir: Path,
    osm_path: Path | None = None,
    sumo_home: Path | None = None,
    skip_vehicle_check: bool = False,
) -> Path:
    """Read ``source`` and write a corrected network under ``output_dir``.

    ``source`` is only read. The corrected file is ``output_dir / zurich.corrected.net.xml``.
    """
    source = Path(source)
    output_dir = Path(output_dir)
    if not source.is_file():
        raise SystemExit(f"Source network not found: {source}")
    output_path = output_dir / CORRECTED_NAME
    assert_distinct_output(source, output_path)
    if "corrected" in source.name:
        print(
            "Warning: the source file name contains 'corrected'. "
            f"The usual source is the unedited network at {DEFAULT_OUTPUT}.",
            flush=True,
        )
    home = sumo_home or find_sumo_home()
    os.environ["SUMO_HOME"] = str(home)
    output_dir.mkdir(parents=True, exist_ok=True)
    history_dir = output_dir / "history"
    previous_report = output_dir / "report.json"
    if previous_report.is_file():
        history_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (history_dir / f"{stamp}.json").write_text(previous_report.read_text(encoding="utf-8"), encoding="utf-8")

    with source.open("r", encoding="utf-8", errors="replace") as handle:
        head = handle.read(2000)
    if "<net" not in head:
        raise SystemExit(
            f"{source} is not a SUMO network. The correction reads a .net.xml file. "
            "A MATSim output_network.xml cannot be corrected with these rules."
        )
    print(f"Reading unedited network {source}", flush=True)
    net = _load_net(source, home)
    before = inspect_network(net)
    osm = osm_path or default_osm_path(source)
    if osm.is_file():
        print(f"Reading OSM restrictions and tram access {osm}", flush=True)
        restrictions = parse_restrictions(osm)
        way_kinds = parse_way_kinds(osm)
    else:
        print(f"OSM file not found at {osm}. Restriction and tram-access edits will be skipped.", flush=True)
        restrictions = []
        way_kinds = {}
    edits, suspects, restriction_counts, lane_decisions, tram_counts = collect_corrections(net, restrictions, way_kinds)
    deletes = {edit.connection_key for edit in edits if edit.action == "delete"}
    adds = {edit.connection_key for edit in edits if edit.action == "add"}
    if deletes & adds:
        overlap = ", ".join(str(item) for item in sorted(deletes & adds)[:5])
        raise SystemExit(f"A connection was both added and deleted: {overlap}")
    before_keys = _connection_keys(net)
    expected = edited_connections(before_keys, deletes, adds)

    patch_path = output_dir / "corrections.con.xml"
    staged_path = output_dir / "zurich.corrected.staged.net.xml"
    permission_path = output_dir / "zurich.permissions.net.xml"
    patch_path.write_text(connection_xml(edits, source), encoding="utf-8")
    net_input = source
    if lane_decisions:
        print(f"Updating permissions on {len(lane_decisions)} tram lanes...", flush=True)
        apply_lane_permissions(source, permission_path, lane_decisions)
        net_input = permission_path
    netconvert = find_netconvert(home)
    log_path = output_dir / "netconvert.log"
    command = [
        netconvert,
        "--sumo-net-file",
        str(net_input),
        "--connection-files",
        str(patch_path),
        "--output-file",
        str(staged_path),
        "--output.street-names",
        "--output.original-names",
    ]
    print(f"Applying {len(edits)} connection edits with netconvert...", flush=True)
    completed = subprocess.run(command, text=True, capture_output=True, timeout=600)
    log_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode != 0 or not staged_path.is_file():
        sys.stderr.write(completed.stderr[-4000:])
        raise SystemExit(f"netconvert failed. See {log_path}")
    staged_path.write_text(strip_newer_vclasses(staged_path.read_text(encoding="utf-8")), encoding="utf-8")

    print("Checking that only the requested connections changed...", flush=True)
    corrected = _load_net(staged_path, home)
    edge_ids_unchanged = _normal_edge_ids(net) == _normal_edge_ids(corrected)
    after_keys = _connection_keys(corrected)
    unexpected = sorted(after_keys.symmetric_difference(expected))
    if not edge_ids_unchanged or unexpected:
        sample = "\n".join(str(item) for item in unexpected[:20])
        rejected = output_dir / "zurich.corrected.rejected.net.xml"
        staged_path.replace(rejected)
        raise SystemExit(
            "The corrected network changed more than the edit list allows. "
            f"Edge ids unchanged: {edge_ids_unchanged}. Unexpected connections: {len(unexpected)}.\n"
            f"{sample}\nRejected network: {rejected}"
        )
    staged_path.replace(output_path)
    permission_errors = _permission_errors(corrected, lane_decisions)
    if permission_errors:
        sample = "\n".join(permission_errors[:10])
        raise SystemExit(f"Tram lane permissions were not applied.\n{sample}")
    after = inspect_network(corrected)
    tram_after = _tram_counts(corrected, way_kinds)
    route_checks = _driving_route_sample(corrected, way_kinds)

    sumo_bin = str(home / "bin" / "sumo")
    if not Path(sumo_bin).is_file():
        sumo_bin = "sumo"
    load_128 = _run_sumo(sumo_bin, output_path, None, end=0)
    system_sumo = Path("/usr/bin/sumo")
    load_118 = _run_sumo(str(system_sumo), output_path, None, end=0) if system_sumo.is_file() else None
    vehicle = {"tested": 0, "rejected": 0, "rejected_ids": [], "skipped": True}
    if not skip_vehicle_check:
        print("Driving one vehicle across each reconnected lane...", flush=True)
        vehicle = _vehicle_route_failures(sumo_bin, output_path, edits, output_dir)
        vehicle["skipped"] = False

    reason_counts: dict[str, int] = {}
    for edit in edits:
        for reason in edit.reason.split("+"):
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    suspect_counts: dict[str, int] = {}
    for suspect in suspects:
        suspect_counts[suspect.kind] = suspect_counts.get(suspect.kind, 0) + 1
    routes = cover_routes_on_graph(_suspect_links(suspects), _successors(corrected))
    report = {
        "source": str(source),
        "source_sha256": file_sha256(source),
        "output": str(output_path),
        "osm": str(osm) if osm.is_file() else None,
        "rules_version": 3,
        "rules": list(RULES),
        "external_map_check": _external_map_status(route_checks),
        "before": before,
        "after": after,
        "tram_access": {"before": tram_counts, "after": tram_after, "lane_edits": _decision_counts(lane_decisions)},
        "driving_route_sample": route_checks,
        "applied": reason_counts,
        "osm_restrictions": restriction_counts,
        "suspects": suspect_counts,
        "route_cover": {
            "routes": len(routes),
            "suspect_maneuvers": sum(1 for route in routes for step in route if step[2] == "suspect"),
            "connector_edges": sum(1 for route in routes for step in route if step[2] == "link"),
        },
        "checks": {
            "edge_ids_unchanged": True,
            "connections_match_edit_list": True,
            "sumo_loads": load_128.returncode == 0,
            "sumo_118_loads": None if load_118 is None else load_118.returncode == 0,
            "added_lane_routes_rejected": vehicle.get("rejected", 0),
        },
        "vehicle_check": {key: value for key, value in vehicle.items() if key != "rejected_ids"},
    }
    (output_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_table(
        output_dir / "corrections.csv",
        ["action", "reason", "junction", "street", "from_edge", "to_edge", "from_lane", "to_lane", "evidence"],
        [
            {
                "action": edit.action,
                "reason": edit.reason,
                "junction": edit.junction,
                "street": edit.street,
                "from_edge": edit.from_edge,
                "to_edge": edit.to_edge,
                "from_lane": str(edit.from_lane),
                "to_lane": str(edit.to_lane),
                "evidence": edit.evidence,
            }
            for edit in edits
        ],
    )
    _write_table(
        output_dir / "suspects.csv",
        ["kind", "junction", "street", "from_edge", "to_edge", "detail"],
        [
            {
                "kind": suspect.kind,
                "junction": suspect.junction,
                "street": suspect.street,
                "from_edge": suspect.from_edge,
                "to_edge": suspect.to_edge,
                "detail": suspect.detail,
            }
            for suspect in suspects
        ],
    )
    with (output_dir / "route-cover.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["route_id", "step", "role", "from_edge", "to_edge"])
        writer.writeheader()
        for route_id, route in enumerate(routes, start=1):
            for step, (from_edge, to_edge, role) in enumerate(route, start=1):
                writer.writerow(
                    {"route_id": route_id, "step": step, "role": role, "from_edge": from_edge, "to_edge": to_edge}
                )

    _print_counts("Before", before, COUNT_ROWS)
    _print_counts("After", after, COUNT_ROWS)
    _write_table(
        output_dir / "tram-lanes.csv",
        ["action", "street", "edge", "lane", "evidence"],
        [
            {
                "action": decision.action,
                "street": decision.street,
                "edge": decision.edge_id,
                "lane": str(decision.lane_index),
                "evidence": decision.evidence,
            }
            for decision in lane_decisions
        ],
    )
    _write_table(
        output_dir / "driving-routes.csv",
        ["role", "street", "edge", "lat", "lon", "driving_offset_m", "reading", "error"],
        [
            {
                "role": str(row.get("role", "")),
                "street": str(row.get("street", "")),
                "edge": str(row.get("edge", "")),
                "lat": str(row.get("lat", "")),
                "lon": str(row.get("lon", "")),
                "driving_offset_m": str(row.get("driving_offset_m", "")),
                "reading": str(row.get("reading", "")),
                "error": str(row.get("error", "")),
            }
            for row in route_checks
        ],
    )
    print(f"Edits written: {len(edits)}", flush=True)
    print(
        "Tram lanes: "
        f"reserved {tram_counts.get('exclusive_tram_lanes', 0)} "
        f"({tram_counts.get('exclusive_tram_lanes_allowing_cars', 0)} admitting cars), "
        f"embedded {tram_counts.get('shared_lanes', 0)}, "
        f"ambiguous {tram_counts.get('ambiguous_lanes', 0)}",
        flush=True,
    )
    for reason, count in sorted(reason_counts.items()):
        print(f"  {reason}: {count}", flush=True)
    print(f"Suspects left unchanged: {len(suspects)}", flush=True)
    print(f"Unedited source: {source.resolve()}", flush=True)
    print(f"Corrected network: {output_path.resolve()}", flush=True)
    print(f"Report: {(output_dir / 'report.json').resolve()}", flush=True)
    if load_128.returncode != 0:
        raise SystemExit(f"SUMO could not load {output_path}")
    if vehicle.get("rejected"):
        print(
            f"Warning: {vehicle['rejected']} reconnected lanes did not produce a valid SUMO route. See added-lanes.sumo.log.",
            flush=True,
        )
    return output_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Correct unambiguous intersection errors on the unedited City of Zürich SUMO network. "
            "The source file is not modified. Run the command again after changing the rules; "
            "each run starts from the same source and overwrites the corrected network and report."
        )
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Unedited SUMO network to read (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for the corrected network and report (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--osm",
        type=Path,
        default=None,
        help="OSM file used for restriction relations (default: the .osm.xml beside the source)",
    )
    parser.add_argument(
        "--skip-vehicle-check",
        action="store_true",
        help="Do not drive a test vehicle across each reconnected lane",
    )
    args = parser.parse_args(argv)
    correct_zurich_network(args.source, args.output_dir, args.osm, skip_vehicle_check=args.skip_vehicle_check)


if __name__ == "__main__":
    main()
