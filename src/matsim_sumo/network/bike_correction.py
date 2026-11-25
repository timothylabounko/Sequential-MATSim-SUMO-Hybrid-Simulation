"""Correct the bicycle network the way the car search corrects turns.

The car search asks Google which turns to try. This search does not. The
official graph is already the network, including its one-ways and turn bans.
What remains is a gap: two reference lines that should meet but whose ends
are a few metres apart, so a bicycle trip cannot cross. Each such gap is a
gene. A chromosome is the set of short connectors to add.

The score counts a trip with no path ten times and charges a small penalty
per metre of connector, the same weights as a network error and an edit in
the car search. The population, the generations and the patience follow that
search: 8 + 4 × effort chromosomes, 6 + 2 × effort generations, and a wider
gap after a round that did not improve. Rounds stop when every trip has a
path, when ``patience`` rounds bring no new lowest score, or after ``rounds``.

A table of simulated edge volumes adds the GEH against the bicycle counters.
That term is zero until a simulation has been run; it does not invent counts.
"""

from __future__ import annotations

import json
import random
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from matsim_sumo.network.bike_counts import geh
from matsim_sumo.network.bike_network import BikeEdge, BikeGraph, successors
from matsim_sumo.network.bike_reference import DEFAULT_SPEED_MPS, lv95_to_sumo

NETWORK_WEIGHT = 10.0
LENGTH_PENALTY = 0.001
MIN_GAP_M = 1.5
BASE_GAP_M = 8.0


@dataclass(frozen=True)
class Connector:
    id: str
    from_node: str
    to_node: str
    length: float

    def edges(self) -> tuple[BikeEdge, BikeEdge]:
        forward = _connector_edge(self.id, self.from_node, self.to_node, self.length)
        back = _connector_edge(f"-{self.id}", self.to_node, self.from_node, self.length)
        return forward, back


def _connector_edge(edge_id: str, origin: str, dest: str, length: float) -> BikeEdge:
    return BikeEdge(
        id=edge_id,
        from_node=origin,
        to_node=dest,
        name="connector",
        speed=DEFAULT_SPEED_MPS,
        priority=1,
        source="connector",
        fid=edge_id,
        length=length,
        attract=1.0,
        shape=(),
    )


def dead_ends(graph: BikeGraph) -> list[str]:
    """Nodes with only one neighbouring node: a line that stops short of the next."""
    neighbours: dict[str, set[str]] = {}
    for edge in graph.edges:
        neighbours.setdefault(edge.from_node, set()).add(edge.to_node)
        neighbours.setdefault(edge.to_node, set()).add(edge.from_node)
    return sorted(node for node, others in neighbours.items() if len(others) <= 1)


def candidate_connectors(graph: BikeGraph, min_gap: float = MIN_GAP_M, max_gap: float = BASE_GAP_M) -> list[Connector]:
    """Both-direction links between dead ends that the weld distance left apart."""
    ends = dead_ends(graph)
    found: list[Connector] = []
    seen: set[tuple[str, str]] = set()
    for index, origin in enumerate(ends):
        ax, ay = graph.nodes[origin]
        for dest in ends[index + 1 :]:
            bx, by = graph.nodes[dest]
            length = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
            if not min_gap < length <= max_gap:
                continue
            pair = tuple(sorted((origin, dest)))
            if pair in seen:
                continue
            seen.add(pair)
            found.append(Connector(f"c_{pair[0]}_{pair[1]}", pair[0], pair[1], length))
    return found


def read_trips(path: Path) -> list[tuple[str, str]]:
    """Origin and destination edge of every ``<trip>`` in a SUMO routes file."""
    trips: list[tuple[str, str]] = []
    for _event, element in ET.iterparse(path, events=("end",)):
        if element.tag == "trip" and element.get("from") and element.get("to"):
            trips.append((element.get("from"), element.get("to")))
        element.clear()
    return trips


def unroutable(trips: list[tuple[str, str]], succ: dict[str, set[str]]) -> int:
    """Trips whose destination cannot be reached from their origin."""
    reach: dict[str, set[str]] = {}

    def can_reach(origin: str) -> set[str]:
        if origin not in reach:
            seen: set[str] = set()
            stack = [origin]
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                stack.extend(succ.get(current, ()))
            reach[origin] = seen
        return reach[origin]

    missed = 0
    for origin, dest in trips:
        if origin not in succ or dest not in can_reach(origin):
            missed += 1
    return missed


def count_geh(graph: BikeGraph, sites: list[dict], volumes: dict[str, float], radius: float = 40.0) -> float:
    """Sum of GEH at counters whose nearest bicycle edge has a simulated volume.

    ``sites`` use LV95 ``east`` and ``north``. ``volumes`` maps a SUMO edge id
    to the bicycles that entered it. A counter with no simulated edge is left
    out, the same way an unmatched car cross-section is left out.
    """
    if not volumes:
        return 0.0
    total = 0.0
    matched = 0
    for site in sites:
        x, y = lv95_to_sumo(site["east"], site["north"])
        edge = _nearest(graph, x, y, radius)
        if edge is None or edge not in volumes:
            continue
        opposite = volumes.get(f"-{edge}", 0.0) if not edge.startswith("-") else volumes.get(edge[1:], 0.0)
        simulated = volumes[edge] + (opposite or 0.0)
        total += geh(simulated, float(site["count"]))
        matched += 1
    return total if matched else 0.0


def _nearest(graph: BikeGraph, x: float, y: float, radius: float) -> str | None:
    best, best_d = None, radius
    for edge in graph.edges:
        if edge.id.startswith("-") or not edge.shape:
            continue
        for (px, py), (qx, qy) in zip(edge.shape, edge.shape[1:]):
            dx, dy = qx - px, qy - py
            seg = dx * dx + dy * dy
            t = 0.0 if seg == 0 else max(0.0, min(1.0, ((x - px) * dx + (y - py) * dy) / seg))
            distance = ((x - px - t * dx) ** 2 + (y - py - t * dy) ** 2) ** 0.5
            if distance < best_d:
                best, best_d = edge.id, distance
    return best


def apply_connectors(graph: BikeGraph, chosen: list[Connector]) -> BikeGraph:
    """Return a new graph with both directions of each chosen connector."""
    edges = list(graph.edges)
    nodes = dict(graph.nodes)
    for connector in chosen:
        edges.extend(connector.edges())
    return BikeGraph(edges=edges, banned=set(graph.banned), nodes=nodes, dropped_bans=graph.dropped_bans)


def score(
    graph: BikeGraph,
    trips: list[tuple[str, str]],
    chosen: list[Connector],
    sites: list[dict] | None = None,
    volumes: dict[str, float] | None = None,
) -> float:
    edited = apply_connectors(graph, chosen)
    missed = unroutable(trips, successors(edited.edges, edited.banned))
    length = sum(connector.length for connector in chosen)
    agreement = count_geh(edited, sites or [], volumes or {})
    return NETWORK_WEIGHT * missed + LENGTH_PENALTY * length + agreement


def _population(rng: random.Random, genes: int, size: int) -> list[tuple[int, ...]]:
    chromosomes = [tuple(0 for _ in range(genes))]
    while len(chromosomes) < size:
        chromosomes.append(tuple(1 if rng.random() < 0.3 else 0 for _ in range(genes)))
    return chromosomes


def evolve(
    graph: BikeGraph,
    trips: list[tuple[str, str]],
    candidates: list[Connector],
    effort: int = 0,
    seed: int = 1,
    sites: list[dict] | None = None,
    volumes: dict[str, float] | None = None,
) -> tuple[list[Connector], float]:
    """One evolutionary search. The empty edit is always in the population."""
    if not candidates:
        return [], score(graph, trips, [], sites, volumes)
    rng = random.Random(seed + effort)
    size = 8 + 4 * effort
    generations = 6 + 2 * effort
    population = _population(rng, len(candidates), size)

    def fitted(chromosome: tuple[int, ...]) -> float:
        chosen = [candidates[index] for index, bit in enumerate(chromosome) if bit]
        return score(graph, trips, chosen, sites, volumes)

    fitness = [fitted(chromosome) for chromosome in population]
    for _generation in range(generations):
        ranked = sorted(range(len(population)), key=lambda index: fitness[index])
        nxt = [population[ranked[0]]]
        while len(nxt) < size:
            left = population[min(rng.randrange(size), rng.randrange(size))]
            right = population[min(rng.randrange(size), rng.randrange(size))]
            cut = rng.randrange(len(candidates))
            child = list(left[:cut] + right[cut:])
            gene = rng.randrange(len(candidates))
            child[gene] = 1 - child[gene]
            nxt.append(tuple(child))
        population = nxt
        fitness = [fitted(chromosome) for chromosome in population]
    best = min(range(len(population)), key=lambda index: fitness[index])
    chosen = [candidates[index] for index, bit in enumerate(population[best]) if bit]
    return chosen, fitness[best]


def repair(
    graph: BikeGraph,
    trips: list[tuple[str, str]],
    rounds: int = 30,
    patience: int = 3,
    max_effort: int = 2,
    sites: list[dict] | None = None,
    volumes: dict[str, float] | None = None,
) -> dict:
    """Repeat the search from the best graph, widening the gap after a miss."""
    current = graph
    best_score = score(current, trips, [], sites, volumes)
    stale = 0
    effort = 0
    added: list[dict] = []
    history = [{"round": 0, "score": best_score, "connectors": 0, "effort": 0}]
    stopped = "reached the round limit"
    for round_index in range(1, rounds + 1):
        gap = BASE_GAP_M * (1 + effort)
        candidates = candidate_connectors(current, MIN_GAP_M, gap)
        chosen, chosen_score = evolve(current, trips, candidates, effort=effort, seed=round_index, sites=sites, volumes=volumes)
        improved = chosen_score < best_score - 1e-9
        history.append({
            "round": round_index,
            "score": chosen_score,
            "connectors": len(chosen),
            "candidates": len(candidates),
            "gap_m": gap,
            "effort": effort,
            "kept": improved,
        })
        if improved:
            current = apply_connectors(current, chosen)
            best_score = chosen_score
            stale = 0
            effort = 0
            added.extend({"id": item.id, "from": item.from_node, "to": item.to_node, "length_m": round(item.length, 2)} for item in chosen)
        else:
            stale += 1
            effort = min(max_effort, effort + 1)
        if unroutable(trips, successors(current.edges, current.banned)) == 0 and not candidate_connectors(current, MIN_GAP_M, BASE_GAP_M * (1 + max_effort)):
            stopped = "every trip has a path"
            break
        if patience and stale >= patience:
            stopped = f"no new lowest score in {patience} rounds"
            break
    return {
        "stopped": stopped,
        "score": best_score,
        "unroutable": unroutable(trips, successors(current.edges, current.banned)),
        "connectors": added,
        "edges": len(current.edges),
        "rounds": history,
        "graph": current,
    }
