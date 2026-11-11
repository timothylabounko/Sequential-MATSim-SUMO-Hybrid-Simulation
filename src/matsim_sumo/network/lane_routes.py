"""Routes that respect lane-change bans.

The routers (``duarouter`` and SUMO's rerouting device) choose a sequence of
edges and assume a vehicle can change to any lane on the way. Where the road
markings forbid a lane change (OpenStreetMap ``change:lanes``, imported by
netconvert as ``changeLeft``/``changeRight``), some of those routes cannot be
driven: the vehicle waits at the end of its lane until it is teleported as
"wrong lane". An example is the A1/A4 merge at Opfikon, where the A1/A4 lanes
are separated from the lanes to Zürich-City by a solid line.

A route is drivable if, starting from any lane of its first edge, a vehicle
can follow the route using only the network's connections and the lane changes
allowed for passenger cars. Every route that is not drivable is replaced by the
fastest drivable route between the same edges (free-flow travel times, Dijkstra
on the lane graph).

SUMO's rerouting device has the same blind spot while the vehicle drives. An
edge is a trap if a path of at most ``TRAP_DISTANCE_M`` starts there that the
edge graph allows but the lanes do not. A vehicle whose route uses a trap edge
keeps its (drivable) route and gets no rerouting device; all other vehicles
reroute as usual.
"""

from __future__ import annotations

import heapq
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

VCLASS = "passenger"
LANE_CHANGE_S = 2.0
TRAP_DISTANCE_M = 1000.0
NO_REROUTING = '<param key="has.rerouting.device" value="false"/>'


def _allows(value: str | None) -> bool:
    if value is None:
        return True
    tokens = value.split()
    return VCLASS in tokens or "all" in tokens


class LaneGraph:
    """Lanes of a SUMO network usable by passenger cars, with connections and allowed lane changes."""

    def __init__(self, net_path: Path):
        self.lanes: dict[str, list[str]] = {}
        self.length: dict[str, float] = {}
        self.edge_length: dict[str, float] = {}
        self.change: dict[str, list[str]] = defaultdict(list)
        self.next: dict[str, list[str]] = defaultdict(list)
        usable: set[str] = set()
        for _event, element in ET.iterparse(net_path, events=("end",)):
            if element.tag == "edge" and element.get("function") != "internal":
                lanes = [lane for lane in element.iter("lane")]
                ids = []
                for lane in lanes:
                    allow, disallow = lane.get("allow"), lane.get("disallow")
                    ok = (allow is None or VCLASS in allow.split() or allow == "all") and (
                        disallow is None or VCLASS not in disallow.split()
                    )
                    ids.append(lane.get("id") if ok else None)
                    if ok:
                        usable.add(lane.get("id"))
                        self.length[lane.get("id")] = float(lane.get("length")) / max(1.0, float(lane.get("speed")))
                for index, lane in enumerate(lanes):
                    if ids[index] is None:
                        continue
                    if index + 1 < len(lanes) and ids[index + 1] and _allows(lane.get("changeLeft")):
                        self.change[ids[index]].append(ids[index + 1])
                    if index > 0 and ids[index - 1] and _allows(lane.get("changeRight")):
                        self.change[ids[index]].append(ids[index - 1])
                self.lanes[element.get("id")] = [lane for lane in ids if lane]
                if lanes:
                    self.edge_length[element.get("id")] = float(lanes[0].get("length"))
                element.clear()
            elif element.tag == "connection" and not element.get("from", "").startswith(":"):
                source = f"{element.get('from')}_{element.get('fromLane')}"
                target = f"{element.get('to')}_{element.get('toLane')}"
                self.next[source].append(target)
                element.clear()
        self.next = defaultdict(list, {lane: [t for t in targets if t in usable] for lane, targets in self.next.items() if lane in usable})

    def trap_edges(self, distance: float = TRAP_DISTANCE_M) -> set[str]:
        """Edges from which some edge path shorter than ``distance`` cannot be driven on the lanes."""
        successors: dict[str, set[str]] = defaultdict(set)
        for lane, targets in self.next.items():
            successors[self.edge_of(lane)].update(self.edge_of(t) for t in targets)
        traps = set()
        for edge, lanes in self.lanes.items():
            stack = [(edge, frozenset(self._closure(set(lanes))), 0.0)]
            seen: dict[tuple[str, frozenset], float] = {}
            while stack and edge not in traps:
                current, reachable, driven = stack.pop()
                if driven >= distance or seen.get((current, reachable), distance) <= driven:
                    continue
                seen[(current, reachable)] = driven
                for following in successors.get(current, ()):
                    entered = {t for lane in reachable for t in self.next.get(lane, ()) if self.edge_of(t) == following}
                    if not entered:
                        traps.add(edge)
                        break
                    stack.append((following, frozenset(self._closure(entered)), driven + self.edge_length.get(following, 0.0)))
        return traps

    @staticmethod
    def edge_of(lane: str) -> str:
        return lane.rsplit("_", 1)[0]

    def _closure(self, lanes: set[str]) -> set[str]:
        found, stack = set(lanes), list(lanes)
        while stack:
            for other in self.change.get(stack.pop(), ()):
                if other not in found:
                    found.add(other)
                    stack.append(other)
        return found

    def first_blocked(self, edges: list[str]) -> int | None:
        """Index of the first edge a vehicle cannot reach on its lanes, or None if the route is drivable."""
        reachable = self._closure(set(self.lanes.get(edges[0], ())))
        for index, edge in enumerate(edges[1:], 1):
            reachable = self._closure({t for lane in reachable for t in self.next.get(lane, ()) if self.edge_of(t) == edge})
            if not reachable:
                return index
        return None

    def fastest(self, origin: str, destination: str) -> list[str] | None:
        """Fastest drivable edge sequence from ``origin`` to ``destination``."""
        targets = set(self.lanes.get(destination, ()))
        queue = [(0.0, lane, None) for lane in self.lanes.get(origin, ())]
        heapq.heapify(queue)
        previous: dict[str, str | None] = {}
        while queue:
            cost, lane, before = heapq.heappop(queue)
            if lane in previous:
                continue
            previous[lane] = before
            if lane in targets:
                lanes = [lane]
                while previous[lanes[-1]] is not None:
                    lanes.append(previous[lanes[-1]])
                edges: list[str] = []
                for item in reversed(lanes):
                    if not edges or edges[-1] != self.edge_of(item):
                        edges.append(self.edge_of(item))
                return edges
            for other in self.change.get(lane, ()):
                if other not in previous:
                    heapq.heappush(queue, (cost + LANE_CHANGE_S, other, lane))
            for other in self.next.get(lane, ()):
                if other not in previous:
                    heapq.heappush(queue, (cost + self.length[other], other, lane))
        return None


VEHICLE = re.compile(r'(<vehicle\b[^>]*>)(\s*)<route edges="([^"]*)"(/>|>.*?</route>)', re.DOTALL)


def fix_routes(net_path: Path, routes_path: Path) -> dict:
    """Replace undrivable routes in a duarouter output in place. Returns counts and the blocked transitions."""
    graph = LaneGraph(net_path)
    text = routes_path.read_text(encoding="utf-8")
    traps = graph.trap_edges()
    blocked: dict[tuple[str, str], int] = defaultdict(int)
    stats = {"routes": 0, "undrivable": 0, "rerouted": 0, "fixed_route": 0, "trap_edges": len(traps)}

    def replace(match: re.Match) -> str:
        stats["routes"] += 1
        edges = match.group(3).split()
        index = graph.first_blocked(edges)
        changed = False
        if index is not None:
            stats["undrivable"] += 1
            blocked[(edges[index - 1], edges[index])] += 1
            route = graph.fastest(edges[0], edges[-1])
            if route:
                stats["rerouted"] += 1
                edges, changed = route, True
        if not changed and not traps.intersection(edges):
            return match.group(0)
        stats["fixed_route"] += 1
        return f'{match.group(1)}{match.group(2)}<route edges="{" ".join(edges)}"/>{match.group(2)}{NO_REROUTING}'

    text = VEHICLE.sub(replace, text)
    routes_path.write_text(text, encoding="utf-8")
    stats["blocked"] = [
        {"from": frm, "to": to, "routes": count} for (frm, to), count in sorted(blocked.items(), key=lambda item: -item[1])
    ]
    return stats
