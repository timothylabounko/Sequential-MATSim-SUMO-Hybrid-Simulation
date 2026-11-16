"""Repair the corrected Zürich network against the National morning demand.

Each round has three steps. The network is first compared with OpenStreetMap,
and a link that stops at a junction where OpenStreetMap continues is connected.
The National cars are then simulated, and the stuck approaches are checked
against Google Routes. The evolutionary search tries the edits Google supports
together with lifting lane-change bans near wrong-lane stops, and sends
every individual back to OpenStreetMap.

Every round starts from the best network so far: the one with the fewest
stuck vehicles, then the fewest unroutable trips. A round whose search found
no better network is followed by a wider search on the same network (see
``search_network``'s ``effort``), so consecutive rounds never repeat one
search. Rounds stop when no vehicle is stuck, when ``--patience`` rounds in a
row bring no new lowest stuck count, or after ``--rounds`` searches; one more
simulation then measures the last search. ``final.net.xml`` is the best
network, and the ``final_*`` fields of ``summary.json`` describe its run.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import sumolib

from matsim_sumo.network.google_turn_search import (
    apply_patch,
    google_route_metres,
    gridlock_count,
    inserted_vehicles,
    search_network,
)
from matsim_sumo.network.osm_connectivity import (
    component_junction_ids,
    connection_component_count,
    connection_xml,
    connectivity_problems,
    fetch_highway_ways,
    junction_node_ids,
    lane_pairs,
    missing_component_links,
    nowhere_links,
    passenger_component_count,
    required_continuations,
    unresolved_edge_ids,
)
from matsim_sumo.network.turn_evolution import parse_gridlocks, summarise


def _osm_ways(net, cache: dict, cache_path: Path) -> dict[int, set[int]]:
    node_ids: list[int] = []
    for edge, _exits in nowhere_links(net):
        node_ids.extend(junction_node_ids(edge.getToNode().getID()))
    node_ids.extend(component_junction_ids(net))

    def save() -> None:
        cache_path.write_text(json.dumps(cache), encoding="utf-8")

    ways = fetch_highway_ways(node_ids, cache, on_batch=save)
    save()
    return ways


def osm_review(net, ways: dict[int, set[int]], baseline_components: int) -> dict:
    unresolved = unresolved_edge_ids(net, ways)
    components = passenger_component_count(net)
    drive = connection_component_count(net)
    return {
        "components": components,
        "drive_components": drive,
        "nowhere_links": len(nowhere_links(net)),
        "osm_unresolved": unresolved,
        "problems": connectivity_problems(
            unresolved=unresolved,
            components=components,
            baseline_components=baseline_components,
            drive_components=drive,
        ),
    }


def osm_connect(source: Path, output: Path, cache_path: Path, passes: int = 4) -> dict:
    """Connect links that stop where OpenStreetMap continues. ``source`` is only read."""
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
    current = source
    added: list[dict] = []
    baseline_components = passenger_component_count(sumolib.net.readNet(str(source)))
    for step in range(1, passes + 1):
        net = sumolib.net.readNet(str(current))
        ways = _osm_ways(net, cache, cache_path)
        continuations = required_continuations(net, ways) + missing_component_links(net, ways)
        if not continuations:
            break
        pairs = []
        for item in continuations:
            for from_lane, to_lane in lane_pairs(net.getEdge(item.from_edge), net.getEdge(item.to_edge)):
                pairs.append((item.from_edge, item.to_edge, from_lane, to_lane))
            added.append({"street": item.street, "from": item.from_edge, "to": item.to_edge, "junction": item.junction})
        if not pairs:
            break
        staged = output.with_name(f"{output.stem}.pass{step}.net.xml")
        apply_patch(current, connection_xml(pairs), staged)
        current = staged
        print(f"  osm pass {step}: connected {len(continuations)} links that OpenStreetMap continues", flush=True)
    if current != output:
        shutil.copyfile(current, output)
    final = sumolib.net.readNet(str(output))
    review = osm_review(final, _osm_ways(final, cache, cache_path), baseline_components)
    review["added"] = added
    return review


def describe_gridlocks(net, log_text: str) -> list[dict]:
    """Group stuck vehicles by approach and name the junction they wait at."""
    by_edge: dict[str, Counter] = defaultdict(Counter)
    for item in parse_gridlocks(log_text):
        edge_id = item.edge
        if edge_id.startswith(":"):
            edge_id = edge_id[1:].rsplit("_", 1)[0]
            node = net.getNode(edge_id) if net.hasNode(edge_id) else None
            if node is None or not node.getIncoming():
                continue
            edge_id = node.getIncoming()[0].getID()
        by_edge[edge_id][item.kind] += 1
    rows = []
    for edge_id, kinds in by_edge.items():
        if not net.hasEdge(edge_id):
            continue
        edge = net.getEdge(edge_id)
        node = edge.getToNode()
        street = edge.getName() or f"{(edge.getType() or 'road').replace('highway.', '').replace('_', ' ')} {edge_id}"
        cross = sorted(
            {
                other.getName()
                for other in list(node.getIncoming()) + list(node.getOutgoing())
                if other.getName() and other.getName() != edge.getName()
            }
        )
        lon, lat = net.convertXY2LonLat(*node.getCoord())
        rows.append(
            {
                "approach": street,
                "junction_with": " / ".join(cross) or "(same street)",
                "vehicles": sum(kinds.values()),
                "kinds": ", ".join(f"{kind} {count}" for kind, count in kinds.most_common()),
                "edge": edge_id,
                "junction": node.getID(),
                "lat": round(lat, 6),
                "lon": round(lon, 6),
            }
        )
    rows.sort(key=lambda row: -row["vehicles"])
    return rows


KEY_CHECK = ((47.3779, 8.5403), (47.3769, 8.5417))
NETWORK_ERROR_KINDS = ("wrong lane", "no connection", "collision")


def split_kinds(by_kind: dict[str, int]) -> tuple[int, int]:
    """Stuck vehicles caused by the network layout, and the rest."""
    network = sum(count for kind, count in by_kind.items() if kind in NETWORK_ERROR_KINDS)
    return network, sum(by_kind.values()) - network


def _count_runs(work_dir: Path) -> int:
    return sum(1 for path in work_dir.rglob("*.log") if not path.name.startswith("netconvert"))


def _unroutable(log_path: Path) -> int:
    routing = log_path.with_suffix(".routing.txt")
    return routing.read_text(encoding="utf-8", errors="replace").count("has no valid route") if routing.is_file() else 0


def teleport_share(stuck: int, inserted: int | None) -> float | None:
    """Stuck vehicles as a percentage of the inserted vehicles."""
    return round(100.0 * stuck / inserted, 2) if inserted else None


RUN_SUFFIXES = (".log", ".rou.xml", ".routing.txt", ".stats.xml")
EDIT_FIELDS = ("applied", "lane_bans_lifted", "osm_added")


def ranking(entry: dict) -> tuple[int, int]:
    """Order of two censuses: fewer stuck vehicles first, then fewer unroutable trips."""
    return (entry["stuck"], entry["unroutable"])


def run(
    net: Path,
    routes: Path,
    work_dir: Path,
    api_key: str,
    rounds: int,
    end_s: int,
    route_fn=None,
    teleport_s: int = 120,
    patience: int = 2,
) -> dict:
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_path = work_dir / "osm-nodes.json"
    current = net
    edits = {field: [] for field in EDIT_FIELDS}
    history: list[dict] = []
    best: dict | None = None
    stale = 0
    number = 0
    while True:
        number += 1
        round_dir = work_dir / f"round{number}"
        round_dir.mkdir(parents=True, exist_ok=True)
        if best is not None and current == best["net"]:
            entry = {key: best["entry"][key] for key in ("osm", "score", "stuck", "unroutable", "inserted")}
            entry = {"round": number, **entry, "osm_added": [], "census_of_round": best["round"]}
            print(f"round {number}: network of round {best['round']} unchanged, reusing its simulation", flush=True)
        else:
            print(f"round {number}: OpenStreetMap check", flush=True)
            checked_net = round_dir / "osm.net.xml"
            osm = osm_connect(current, checked_net, cache_path)
            print(f"round {number}: osm {osm['problems'] or 'no link to nowhere, network connected'}", flush=True)
            log_path = round_dir / "census.log"
            count = gridlock_count(checked_net, routes, log_path, end_s, teleport_s, statistics=True)
            unroutable = _unroutable(log_path)
            entry = {
                "round": number,
                "osm": {k: v for k, v in osm.items() if k != "added"},
                "osm_added": osm["added"],
                "score": count,
                "stuck": count - unroutable,
                "unroutable": unroutable,
                "inserted": inserted_vehicles(log_path),
            }
        entry["teleported_percent"] = teleport_share(entry["stuck"], entry["inserted"])
        history.append(entry)
        improved = "census_of_round" not in entry and (best is None or ranking(entry) < ranking(best["entry"]))
        if improved:
            best = {
                "round": number,
                "net": checked_net,
                "log": log_path,
                "entry": entry,
                "edits": dict(edits, osm_added=edits["osm_added"] + entry["osm_added"]),
            }
            stale = 0
        else:
            stale += 1
        entry["new_lowest"] = improved
        entry["best_round"] = best["round"]
        print(
            f"round {number}: {entry['stuck']} stuck vehicles, {entry['unroutable']} unroutable trips; "
            f"lowest so far {best['entry']['stuck']} (round {best['round']}), "
            f"{stale} round(s) without a new lowest",
            flush=True,
        )
        if best["entry"]["stuck"] == 0:
            stopped = "no stuck vehicle"
            break
        if stale >= patience:
            stopped = f"no new lowest stuck count in {patience} rounds in a row"
            break
        if number > rounds:
            stopped = f"stopped after {rounds} rounds"
            break
        for name in ("google-cache.json",):
            shared = work_dir / name
            if shared.is_file():
                shutil.copyfile(shared, round_dir / name)
        if cache_path.is_file():
            shutil.copyfile(cache_path, round_dir / "osm-nodes.json")
        report = search_network(
            best["net"],
            routes,
            best["log"].read_text(encoding="utf-8", errors="replace"),
            round_dir,
            api_key,
            random.Random(42 + number),
            end_s=end_s,
            route_fn=route_fn,
            baseline_score=best["entry"]["score"],
            route_file=best["log"].with_suffix(".rou.xml"),
            teleport_s=teleport_s,
            effort=stale,
        )
        for name in ("google-cache.json", "osm-nodes.json"):
            if (round_dir / name).is_file():
                shutil.copyfile(round_dir / name, work_dir / name)
        entry["searched_round"] = best["round"]
        entry["search_effort"] = stale
        entry["google_disagreements"] = report["google_disagreements"]
        entry["applied"] = report["applied"]
        entry["lane_ban_candidates"] = report["lane_ban_candidates"]
        entry["lane_bans_lifted"] = report["lane_bans_lifted"]
        entry["lane_movements"] = report["lane_movements"]
        entry["best_fitness"] = report["best_fitness"]
        entry["generations"] = report["generations"]
        print(
            f"round {number}: search (effort {stale}) on the network of round {best['round']} kept "
            f"{len(report['applied'])} turn edits and lifted {len(report['lane_bans_lifted'])} lane-change bans, "
            f"best {report['best_fitness']}",
            flush=True,
        )
        if (report["applied"] or report["lane_bans_lifted"]) and report["best_fitness"] < best["entry"]["score"]:
            current = Path(report["best_network"])
            edits = dict(
                best["edits"],
                applied=best["edits"]["applied"] + report["applied"],
                lane_bans_lifted=best["edits"]["lane_bans_lifted"] + report["lane_bans_lifted"],
            )
        else:
            current = best["net"]
            edits = best["edits"]

    final_net = work_dir / "final.net.xml"
    final_log = work_dir / "final.log"
    shutil.copyfile(best["net"], final_net)
    for suffix in RUN_SUFFIXES:
        if best["log"].with_suffix(suffix).is_file():
            shutil.copyfile(best["log"].with_suffix(suffix), final_log.with_suffix(suffix))
    print(f"final: the network of round {best['round']}, the lowest stuck count, with its simulation", flush=True)
    final = best["entry"]
    net_obj = sumolib.net.readNet(str(final_net))
    rows = describe_gridlocks(net_obj, final_log.read_text(encoding="utf-8", errors="replace"))
    with (work_dir / "gridlocks.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["approach"])
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "stopped": stopped,
        "best_round": best["round"],
        "patience": patience,
        "max_rounds": rounds,
        "rounds": history,
        "sumo_runs": _count_runs(work_dir),
        "final_network": str(final_net),
        "final_osm": final["osm"],
        "final_score": final["score"],
        "final_stuck": final["stuck"],
        "final_unroutable": final["unroutable"],
        "final_inserted": final["inserted"],
        "final_teleported_percent": final["teleported_percent"],
        "teleport_after_s": teleport_s,
        "final_gridlocks": summarise(parse_gridlocks(final_log.read_text(encoding="utf-8", errors="replace"))),
        **best["edits"],
    }
    (work_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return {"summary": summary, "gridlocks": rows}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--net", type=Path, required=True, help="Corrected SUMO network (read only)")
    parser.add_argument("--routes", type=Path, required=True, help="National SUMO trips or routes")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=10, help="At most this many searches (safety cap)")
    parser.add_argument(
        "--patience",
        type=int,
        default=2,
        help="Stop after this many rounds in a row without a new lowest stuck count",
    )
    parser.add_argument("--end", type=int, default=3600, help="Simulation end in seconds")
    parser.add_argument("--teleport", type=int, default=120, help="Seconds without moving before a vehicle counts as stuck")
    parser.add_argument("--top", type=int, default=40, help="Stuck approaches to print")
    args = parser.parse_args(argv)
    if args.patience < 1:
        parser.error("--patience must be at least 1")
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GOOGLE_MAPS_API_KEY is not set")
    try:
        google_route_metres(*KEY_CHECK, api_key)
    except (RuntimeError, OSError) as exc:
        raise SystemExit(f"Google rejected GOOGLE_MAPS_API_KEY: {exc}") from exc
    result = run(
        args.net, args.routes, args.work_dir, api_key, args.rounds, args.end,
        teleport_s=args.teleport, patience=args.patience,
    )
    summary = result["summary"]
    print()
    print(f"stopped: {summary['stopped']}")
    print(f"final network: round {summary['best_round']} of {len(summary['rounds'])}, the lowest stuck count")
    print(f"SUMO runs: {summary['sumo_runs']}")
    print(f"turn edits kept: {len(summary['applied'])}")
    for edit in summary["applied"]:
        print(f"  {edit['action']} {edit['street']} {edit['from']} -> {edit['to']}")
    print(f"lane-change bans lifted: {len(summary['lane_bans_lifted'])}")
    for group in summary["lane_bans_lifted"]:
        print(f"  {' '.join(group)}")
    print(f"links connected from OpenStreetMap: {len(summary['osm_added'])}")
    print(f"final OpenStreetMap check: {summary['final_osm']['problems'] or 'no link to nowhere, network connected'}")
    print(f"stuck vehicles left: {summary['final_stuck']} {summary['final_gridlocks']['by_kind']}")
    print(f"unroutable trips left: {summary['final_unroutable']}")
    print(
        f"teleported share: {summary['final_teleported_percent']} % of {summary['final_inserted']} inserted vehicles "
        f"(stuck = not moved for {args.teleport} s)"
    )
    network, capacity = split_kinds(summary["final_gridlocks"]["by_kind"])
    print(f"  network errors (wrong lane, no connection): {network}")
    print(f"  capacity (jam, yield) and other: {capacity}")
    print()
    print(f"vehicles  approach -> junction with  (what happened), {args.top} worst; all in gridlocks.csv")
    for row in result["gridlocks"][: args.top]:
        print(f"{row['vehicles']:8d}  {row['approach']} -> {row['junction_with']}  ({row['kinds']})")


if __name__ == "__main__":
    main()
