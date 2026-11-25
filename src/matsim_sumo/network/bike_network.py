"""Build a SUMO bicycle network from the official reference lines.

Nodes closer than the weld distance become one junction. Each open direction
of a reference link becomes one edge that only a bicycle may use. A turn in
``NETZ_VELO_ABBIEGEVERBOTE`` is left out of the connection list. Coordinates
are UTM zone 32, the same frame ``plans_to_sumo`` uses to snap MATSim links.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from matsim_sumo.network.bike_reference import BikeLink, BikeReference, TurnBan, lv95_to_sumo

WELD_M = 1.0


@dataclass(frozen=True)
class BikeEdge:
    id: str
    from_node: str
    to_node: str
    name: str
    speed: float
    priority: int
    source: str
    fid: str
    length: float
    attract: float
    shape: tuple[tuple[float, float], ...]


@dataclass
class BikeGraph:
    edges: list[BikeEdge] = field(default_factory=list)
    banned: set[tuple[str, str]] = field(default_factory=set)
    nodes: dict[str, tuple[float, float]] = field(default_factory=dict)
    dropped_bans: int = 0

    def edge_map(self) -> dict[str, BikeEdge]:
        return {edge.id: edge for edge in self.edges}


def _node_id(x: float, y: float, weld: float = WELD_M) -> tuple[str, float, float]:
    gx, gy = round(x / weld), round(y / weld)
    return f"n{int(gx)}_{int(gy)}", gx * weld, gy * weld


def _length(shape: list[tuple[float, float]]) -> float:
    total = 0.0
    for (ax, ay), (bx, by) in zip(shape, shape[1:]):
        total += ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
    return total


def _priority(link: BikeLink) -> int:
    if link.cycleway:
        return 3
    if link.bike_lane:
        return 2
    return 1


def _shape(part: tuple[tuple[float, float], ...], reverse: bool) -> list[tuple[float, float]]:
    coords = [lv95_to_sumo(x, y) for x, y in part]
    if reverse:
        coords.reverse()
    return coords


def is_reverse(a: str, b: str) -> bool:
    return a == f"-{b}" or b == f"-{a}"


def build_graph(reference: BikeReference, weld: float = WELD_M) -> BikeGraph:
    """Weld the reference into directed bicycle edges and apply turn bans."""
    graph = BikeGraph()
    by_fid: dict[str, list[BikeEdge]] = {}
    ends: dict[str, dict] = {}
    for link in reference.links:
        made: list[BikeEdge] = []
        for part_index, part in enumerate(link.parts):
            for reverse, speed, attract, enabled in (
                (False, link.speed_forward, link.attract_forward, link.forward),
                (True, link.speed_backward, link.attract_backward, link.backward),
            ):
                if not enabled:
                    continue
                shape = _shape(part, reverse)
                if _length(shape) < weld:
                    continue
                from_id, fx, fy = _node_id(*shape[0], weld)
                to_id, tx, ty = _node_id(*shape[-1], weld)
                if from_id == to_id:
                    continue
                graph.nodes[from_id] = (fx, fy)
                graph.nodes[to_id] = (tx, ty)
                shape[0] = (fx, fy)
                shape[-1] = (tx, ty)
                suffix = f"_{part_index}" if len(link.parts) > 1 else ""
                base = f"v{link.fid}{suffix}"
                edge_id = f"-{base}" if reverse else base
                edge = BikeEdge(
                    id=edge_id,
                    from_node=from_id,
                    to_node=to_id,
                    name=link.name,
                    speed=speed,
                    priority=_priority(link),
                    source=link.source,
                    fid=link.fid,
                    length=_length(shape),
                    attract=attract,
                    shape=tuple(shape),
                )
                graph.edges.append(edge)
                made.append(edge)
        by_fid.setdefault(link.fid, []).extend(made)
        forwards = [edge for edge in made if not edge.id.startswith("-")]
        backwards = [edge for edge in made if edge.id.startswith("-")]
        if forwards or backwards:
            ends[link.fid] = {
                "forward_into_end": forwards[-1] if forwards else None,
                "backward_into_start": backwards[0] if backwards else None,
            }
    graph.banned, graph.dropped_bans = _banned_pairs(reference.bans, by_fid, ends)
    return graph


def _banned_pairs(
    bans: list[TurnBan],
    by_fid: dict[str, list[BikeEdge]],
    ends: dict[str, dict],
) -> tuple[set[tuple[str, str]], int]:
    banned: set[tuple[str, str]] = set()
    dropped = 0
    for ban in bans:
        info = ends.get(ban.from_fid)
        approach = None if info is None else (info["forward_into_end"] if ban.at_end else info["backward_into_start"])
        if approach is None:
            dropped += 1
            continue
        leaves = [edge for edge in by_fid.get(ban.to_fid, ()) if edge.from_node == approach.to_node]
        if not leaves:
            dropped += 1
            continue
        for edge in leaves:
            if not is_reverse(approach.id, edge.id):
                banned.add((approach.id, edge.id))
    return banned, dropped


def successors(edges: list[BikeEdge], banned: set[tuple[str, str]] | None = None) -> dict[str, set[str]]:
    """Directed bicycle successors, without U-turns and without banned turns."""
    banned = banned or set()
    leaving: dict[str, list[str]] = {}
    for edge in edges:
        leaving.setdefault(edge.from_node, []).append(edge.id)
    result: dict[str, set[str]] = {}
    for edge in edges:
        result[edge.id] = {
            other
            for other in leaving.get(edge.to_node, ())
            if not is_reverse(edge.id, other) and (edge.id, other) not in banned
        }
    return result


def connections(graph: BikeGraph) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for origin, dests in successors(graph.edges, graph.banned).items():
        for dest in sorted(dests):
            pairs.append((origin, dest))
    return pairs


def _shape_text(shape: tuple[tuple[float, float], ...]) -> str:
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in shape)


def write_plain(graph: BikeGraph, directory: Path) -> dict[str, Path]:
    """Write the node, edge and connection files netconvert reads."""
    directory.mkdir(parents=True, exist_ok=True)
    nodes = ET.Element("nodes")
    for node_id, (x, y) in sorted(graph.nodes.items()):
        ET.SubElement(nodes, "node", id=node_id, x=f"{x:.2f}", y=f"{y:.2f}", type="priority")
    edges = ET.Element("edges")
    for edge in graph.edges:
        element = ET.SubElement(
            edges,
            "edge",
            id=edge.id,
            **{"from": edge.from_node, "to": edge.to_node},
            priority=str(edge.priority),
            numLanes="1",
            speed=f"{edge.speed:.2f}",
            allow="bicycle",
            name=edge.name,
            shape=_shape_text(edge.shape),
        )
        ET.SubElement(element, "param", key="source", value=edge.source)
        ET.SubElement(element, "param", key="attractiveness", value=f"{edge.attract:.0f}")
    cons = ET.Element("connections")
    for origin, dest in connections(graph):
        ET.SubElement(cons, "connection", **{"from": origin, "to": dest})
    paths = {
        "nodes": directory / "bike.nod.xml",
        "edges": directory / "bike.edg.xml",
        "connections": directory / "bike.con.xml",
    }
    ET.ElementTree(nodes).write(paths["nodes"], encoding="utf-8", xml_declaration=True)
    ET.ElementTree(edges).write(paths["edges"], encoding="utf-8", xml_declaration=True)
    ET.ElementTree(cons).write(paths["connections"], encoding="utf-8", xml_declaration=True)
    return paths
