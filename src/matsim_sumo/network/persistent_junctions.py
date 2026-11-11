"""Repair the junctions that keep stopping vehicles, with rules that hold for any city.

Reads the network of the signal stage, its signal programs, and the two runs
(one per seed) of the best setting of the driver-behaviour search. Every edit
comes from what those runs show; nothing is taken from inspecting one city.

1. Short links. A link shorter than ``JOIN_M`` lies between two junctions that
   act as one. Where at least ``MIN_STUCK`` vehicles were stuck at those two
   junctions (on an approach to them or inside them) in both runs, the two
   junctions are joined into one and the link disappears. Signals, rail
   crossings and dead ends are not joined, so no signal program changes.
2. Lane continuations. A lane that stopped a vehicle in the wrong lane, or
   with no connection to the next edge of its route, gets the movement of the
   lane next to it (``zero_teleport_repair.lane_continuations``), from either run.
3. Lane-change bans. On the route of a vehicle stopped in the wrong lane, bans
   on changing lanes are lifted on the link where it stopped and on the links
   up to ``UNBAN_UPSTREAM_M`` before it, so drivers can reach the lane they need.

The signal programs are then carried over link by link, signals left without
a program get Webster's, and the repaired network is simulated once with the
SUMO default driver (``plain.*``), which the zero-teleport rounds start from.
``persistent.csv`` lists every edit and the vehicles behind it.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import sumolib

from matsim_sumo.network.signal_timing import NETCONVERT_OPTIONS
from matsim_sumo.network.simulation import route_trips, simulate, write_config
from matsim_sumo.network.zero_teleport_final import lift_lane_change_bans, stuck_events
from matsim_sumo.network.zero_teleport_repair import (
    carry_signals,
    lane_continuations,
    route_edges,
    safe_greens,
    time_signals,
    untimed_signals,
)

JOIN_M = 20.0
MIN_STUCK = 2
UNBAN_UPSTREAM_M = 500.0
NOT_JOINED = ("rail_crossing", "rail_signal", "dead_end", "district")
SEED = 23


def junction_of(net, lane: str) -> str | None:
    """Junction at the end of a lane; an internal lane belongs to its junction."""
    if lane.startswith(":"):
        node = lane[1:].rsplit("_", 2)[0]
        return node if net.hasNode(node) else None
    edge = lane.rsplit("_", 1)[0]
    return net.getEdge(edge).getToNode().getID() if net.hasEdge(edge) else None


def joinable(node) -> bool:
    kind = node.getType()
    return not kind.startswith("traffic_light") and kind not in NOT_JOINED


def short_link_joins(net, runs: list[list[dict]], length: float = JOIN_M, min_stuck: int = MIN_STUCK) -> list[dict]:
    """Short links whose two junctions stopped at least ``min_stuck`` vehicles in every run."""
    per_run = [Counter(junction_of(net, event["lane"]) for event in events) for events in runs]
    rows: dict[frozenset, dict] = {}
    for edge in net.getEdges():
        if edge.getFunction() == "internal" or edge.getLength() >= length:
            continue
        a, b = edge.getFromNode(), edge.getToNode()
        if a.getID() == b.getID() or not (joinable(a) and joinable(b)):
            continue
        counts = [run[a.getID()] + run[b.getID()] for run in per_run]
        pair = frozenset((a.getID(), b.getID()))
        if counts and min(counts) >= min_stuck and pair not in rows:
            rows[pair] = {
                "edge": edge.getID(),
                "street": edge.getName(),
                "length_m": round(edge.getLength(), 1),
                "nodes": tuple(sorted(pair)),
                "stuck_per_run": counts,
            }
    return list(rows.values())


def join_groups(pairs: list[tuple[str, ...]]) -> list[set[str]]:
    """One group per connected set of pairs; a junction listed twice would otherwise be ignored by netconvert."""
    groups: list[set[str]] = []
    for pair in pairs:
        nodes = set(pair)
        for group in [group for group in groups if group & nodes]:
            groups.remove(group)
            nodes |= group
        groups.append(nodes)
    return groups


def banned_edges(net_path: Path) -> set[str]:
    """Edges with a lane that forbids changing lanes for some vehicle class."""
    found = set()
    for _event, element in ET.iterparse(net_path, events=("end",)):
        if element.tag == "edge":
            if element.get("function") != "internal" and any(
                lane.get("changeLeft") or lane.get("changeRight") for lane in element.findall("lane")
            ):
                found.add(element.get("id"))
            element.clear()
    return found


def bans_before(net, path: list[str], stuck_edge: str, banned: set[str], upstream: float = UNBAN_UPSTREAM_M) -> list[str]:
    """Banned edges among the stuck edge and the route's edges up to ``upstream`` metres before it."""
    if stuck_edge not in path:
        return [stuck_edge] if stuck_edge in banned else []
    found = []
    walked = 0.0
    for edge_id in reversed(path[: path.index(stuck_edge) + 1]):
        if edge_id in banned:
            found.append(edge_id)
        if net.hasEdge(edge_id):
            walked += net.getEdge(edge_id).getLength()
        if walked > upstream:
            break
    return found


def run_netconvert(command: list[str], log: Path) -> None:
    completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=3600)
    log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise SystemExit((completed.stdout + completed.stderr)[-2000:])


def behaviour_runs(behaviour: Path) -> list[Path]:
    """Run folders of the best setting: one per seed, or the best run and its seed check."""
    summary = json.loads((behaviour / "summary.json").read_text(encoding="utf-8"))
    label = summary["best"]["label"]
    seeds = summary.get("seeds") or [SEED]
    if len(seeds) > 1:
        return [behaviour / (label if seed == SEED else f"{label}-seed{seed}") for seed in seeds]
    return [behaviour / label, behaviour / "best-seed-check"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--net", type=Path, required=True, help="Network of the signal stage (read only)")
    parser.add_argument("--signals", type=Path, required=True, help="Signal programs of --net")
    parser.add_argument("--behaviour", type=Path, required=True, help="Work folder of the driver-behaviour search")
    parser.add_argument("--trips", type=Path, required=True, help="Full-demand trips for the plain run")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--end", type=int, default=3600)
    parser.add_argument("--teleport", type=int, default=300)
    parser.add_argument("--no-simulate", action="store_true", help="Only write the network and its signals")
    args = parser.parse_args(argv)
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)

    print(f"reading {args.net}", flush=True)
    net = sumolib.net.readNet(str(args.net))
    run_dirs = [path for path in behaviour_runs(args.behaviour) if (path / "sim.log").is_file()]
    if len(run_dirs) < 2:
        raise SystemExit(f"the behaviour search in {args.behaviour} has fewer than two runs of its best setting")
    logs = [(path / "sim.log").read_text(encoding="utf-8", errors="replace") for path in run_dirs]
    runs = [stuck_events(text) for text in logs]
    print(f"stuck vehicles in the two runs: {', '.join(str(len(events)) for events in runs)}", flush=True)

    rows: list[dict] = []
    joins = short_link_joins(net, runs)
    groups = join_groups([row["nodes"] for row in joins])
    joined_edges = {
        edge.getID()
        for edge in net.getEdges()
        if any({edge.getFromNode().getID(), edge.getToNode().getID()} <= group for group in groups)
    }
    for row in joins:
        rows.append({"rule": "join short link", "where": row["street"] or row["edge"],
                     "change": f"join {' + '.join(row['nodes'])} ({row['length_m']} m link {row['edge']})",
                     "vehicles": " / ".join(map(str, row["stuck_per_run"]))})

    def routes_of(path: Path) -> Path | None:
        for routes in (path / "vehroutes.xml.gz", args.behaviour / "routes" / "routed.rou.xml"):
            if routes.is_file():
                try:
                    for _event, _element in ET.iterparse(gzip.open(routes) if routes.suffix == ".gz" else routes):
                        pass
                    return routes
                except (ET.ParseError, OSError, EOFError):
                    print(f"  {routes} is incomplete; not used", flush=True)
        return None

    route_files = [routes_of(path) for path in run_dirs]
    connections: dict[tuple, int] = {}
    for path, text, routes in zip(run_dirs, logs, route_files):
        if routes is None:
            print(f"  no readable routes for {path.name}: its lane continuations are left out", flush=True)
            continue
        added, _lane_rows = lane_continuations(net, text, routes)
        for key, count in added.items():
            if key[0] not in joined_edges and key[1] not in joined_edges:
                connections[key] = connections.get(key, 0) + count
    for (frm, to, from_lane, to_lane), count in sorted(connections.items()):
        rows.append({"rule": "lane continuation", "where": net.getEdge(frm).getName() or frm,
                     "change": f"{frm}_{from_lane} -> {to}_{to_lane}", "vehicles": str(count)})

    banned = banned_edges(args.net)
    unban: Counter = Counter()
    for events, routes in zip(runs, route_files):
        stuck = {e["vehicle"]: e["lane"] for e in events if e["kind"] in ("wrong lane", "no connection") and not e["lane"].startswith(":")}
        paths = route_edges(routes, set(stuck)) if routes else {}
        for vehicle, lane in stuck.items():
            for edge_id in bans_before(net, paths.get(vehicle, []), lane.rsplit("_", 1)[0], banned):
                if edge_id not in joined_edges:
                    unban[edge_id] += 1
    for edge_id, count in sorted(unban.items()):
        rows.append({"rule": "lift lane-change ban", "where": net.getEdge(edge_id).getName() or edge_id,
                     "change": edge_id, "vehicles": str(count)})
    for rule, count in Counter(row["rule"] for row in rows).most_common():
        print(f"  {count:3d} {rule}", flush=True)

    joined, output = out / "joined.net.xml", out / "repaired.net.xml"
    if groups:
        join_file = out / "joins.nod.xml"
        join_file.write_text("<nodes>\n" + "".join(
            f'    <join nodes="{" ".join(sorted(group))}"/>\n' for group in groups) + "</nodes>\n", encoding="utf-8")
        run_netconvert(["netconvert", "-s", str(args.net), "-n", str(join_file), *NETCONVERT_OPTIONS, "-o", str(joined)],
                       out / "join.netconvert.txt")
    else:
        shutil.copyfile(args.net, joined)
    if connections:
        connection_file = out / "persistent.con.xml"
        connection_file.write_text("<connections>\n" + "".join(
            f'    <connection from="{frm}" to="{to}" fromLane="{fl}" toLane="{tl}"/>\n'
            for frm, to, fl, tl in sorted(connections)) + "</connections>\n", encoding="utf-8")
        run_netconvert(["netconvert", "-s", str(joined), "--connection-files", str(connection_file),
                        *NETCONVERT_OPTIONS, "-o", str(output)], out / "connections.netconvert.txt")
    else:
        shutil.copyfile(joined, output)
    lifted = lift_lane_change_bans(output, set(unban))
    with (out / "persistent.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["rule", "where", "change", "vehicles"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"repaired network: {output}: {len(groups)} joins, {len(connections)} lane continuations, "
          f"{lifted} lane-change bans lifted", flush=True)

    new_net = sumolib.net.readNet(str(output))
    signals_out = out / "signals.add.xml"
    carried = carry_signals(net, new_net, args.signals, signals_out)
    print(f"signals: {carried['kept']} unchanged, {len(carried['remapped'])} renumbered, {len(carried['dropped'])} dropped", flush=True)
    if args.no_simulate:
        return
    routes = out / "plain.rou.xml"
    print("routing the trips on the repaired network", flush=True)
    route_trips(output, args.trips, routes)
    untimed = untimed_signals(new_net, signals_out)
    timed = time_signals(output, routes, signals_out, out, untimed) if untimed else []
    safe_greens(new_net, signals_out)
    print("simulating the repaired network with the SUMO default driver", flush=True)
    stats = simulate(output, routes, out / "plain.log", args.end, args.teleport, (signals_out,), seed=SEED)
    write_config(out / "plain.sumocfg", output, routes, args.end, args.teleport, (signals_out,), seed=SEED)
    (out / "persistent.json").write_text(json.dumps({
        "runs": [str(path) for path in run_dirs],
        "joins": [sorted(group) for group in groups],
        "lane_continuations": [list(key) for key in sorted(connections)],
        "lane_change_bans_lifted": sorted(unban),
        "signals": carried,
        "webster_timed": timed,
        "plain": stats,
    }, indent=2) + "\n", encoding="utf-8")
    print(f"teleported {stats['teleports']} of {stats['inserted']} inserted ({stats['teleported_percent']} %)", flush=True)


if __name__ == "__main__":
    main()
