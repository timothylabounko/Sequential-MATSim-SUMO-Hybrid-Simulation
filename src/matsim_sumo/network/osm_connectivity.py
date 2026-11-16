"""Compare a SUMO network with OpenStreetMap and reject links that end nowhere.

A link leads nowhere when a passenger road reaches a junction that has another
drivable exit, but no connection uses it. OpenStreetMap's highway ways at that
junction decide whether the exit is real. A network is rejected when one of
those exits is still unconnected, or when a repair splits the drivable roads
into more pieces than the network had at the start.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

WAY_ID = re.compile(r"-?(\d+)")
NODE_ID = re.compile(r"\d+")
OVERPASS_ENDPOINTS = (
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
USER_AGENT = "matsim-sumo-pipeline/osm-connectivity"
OVERPASS_BATCH = 4


@dataclass(frozen=True)
class OsmContinuation:
    """One missing connection that OpenStreetMap shows at a dead-end approach."""

    from_edge: str
    to_edge: str
    junction: str
    street: str


def osm_way_id(edge_id: str) -> int | None:
    """Return the OpenStreetMap way id encoded in a SUMO edge id."""
    match = WAY_ID.match(edge_id)
    if match is None:
        return None
    return int(match.group(1))


def junction_node_ids(junction_id: str) -> list[int]:
    """Return the OpenStreetMap node ids named by a SUMO junction."""
    if junction_id.startswith(":"):
        return []
    return [int(item) for item in NODE_ID.findall(junction_id)]


def matching_exits(incoming_way: int | None, exits: list[tuple[str, int | None]], osm_ways: set[int]) -> list[str]:
    """Keep exits whose way is present at the junction in OpenStreetMap.

    The incoming way and exits with no way id are not continuations. The order
    of ``exits`` is preserved.
    """
    found: list[str] = []
    for edge_id, way_id in exits:
        if way_id is None or way_id == incoming_way or way_id not in osm_ways:
            continue
        if edge_id not in found:
            found.append(edge_id)
    return found


def connectivity_problems(
    *,
    unresolved: list[str],
    components: int,
    baseline_components: int,
    drive_components: int | None = None,
) -> list[str]:
    """Describe why a candidate network is not an acceptable repair."""
    problems: list[str] = []
    if unresolved:
        problems.append(f"{len(unresolved)} links lead nowhere")
    if components > baseline_components:
        problems.append(f"the network split from {baseline_components} pieces into {components}")
    if drive_components is not None and drive_components > components:
        problems.append(
            f"the drivable network is in {drive_components} pieces while the roads form {components}"
        )
    return problems


def fetch_highway_ways(
    node_ids: list[int],
    cache: dict[str, list[int]] | None = None,
    on_batch=None,
) -> dict[int, set[int]]:
    """Return highway way ids at each OpenStreetMap node, using the Overpass API.

    ``cache`` maps a node id string to way ids already retrieved. Cached nodes
    are not requested again. Nodes are requested a few at a time so one slow
    junction cannot time out the whole comparison. ``on_batch`` runs after each
    successful batch, which lets the caller store the cache before the next one.
    """
    stored = cache if cache is not None else {}
    needed = [node_id for node_id in dict.fromkeys(node_ids) if str(node_id) not in stored]
    for start in range(0, len(needed), OVERPASS_BATCH):
        batch = needed[start : start + OVERPASS_BATCH]
        query = (
            "[out:json][timeout:25];"
            f"node(id:{','.join(str(node_id) for node_id in batch)});"
            "way(bn)[highway];out skel;"
        )
        payload = _overpass(query)
        found: dict[int, set[int]] = {node_id: set() for node_id in batch}
        for element in payload.get("elements", []):
            if element.get("type") != "way":
                continue
            way_id = int(element["id"])
            for node_id in element.get("nodes", []):
                if node_id in found:
                    found[node_id].add(way_id)
        for node_id, ways in found.items():
            stored[str(node_id)] = sorted(ways)
        if on_batch is not None:
            on_batch()
        if start + OVERPASS_BATCH < len(needed):
            time.sleep(1.0)
    return {node_id: set(stored.get(str(node_id), [])) for node_id in dict.fromkeys(node_ids)}


def _is_normal(edge) -> bool:
    return not edge.getID().startswith(":") and edge.getFunction() in ("", "normal")


def _passenger(edge) -> bool:
    return _is_normal(edge) and edge.allows("passenger")


def passenger_component_count(net) -> int:
    """Count pieces of the drivable road network."""
    parent: dict[str, str] = {}

    def find(node_id: str) -> str:
        parent.setdefault(node_id, node_id)
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    seen: set[str] = set()
    for edge in net.getEdges():
        if not _passenger(edge):
            continue
        union(edge.getFromNode().getID(), edge.getToNode().getID())
        seen.add(edge.getFromNode().getID())
    return len({find(node_id) for node_id in seen})


def connection_component_count(net) -> int:
    """Count pieces you can drive between, ignoring one-way direction.

    Two passenger edges are in the same piece when a passenger connection
    joins them. A link that reaches a junction and then stops is its own piece
    until that connection exists.
    """
    parent: dict[str, str] = {}

    def find(edge_id: str) -> str:
        parent.setdefault(edge_id, edge_id)
        while parent[edge_id] != edge_id:
            parent[edge_id] = parent[parent[edge_id]]
            edge_id = parent[edge_id]
        return edge_id

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    edges = [edge for edge in net.getEdges() if _passenger(edge)]
    for edge in edges:
        parent.setdefault(edge.getID(), edge.getID())
    for edge in edges:
        for lane in edge.getLanes():
            if not lane.allows("passenger"):
                continue
            for connection in lane.getOutgoing():
                other = connection.getTo()
                if _passenger(other):
                    union(edge.getID(), other.getID())
    return len({find(edge.getID()) for edge in edges})


def nowhere_links(net) -> list[tuple[object, list[object]]]:
    """Return passenger edges that stop despite another drivable exit at the junction."""
    found = []
    for edge in net.getEdges():
        if not _passenger(edge):
            continue
        connected = [conn for lane in edge.getLanes() if lane.allows("passenger") for conn in lane.getOutgoing()]
        if connected:
            continue
        exits = [
            other
            for other in edge.getToNode().getOutgoing()
            if _passenger(other) and other.getID() != edge.getID()
        ]
        if exits:
            found.append((edge, exits))
    return found


def _is_reversal(edge, other) -> bool:
    from matsim_sumo.network.zurich_correction import is_opposite_edge

    if is_opposite_edge(edge.getID(), other.getID()):
        return True
    return (
        other.getFromNode().getID() == edge.getToNode().getID()
        and other.getToNode().getID() == edge.getFromNode().getID()
    )


def required_continuations(net, osm_ways_by_node: dict[int, set[int]]) -> list[OsmContinuation]:
    """Missing connections whose outgoing way is present in OpenStreetMap.

    A different OSM way is a through movement. Where the only OSM exit is the
    reverse of the same street, that turnaround is added so the link does not
    trap a vehicle. A rail crossing is left unchanged: the network builder
    drops a U-turn there, and OpenStreetMap does not show a separate road.
    """
    found: list[OsmContinuation] = []
    seen: set[tuple[str, str]] = set()
    for edge, exits in nowhere_links(net):
        node_ids = junction_node_ids(edge.getToNode().getID())
        osm_ways: set[int] = set()
        for node_id in node_ids:
            osm_ways.update(osm_ways_by_node.get(node_id, set()))
        incoming_way = osm_way_id(edge.getID())
        through: list[str] = []
        turnarounds: list[str] = []
        for other in exits:
            way_id = osm_way_id(other.getID())
            if way_id is None or way_id not in osm_ways:
                continue
            if _is_reversal(edge, other):
                if edge.getToNode().getType() not in ("rail_crossing", "rail_signal"):
                    turnarounds.append(other.getID())
                continue
            if way_id != incoming_way:
                through.append(other.getID())
        chosen = through or turnarounds
        for to_edge in chosen:
            key = (edge.getID(), to_edge)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                OsmContinuation(
                    from_edge=edge.getID(),
                    to_edge=to_edge,
                    junction=edge.getToNode().getID(),
                    street=edge.getName() or "",
                )
            )
    return found


def lane_pairs(from_edge, to_edge) -> list[tuple[int, int]]:
    """Connect every passenger lane on the approach to the nearest passenger lane on the exit."""
    from_lanes = [lane.getIndex() for lane in from_edge.getLanes() if lane.allows("passenger")]
    to_lanes = [lane.getIndex() for lane in to_edge.getLanes() if lane.allows("passenger")]
    if not from_lanes or not to_lanes:
        return []
    return [(index, min(to_lanes, key=lambda lane: abs(lane - index))) for index in from_lanes]


def connection_xml(pairs: list[tuple[str, str, int, int]]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<connections>"]
    for from_edge, to_edge, from_lane, to_lane in pairs:
        lines.append(
            f'    <connection from="{from_edge}" to="{to_edge}" fromLane="{from_lane}" toLane="{to_lane}"/>'
        )
    lines.append("</connections>")
    lines.append("")
    return "\n".join(lines)


def missing_component_links(net, osm_ways_by_node: dict[int, set[int]]) -> list[OsmContinuation]:
    """One OpenStreetMap connection wherever two drivable pieces meet at a junction.

    The pieces already share a node, so this does not invent a road. It only
    adds a turn between two ways OpenStreetMap already places on that node.
    """
    parent: dict[str, str] = {}

    def find(edge_id: str) -> str:
        parent.setdefault(edge_id, edge_id)
        while parent[edge_id] != edge_id:
            parent[edge_id] = parent[parent[edge_id]]
            edge_id = parent[edge_id]
        return edge_id

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    edges = [edge for edge in net.getEdges() if _passenger(edge)]
    for edge in edges:
        parent.setdefault(edge.getID(), edge.getID())
        for lane in edge.getLanes():
            if not lane.allows("passenger"):
                continue
            for connection in lane.getOutgoing():
                other = connection.getTo()
                if _passenger(other):
                    union(edge.getID(), other.getID())

    found: list[OsmContinuation] = []
    joined: set[tuple[str, str]] = set()
    for node in net.getNodes():
        node_ids = junction_node_ids(node.getID())
        osm_ways: set[int] = set()
        for node_id in node_ids:
            osm_ways.update(osm_ways_by_node.get(node_id, set()))
        if not osm_ways:
            continue
        incoming = [edge for edge in node.getIncoming() if _passenger(edge)]
        outgoing = [edge for edge in node.getOutgoing() if _passenger(edge)]
        for source in incoming:
            source_way = osm_way_id(source.getID())
            if source_way not in osm_ways:
                continue
            already = {
                connection.getTo().getID()
                for lane in source.getLanes()
                if lane.allows("passenger")
                for connection in lane.getOutgoing()
            }
            for dest in outgoing:
                if dest.getID() == source.getID() or dest.getID() in already:
                    continue
                dest_way = osm_way_id(dest.getID())
                if dest_way not in osm_ways or find(source.getID()) == find(dest.getID()):
                    continue
                pair = tuple(sorted((find(source.getID()), find(dest.getID()))))
                if pair in joined:
                    continue
                joined.add(pair)
                found.append(
                    OsmContinuation(
                        from_edge=source.getID(),
                        to_edge=dest.getID(),
                        junction=node.getID(),
                        street=source.getName() or "",
                    )
                )
    return found


def component_junction_ids(net) -> list[int]:
    """OpenStreetMap node ids where two drivable pieces already touch."""
    parent: dict[str, str] = {}

    def find(edge_id: str) -> str:
        parent.setdefault(edge_id, edge_id)
        while parent[edge_id] != edge_id:
            parent[edge_id] = parent[parent[edge_id]]
            edge_id = parent[edge_id]
        return edge_id

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    edges = [edge for edge in net.getEdges() if _passenger(edge)]
    for edge in edges:
        parent.setdefault(edge.getID(), edge.getID())
        for lane in edge.getLanes():
            if not lane.allows("passenger"):
                continue
            for connection in lane.getOutgoing():
                other = connection.getTo()
                if _passenger(other):
                    union(edge.getID(), other.getID())
    node_ids: list[int] = []
    for node in net.getNodes():
        members = [edge for edge in list(node.getIncoming()) + list(node.getOutgoing()) if _passenger(edge)]
        if len({find(edge.getID()) for edge in members}) > 1:
            node_ids.extend(junction_node_ids(node.getID()))
    return node_ids


def unresolved_edge_ids(net, osm_ways_by_node: dict[int, set[int]]) -> list[str]:
    """Edges that still lead nowhere even though OpenStreetMap has a continuation."""
    return [item.from_edge for item in required_continuations(net, osm_ways_by_node)]


def _overpass(query: str) -> dict:
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_error = "no Overpass endpoint responded"
    for attempt in range(4):
        for endpoint in OVERPASS_ENDPOINTS:
            request = urllib.request.Request(
                endpoint,
                data=body,
                headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as exc:
                last_error = f"{endpoint}: {exc}"
        time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"OpenStreetMap query failed: {last_error}")
