"""Repair the corrected Zürich network against the National morning demand.

Each round has three steps. The network is first compared with OpenStreetMap,
and a link that stops at a junction where OpenStreetMap continues is connected.
The National cars are then simulated, and the stuck approaches are checked
against Google Routes. The evolutionary search tries the edits Google supports
together with lifting lane-change bans near wrong-lane stops, and sends
every individual back to OpenStreetMap.

Network errors (wrong lane, no connection, collision) and trips without a
route come first: the score counts each of them ``--network-weight`` times
(default 10) and every other stuck vehicle once, and the search asks Google
about the approaches with a network error before the others.

Every round starts from the best network so far: the one with the lowest
score, then the fewest stuck vehicles. A round whose search found no better
network is followed by a wider search on the same network (see
``search_network``'s ``effort``), so consecutive rounds never repeat one
search. The width grows up to ``--max-effort`` and then stays there, with a
new random seed every round. Rounds stop when no vehicle is stuck, when
``--patience`` rounds in a row bring no new lowest score (``0``: never,
iterate until no vehicle is stuck), or after ``--rounds`` searches; one more
simulation then measures the last search. ``final.net.xml`` is the best
network, and the ``final_*`` fields of ``summary.json`` describe its run.

``--workers`` candidate networks are simulated at the same time, and a
candidate's simulation stops as soon as its score is above the network it
was built from. ``--signals`` (with ``--signals-net``, the network they were
written for) and ``--behaviour`` (a driver-behaviour ``summary.json``) run
every simulation with the signals and drivers of the level below; the
signals are carried onto each edited network. ``--google-cache`` and
``--osm-cache`` are shared by all levels.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import shutil
import xml.etree.ElementTree as ET
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
from matsim_sumo.network.turn_evolution import NETWORK_ERROR_KINDS, parse_gridlocks, summarise


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
CONNECTION_TAG = re.compile(r"<connection\b[^>]*>")
ATTRIBUTE = re.compile(r'(\w+)="([^"]*)"')


def signal_links_of(net_path: Path) -> dict[str, dict[int, tuple]]:
    """The links of every signal of a network file, read from its connections (as sumolib's ``getConnections``)."""
    links: dict[str, dict[int, tuple]] = {}
    for match in CONNECTION_TAG.finditer(net_path.read_text(encoding="utf-8", errors="replace")):
        attributes = dict(ATTRIBUTE.findall(match.group(0)))
        if "tl" not in attributes or "linkIndex" not in attributes or attributes["from"].startswith(":"):
            continue
        links.setdefault(attributes["tl"], {})[int(attributes["linkIndex"])] = (
            attributes["from"],
            int(attributes["fromLane"]),
            attributes["to"],
            int(attributes["toLane"]),
        )
    return links


def carry_signals_onto(base_links: dict, signals: Path, net_path: Path, output: Path) -> Path:
    """``signals`` for the edited network ``net_path``; ``signals`` itself when no signal's links changed.

    As in the intersection stage, a program is renumbered for moved links, and a signal with a link
    that has no counterpart keeps the network's own program.
    """
    from matsim_sumo.network.zero_teleport_repair import carried_state, link_matches

    new_links = signal_links_of(net_path)
    if new_links == base_links:
        return signals
    tree = ET.parse(signals)
    root = tree.getroot()
    for logic in list(root.iter("tlLogic")):
        tls_id = logic.get("id")
        old, new = base_links.get(tls_id, {}), new_links.get(tls_id)
        if new is None:
            root.remove(logic)
            continue
        if old == new:
            continue
        matches = link_matches(old, new)
        if matches is None:
            root.remove(logic)
            continue
        for phase in logic.findall("phase"):
            phase.set("state", carried_state(phase.get("state", ""), matches))
    tree.write(output, encoding="UTF-8", xml_declaration=True)
    return output


def driver_setup(behaviour_summary: Path, routes: Path, output: Path) -> tuple[Path, list[str]]:
    """The trips with the best drivers of a behaviour round, and that round's SUMO options."""
    from matsim_sumo.network.behaviour_calibration import individual_of, sumo_options, with_behaviour

    summary = json.loads(behaviour_summary.read_text(encoding="utf-8"))
    individual = individual_of(summary["best"]["genes"])
    output.write_text(with_behaviour(routes.read_text(encoding="utf-8"), individual), encoding="utf-8")
    return output, sumo_options(individual)


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


def ranking(entry: dict) -> tuple[float, int, int]:
    """Order of two censuses: lower score (network errors weighted) first, then fewer stuck vehicles and unroutable trips."""
    return (entry["score"], entry["stuck"], entry["unroutable"])


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
    max_effort: int = 3,
    workers: int = 1,
    network_weight: float = 10.0,
    google_cache: Path | None = None,
    osm_cache: Path | None = None,
    signals: Path | None = None,
    signals_net: Path | None = None,
    behaviour: Path | None = None,
    early_stop: bool = True,
    fallback: Path | None = None,
) -> dict:
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_path = osm_cache or work_dir / "osm-nodes.json"
    google_cache = google_cache or work_dir / "google-cache.json"
    driver_options: list[str] = []
    if behaviour is not None:
        routes, driver_options = driver_setup(behaviour, routes, work_dir / "trips.behaviour.xml")
        print(f"drivers of {behaviour}: {' '.join(driver_options) or 'vehicle types only'}", flush=True)
    base_links = signal_links_of(signals_net) if signals is not None else {}
    if signals is not None:
        print(f"signals of {signals} ({len(base_links)} signals), carried onto every edited network", flush=True)

    def options_for(net_path: Path, signals_out: Path) -> list[str]:
        options = list(driver_options)
        if signals is not None:
            options += ["--additional-files", str(carry_signals_onto(base_links, signals, net_path, signals_out))]
        return options

    def census(number: int, round_dir: Path, source: Path) -> tuple[dict, Path, Path]:
        checked_net = round_dir / "osm.net.xml"
        osm = osm_connect(source, checked_net, cache_path)
        print(f"round {number}: osm {osm['problems'] or 'no link to nowhere, network connected'}", flush=True)
        log_path = round_dir / "census.log"
        score = gridlock_count(
            checked_net,
            routes,
            log_path,
            end_s,
            teleport_s,
            sumo_options=tuple(options_for(checked_net, round_dir / "census.signals.add.xml")),
            statistics=True,
            network_weight=network_weight,
        )
        gridlocks = parse_gridlocks(log_path.read_text(encoding="utf-8", errors="replace"))
        network, _capacity = split_kinds(summarise(gridlocks)["by_kind"])
        entry = {
            "round": number,
            "osm": {k: v for k, v in osm.items() if k != "added"},
            "osm_added": osm["added"],
            "score": score,
            "stuck": len(gridlocks),
            "network_errors": network,
            "unroutable": _unroutable(log_path),
            "inserted": inserted_vehicles(log_path),
        }
        return entry, checked_net, log_path

    current = net
    edits = {field: [] for field in EDIT_FIELDS}
    history: list[dict] = []
    best: dict | None = None
    stale = 0
    number = 0
    if fallback is not None:
        round_dir = work_dir / "round0"
        round_dir.mkdir(parents=True, exist_ok=True)
        print(f"round 0: {fallback}, the start without the carried edits, for comparison", flush=True)
        entry, checked_net, log_path = census(0, round_dir, fallback)
        entry["teleported_percent"] = teleport_share(entry["stuck"], entry["inserted"])
        entry["without_carried_edits"] = True
        entry["new_lowest"] = True
        entry["best_round"] = 0
        history.append(entry)
        best = {"round": 0, "net": checked_net, "log": log_path, "entry": entry, "edits": dict(edits, osm_added=entry["osm_added"])}
        print(
            f"round 0: {entry['stuck']} stuck vehicles ({entry['network_errors']} network errors), "
            f"{entry['unroutable']} unroutable trips, score {entry['score']:g}",
            flush=True,
        )
    while True:
        number += 1
        round_dir = work_dir / f"round{number}"
        round_dir.mkdir(parents=True, exist_ok=True)
        if best is not None and current == best["net"]:
            entry = {key: best["entry"][key] for key in ("osm", "score", "stuck", "network_errors", "unroutable", "inserted")}
            entry = {"round": number, **entry, "osm_added": [], "census_of_round": best["round"]}
            print(f"round {number}: network of round {best['round']} unchanged, reusing its simulation", flush=True)
        else:
            print(f"round {number}: OpenStreetMap check", flush=True)
            entry, checked_net, log_path = census(number, round_dir, current)
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
        elif not (fallback is not None and number == 1):
            stale += 1
        entry["new_lowest"] = improved
        entry["best_round"] = best["round"]
        print(
            f"round {number}: {entry['stuck']} stuck vehicles ({entry['network_errors']} network errors), "
            f"{entry['unroutable']} unroutable trips, score {entry['score']:g}; "
            f"lowest so far {best['entry']['stuck']} stuck, score {best['entry']['score']:g} (round {best['round']}), "
            f"{stale} round(s) without a new lowest",
            flush=True,
        )
        if best["entry"]["stuck"] == 0:
            stopped = "no stuck vehicle"
            break
        if patience and stale >= patience:
            stopped = f"no new lowest score in {patience} rounds in a row"
            break
        if number > rounds:
            stopped = f"stopped after {rounds} rounds"
            break
        if cache_path.is_file():
            shutil.copyfile(cache_path, round_dir / "osm-nodes.json")
        search_options = lambda net_path, label, folder=round_dir: options_for(net_path, folder / f"{label}.signals.add.xml")
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
            effort=min(stale, max_effort),
            workers=workers,
            network_weight=network_weight,
            options_for=search_options if (signals is not None or driver_options) else None,
            google_cache=google_cache,
            early_stop=early_stop,
        )
        if (round_dir / "osm-nodes.json").is_file():
            shutil.copyfile(round_dir / "osm-nodes.json", cache_path)
        entry["searched_round"] = best["round"]
        entry["search_effort"] = min(stale, max_effort)
        entry["google_disagreements"] = report["google_disagreements"]
        entry["applied"] = report["applied"]
        entry["lane_ban_candidates"] = report["lane_ban_candidates"]
        entry["lane_bans_lifted"] = report["lane_bans_lifted"]
        entry["lane_movements"] = report["lane_movements"]
        entry["best_fitness"] = report["best_fitness"]
        entry["generations"] = report["generations"]
        print(
            f"round {number}: search (effort {min(stale, max_effort)}) on the network of round {best['round']} kept "
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
        "network_weight": network_weight,
        "workers": workers,
        "start_network": str(net),
        "signals": str(signals) if signals is not None else None,
        "behaviour": str(behaviour) if behaviour is not None else None,
        "rounds": history,
        "sumo_runs": _count_runs(work_dir),
        "final_network": str(final_net),
        "final_osm": final["osm"],
        "final_score": final["score"],
        "final_stuck": final["stuck"],
        "final_network_errors": final.get("network_errors"),
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
        help="Stop after this many rounds in a row without a new lowest score (0: until none is stuck)",
    )
    parser.add_argument("--max-effort", type=int, default=3, help="Widest search after rounds without a new lowest")
    parser.add_argument("--workers", type=int, default=1, help="Candidate networks simulated at the same time")
    parser.add_argument(
        "--network-weight",
        type=float,
        default=10.0,
        help="Score of one network error (wrong lane, no connection, collision) or unroutable trip; other stuck vehicles count 1",
    )
    parser.add_argument("--no-early-stop", action="store_true", help="Simulate every candidate to the end")
    parser.add_argument("--google-cache", type=Path, help="Google turn answers shared by all levels (default: in --work-dir)")
    parser.add_argument("--osm-cache", type=Path, help="OpenStreetMap ways shared by all levels (default: in --work-dir)")
    parser.add_argument("--signals", type=Path, help="Signal programs (signals.add.xml) of the level below")
    parser.add_argument("--signals-net", type=Path, help="The network --signals was written for")
    parser.add_argument("--behaviour", type=Path, help="summary.json of a driver-behaviour round: its best drivers")
    parser.add_argument(
        "--fallback-net",
        type=Path,
        help="--net without the edits carried from an earlier run; simulated as round 0, the better one is kept",
    )
    parser.add_argument("--end", type=int, default=3600, help="Simulation end in seconds")
    parser.add_argument("--teleport", type=int, default=120, help="Seconds without moving before a vehicle counts as stuck")
    parser.add_argument("--top", type=int, default=40, help="Stuck approaches to print")
    args = parser.parse_args(argv)
    if args.patience < 0:
        parser.error("--patience must be 0 or more")
    if args.workers < 1:
        parser.error("--workers must be 1 or more")
    if args.signals is not None and args.signals_net is None:
        parser.error("--signals needs --signals-net")
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GOOGLE_MAPS_API_KEY is not set")
    try:
        google_route_metres(*KEY_CHECK, api_key)
    except (RuntimeError, OSError) as exc:
        raise SystemExit(f"Google rejected GOOGLE_MAPS_API_KEY: {exc}") from exc
    result = run(
        args.net, args.routes, args.work_dir, api_key, args.rounds, args.end,
        teleport_s=args.teleport, patience=args.patience, max_effort=args.max_effort,
        workers=args.workers, network_weight=args.network_weight, google_cache=args.google_cache,
        osm_cache=args.osm_cache, signals=args.signals, signals_net=args.signals_net,
        behaviour=args.behaviour, early_stop=not args.no_early_stop, fallback=args.fallback_net,
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
    print(f"  network errors (wrong lane, no connection, collision): {network}")
    print(f"  capacity (jam, yield) and other: {capacity}")
    print()
    print(f"vehicles  approach -> junction with  (what happened), {args.top} worst; all in gridlocks.csv")
    for row in result["gridlocks"][: args.top]:
        print(f"{row['vehicles']:8d}  {row['approach']} -> {row['junction_with']}  ({row['kinds']})")


if __name__ == "__main__":
    main()
