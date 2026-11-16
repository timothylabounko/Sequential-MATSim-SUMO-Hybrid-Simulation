"""Last step of the network correction: no vehicle is teleported, at a cost in realism.

Runs once, after the behaviour search of the last repair round, on that
round's network, signals, routes and driver. It is meant for the stuck
vehicles the repair rounds could not remove, and every change it makes is
written to ``forced.csv`` so it can be reported as a deliberate deviation from
the real network.

1. Stuck places. Every teleport and emergency stop in the logs of the round's
   best behaviour run (one log per seed) is placed on its movement: junction,
   approach edge and lane, and the next edge of the vehicle's last route.
2. Forced network:
   * yield at a junction without a signal: an actuated signal, joining a
     signal within ``JOIN_SIGNAL_M``. A merge (one road leaves the junction)
     becomes a zipper junction instead, so no light stops the main road;
   * yield at a signal: after every green in which the stuck movement and a
     movement in conflict with it are both green, a yellow for the conflicting
     movements and a short protected green ('G') with them on red;
   * jam on an approach to a signal: the maximum green of the phases that
     serve the stuck lane is raised by ``MAX_GREEN_RAISE``;
   * jam anywhere else: the next edge of the stuck vehicle's route, the
     bottleneck it waits for, gets one more lane, connected like the lane next
     to it (netconvert leaves an added lane unconnected);
   * wrong lane or no connection: every lane of the approach gets the movement
     to the next edge (only the stuck lane when no lane had it), and the lane
     change bans on the approach are lifted;
   * collisions: no change here, see step 3.
   Signal programs are carried over link by link, and signals without a
   program are timed with Webster, as in ``zero_teleport_repair``.
3. No teleports: ``--time-to-teleport -1`` with ``--ignore-junction-blocker``
   ``BLOCKER_S`` and ``--collision.action warn``. No vehicle is removed. One
   that stands on a junction for ``BLOCKER_S`` seconds is driven through by the
   others, and vehicles that touch only cause a warning.
4. Stuck count. With teleporting off the teleport count is zero by
   definition, so the runs are watched through TraCI every ``CHECK_S``
   seconds. A vehicle that has stood still ``STUCK_S`` seconds (the old
   teleport time) at the front of a lane is one SUMO would have teleported;
   vehicles standing that long further back in a queue are counted apart.
   Travel times are compared with the Google times of the behaviour search,
   and every number with the round's best run.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import sumolib

from matsim_sumo.network.behaviour_calibration import GENES, sumo_options as behaviour_options, sumo_travel_times, travel_time_error
from matsim_sumo.network.signal_timing import NETCONVERT_OPTIONS
from matsim_sumo.network.simulation import read_statistics, sumo_options, write_config
from matsim_sumo.network.stuck_report import EVENT
from matsim_sumo.network.zero_teleport_repair import (
    NO_CONNECTION,
    carry_signals,
    nearby_signal,
    route_edges,
    safe_greens,
    signal_nodes,
    time_signals,
    untimed_signals,
    write_csv,
)

STUCK_S = 300
CHECK_S = 30
FRONT_M = 15.0
BLOCKER_S = 60
MAX_GREEN_RAISE = 1.5
PROTECTED_YELLOW_S = 3
PROTECTED_MIN_S, PROTECTED_S, PROTECTED_MAX_S = 4, 6, 12
MAX_LANES = 4
NOT_SIGNALLED = ("dead_end", "rail_crossing", "rail_signal", "district", "zipper")
FIRST_SEED = 23
START_LOCK = threading.Lock()


def stuck_events(log_text: str) -> list[dict]:
    """Teleports (jam, yield, wrong lane, collision) and no-connection emergency stops of one run."""
    found = [{"vehicle": vehicle, "kind": kind or "collision", "lane": lane} for vehicle, kind, lane, _time in EVENT.findall(log_text)]
    found += [{"vehicle": vehicle, "kind": "no connection", "lane": lane} for vehicle, lane in NO_CONNECTION.findall(log_text)]
    return found


def movement(net, lane: str, path: list[str]) -> dict | None:
    """Junction, approach edge and lane (None on the junction itself) and next edge of a stuck vehicle."""
    if lane.startswith(":"):
        node_id = lane[1:].rsplit("_", 2)[0]
        if not net.hasNode(node_id):
            return None
        for here, after in zip(path, path[1:]):
            if net.hasEdge(here) and net.getEdge(here).getToNode().getID() == node_id:
                return {"node": node_id, "from_edge": here, "from_lane": None, "to_edge": after}
        return {"node": node_id, "from_edge": None, "from_lane": None, "to_edge": None}
    edge_id, index = lane.rsplit("_", 1)
    if not net.hasEdge(edge_id):
        return None
    after = None
    if edge_id in path and path.index(edge_id) + 1 < len(path):
        after = path[path.index(edge_id) + 1]
    return {"node": net.getEdge(edge_id).getToNode().getID(), "from_edge": edge_id, "from_lane": int(index), "to_edge": after}


def _road(edge) -> bool:
    return edge.getFunction() != "internal" and edge.allows("passenger")


def is_merge(node) -> bool:
    outgoing = [edge for edge in node.getOutgoing() if _road(edge)]
    incoming = [edge for edge in node.getIncoming() if _road(edge)]
    return len(outgoing) == 1 and len(incoming) >= 2


def decide(net, kind: str, where: dict | None) -> tuple[tuple, str] | tuple[None, str]:
    """(change key, description) for one stuck vehicle, or (None, why nothing is changed)."""
    if where is None:
        return None, "lane not in the network"
    node = net.getNode(where["node"])
    signal = node.getType().startswith("traffic_light")
    if kind == "collision":
        return None, "collision: only a warning in the final run"
    if kind == "yield":
        if signal:
            if not (where["from_edge"] and where["to_edge"]):
                return None, "yield at a signal, movement unknown (route changed)"
            return ("protected turn", where["from_edge"], where["to_edge"]), f"protected green {where['from_edge']} -> {where['to_edge']}"
        if node.getType() in NOT_SIGNALLED:
            return None, f"yield at a {node.getType()} junction; left alone"
        if is_merge(node):
            return ("zipper", node.getID()), "merge becomes a zipper junction"
        return ("signal", node.getID()), "actuated signal"
    if kind == "jam":
        if signal and where["from_lane"] is not None:
            lane = f"{where['from_edge']}_{where['from_lane']}"
            return ("longer green", where["from_edge"], where["from_lane"]), f"maximum green x{MAX_GREEN_RAISE:g} for {lane}"
        target = where["to_edge"]
        if not target or not net.hasEdge(target):
            return None, "jam, bottleneck unknown (route changed)"
        edge = net.getEdge(target)
        left = edge.getLane(edge.getLaneNumber() - 1)
        if edge.getLaneNumber() >= MAX_LANES or not left.allows("passenger") or edge.getFunction() == "internal":
            return None, f"jam, bottleneck {target} has {edge.getLaneNumber()} lanes or no car lane on the left; left alone"
        return ("extra lane", target), f"bottleneck {target}: {edge.getLaneNumber()} -> {edge.getLaneNumber() + 1} lanes"
    if kind in ("wrong lane", "no connection"):
        if where["from_lane"] is None or not where["to_edge"]:
            return None, f"{kind}, next edge unknown (route changed)"
        return ("every lane", where["from_edge"], where["to_edge"]), f"every lane of {where['from_edge']} reaches {where['to_edge']}"
    return None, f"{kind}: not handled"


def place(net, key: tuple | None, where: dict | None) -> dict:
    node_id = where["node"] if where else ""
    edge_id = ""
    if key and key[0] in ("protected turn", "longer green", "every lane"):
        edge_id = key[1]
    elif key and key[0] == "extra lane":
        edge_id = key[1]
        node_id = net.getEdge(edge_id).getFromNode().getID()
    elif where and where.get("from_edge"):
        edge_id = where["from_edge"]
    street = net.getEdge(edge_id).getName() if edge_id and net.hasEdge(edge_id) else ""
    if not street and node_id and net.hasNode(node_id):
        street = " / ".join(sorted({edge.getName() for edge in net.getNode(node_id).getIncoming() if edge.getName()}))
    lat = lon = ""
    if node_id and net.hasNode(node_id):
        lon, lat = net.convertXY2LonLat(*net.getNode(node_id).getCoord())
        lat, lon = round(lat, 6), round(lon, 6)
    return {"junction": node_id, "street": street, "lat": lat, "lon": lon}


def forced_changes(net, runs: dict[int, tuple[str, Path]]) -> tuple[dict[tuple, dict], list[dict]]:
    """Changes keyed by what they change, each with its stuck vehicles per seed, and the rows of ``forced.csv``."""
    changes: dict[tuple, dict] = {}
    seeds = sorted(runs)
    for seed, (log_text, vehroutes) in runs.items():
        events = stuck_events(log_text)
        paths = route_edges(vehroutes, {event["vehicle"] for event in events}) if vehroutes.is_file() else {}
        for event in events:
            where = movement(net, event["lane"], paths.get(event["vehicle"], []))
            key, text = decide(net, event["kind"], where)
            row_key = key or ("unchanged", event["lane"], text)
            if row_key not in changes:
                changes[row_key] = {
                    "change": key[0] if key else "unchanged",
                    **place(net, key, where),
                    "detail": text,
                    "kinds": Counter(),
                    **{f"vehicles_seed{s}": 0 for s in seeds},
                    "lanes": set(),
                }
            changes[row_key]["kinds"][event["kind"]] += 1
            changes[row_key][f"vehicles_seed{seed}"] += 1
            if where and where.get("from_lane") is not None:
                changes[row_key]["lanes"].add(where["from_lane"])
    rows = []
    for row in sorted(changes.values(), key=lambda r: (r["change"] == "unchanged", r["change"], -sum(r[f"vehicles_seed{s}"] for s in seeds))):
        rows.append(
            {
                **{k: v for k, v in row.items() if k not in ("kinds", "lanes")},
                "kinds": ", ".join(f"{kind} {count}" for kind, count in row["kinds"].most_common()),
            }
        )
    return changes, rows


def fill_lanes(reaching: dict[int, list[int]], lanes: list[int]) -> dict[int, int]:
    """Target lane for each lane in ``lanes`` that has no connection yet, next to the lanes that have one."""
    if not reaching:
        return {}
    low, high = min(reaching), max(reaching)
    added = {}
    for lane in lanes:
        if lane in reaching:
            continue
        if lane < low:
            added[lane] = min(reaching[low])
        elif lane > high:
            added[lane] = max(reaching[high])
        else:
            neighbour = min(reaching, key=lambda other: (abs(other - lane), other))
            added[lane] = min(reaching[neighbour]) if lane < neighbour else max(reaching[neighbour])
    return added


def every_lane_connections(net, from_edge: str, to_edge: str, stuck_lanes: set[int]) -> list[tuple[str, str, int, int]]:
    edge, target = net.getEdge(from_edge), net.getEdge(to_edge)
    reaching = {}
    for lane in edge.getLanes():
        targets = sorted(c.getToLane().getIndex() for c in lane.getOutgoing() if c.getToLane().getEdge().getID() == to_edge)
        if targets:
            reaching[lane.getIndex()] = targets
    if not reaching:
        if target.getFromNode().getID() != edge.getToNode().getID():
            return []
        car_lanes = [lane.getIndex() for lane in target.getLanes() if lane.allows("passenger")]
        if not car_lanes:
            return []
        return [(from_edge, to_edge, lane, min(car_lanes, key=lambda t: abs(t - lane))) for lane in sorted(stuck_lanes)]
    car = [lane.getIndex() for lane in edge.getLanes() if lane.allows("passenger")]
    return [(from_edge, to_edge, lane, to_lane) for lane, to_lane in sorted(fill_lanes(reaching, car).items())]


def extra_lane_connections(net, edge_id: str, widened: set[str]) -> list[tuple[str, str, int, int]]:
    """Connections for a lane added on the left of ``edge_id``: the same as the lane it is added next to.
    Between two widened edges the new lanes connect to each other."""
    edge = net.getEdge(edge_id)
    new = edge.getLaneNumber()
    left = edge.getLane(new - 1)
    found = []
    for c in left.getOutgoing():
        target = c.getToLane().getEdge()
        to_lane = c.getToLane().getIndex()
        if target.getID() in widened and to_lane == target.getLaneNumber() - 1:
            to_lane = target.getLaneNumber()
        found.append((edge_id, target.getID(), new, to_lane))
    for connections in edge.getIncoming().values():
        for c in connections:
            source = c.getFromLane().getEdge()
            if c.getToLane().getID() != left.getID() or source.getFunction() == "internal":
                continue
            if source.getID() in widened and c.getFromLane().getIndex() == source.getLaneNumber() - 1:
                continue
            found.append((source.getID(), edge_id, c.getFromLane().getIndex(), new))
    return found


def write_patches(out_dir: Path, node_patches: dict, lane_counts: dict, connections: list) -> list[str]:
    options = []
    if node_patches:
        path = out_dir / "forced.nod.xml"
        lines = [
            f'    <node id="{node_id}" ' + " ".join(f'{k}="{v}"' for k, v in attributes.items()) + "/>"
            for node_id, attributes in sorted(node_patches.items())
        ]
        path.write_text("<nodes>\n" + "\n".join(lines) + "\n</nodes>\n", encoding="utf-8")
        options += ["-n", str(path)]
    if lane_counts:
        path = out_dir / "forced.edg.xml"
        lines = [f'    <edge id="{edge_id}" numLanes="{count}"/>' for edge_id, count in sorted(lane_counts.items())]
        path.write_text("<edges>\n" + "\n".join(lines) + "\n</edges>\n", encoding="utf-8")
        options += ["-e", str(path)]
    if connections:
        path = out_dir / "forced.con.xml"
        lines = [
            f'    <connection from="{frm}" to="{to}" fromLane="{fl}" toLane="{tl}"/>'
            for frm, to, fl, tl in sorted(set(connections))
        ]
        path.write_text("<connections>\n" + "\n".join(lines) + "\n</connections>\n", encoding="utf-8")
        options += ["--connection-files", str(path)]
    return options


def lift_lane_change_bans(net_path: Path, edges: set[str]) -> int:
    tree = ET.parse(net_path)
    cleared = 0
    for edge in tree.getroot().iter("edge"):
        if edge.get("id") not in edges:
            continue
        for lane in edge.findall("lane"):
            for attribute in ("changeLeft", "changeRight"):
                if attribute in lane.attrib:
                    del lane.attrib[attribute]
                    cleared += 1
    if cleared:
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
    return cleared


def tls_connections(net) -> dict[str, dict[int, object]]:
    """Connection object of every link index of every signal."""
    found: dict[str, dict[int, object]] = {}
    for tls in net.getTrafficLights():
        for in_lane, out_lane, index in tls.getConnections():
            for connection in in_lane.getOutgoing():
                if connection.getToLane().getID() == out_lane.getID():
                    found.setdefault(tls.getID(), {})[index] = connection
                    break
    return found


def conflicting(connection, other) -> bool:
    node = connection.getFromLane().getEdge().getToNode()
    if other.getFromLane().getEdge().getToNode() is not node:
        return False
    first, second = node.getLinkIndex(connection), node.getLinkIndex(other)
    if first < 0 or second < 0 or first == second:
        return False
    try:
        return node.areFoes(first, second) or node.areFoes(second, first)
    except (KeyError, IndexError):
        return False


def protect(logic, stuck: set[int], foes: set[int]) -> int:
    """After every green in which a stuck link and a link in conflict with it are both green, a yellow for the
    conflicting links and a protected green for the stuck ones."""
    phases = logic.findall("phase")
    result = []
    added = 0
    for phase in phases:
        result.append(phase)
        state = phase.get("state", "")
        if "y" in state:
            continue
        waiting = [i for i in sorted(stuck) if i < len(state) and state[i] in "Gg"]
        moving = [i for i in sorted(foes) if i < len(state) and state[i] in "Gg"]
        if not waiting or not moving:
            continue
        yellow, green = list(state), list(state)
        for i in moving:
            yellow[i], green[i] = "y", "r"
        for i in waiting:
            green[i] = "G"
        result.append(ET.Element("phase", {"duration": str(PROTECTED_YELLOW_S), "state": "".join(yellow)}))
        result.append(
            ET.Element(
                "phase",
                {
                    "duration": str(PROTECTED_S),
                    "state": "".join(green),
                    "minDur": str(PROTECTED_MIN_S),
                    "maxDur": str(PROTECTED_MAX_S),
                },
            )
        )
        added += 1
    for phase in phases:
        logic.remove(phase)
    for phase in result:
        phase.tail = "\n        "
        logic.append(phase)
    return added


def raise_green(logic, links: set[int], factor: float = MAX_GREEN_RAISE) -> int:
    raised = 0
    for phase in logic.findall("phase"):
        state = phase.get("state", "")
        if "y" in state or not any(i < len(state) and state[i] in "Gg" for i in links):
            continue
        if phase.get("maxDur"):
            phase.set("maxDur", str(round(float(phase.get("maxDur")) * factor)))
        else:
            phase.set("duration", str(round(float(phase.get("duration")) * factor)))
        raised += 1
    return raised


def force_signals(net, signals_file: Path, protected: list[tuple], greens: list[tuple]) -> list[str]:
    """Longer greens and protected phases in ``signals_file``; one line per change for the report."""
    links = tls_connections(net)
    tree = ET.parse(signals_file)
    logics = {logic.get("id"): logic for logic in tree.getroot().iter("tlLogic")}
    report = []

    def matching(from_edge: str, to_edge: str | None, from_lane: int | None):
        for tls_id, by_index in links.items():
            found = {
                i
                for i, c in by_index.items()
                if c.getFromLane().getEdge().getID() == from_edge
                and (to_edge is None or c.getToLane().getEdge().getID() == to_edge)
                and (from_lane is None or c.getFromLane().getIndex() == from_lane)
            }
            if found:
                yield tls_id, found

    for from_edge, from_lane in greens:
        hits = list(matching(from_edge, None, from_lane))
        for tls_id, found in hits:
            if tls_id not in logics:
                report.append(f"longer green {from_edge}_{from_lane}: {tls_id} runs netconvert's program; unchanged")
                continue
            count = raise_green(logics[tls_id], found)
            report.append(f"longer green {from_edge}_{from_lane}: {count} phases of {tls_id}")
        if not hits:
            report.append(f"longer green {from_edge}_{from_lane}: no signal link; unchanged")
    for from_edge, to_edge in protected:
        hits = list(matching(from_edge, to_edge, None))
        for tls_id, stuck in hits:
            if tls_id not in logics:
                report.append(f"protected turn {from_edge} -> {to_edge}: {tls_id} runs netconvert's program; unchanged")
                continue
            by_index = links[tls_id]
            foes = {i for i, c in by_index.items() if i not in stuck and any(conflicting(by_index[s], c) for s in stuck)}
            count = protect(logics[tls_id], stuck, foes)
            report.append(
                f"protected turn {from_edge} -> {to_edge}: {count} protected phases at {tls_id} "
                f"(links {sorted(stuck)}, {len(foes)} conflicting links stopped)"
                if count
                else f"protected turn {from_edge} -> {to_edge}: never green with a conflicting movement at {tls_id}; unchanged"
            )
        if not hits:
            report.append(f"protected turn {from_edge} -> {to_edge}: no signal link; unchanged")
    tree.write(signals_file, encoding="UTF-8", xml_declaration=True)
    return report


def final_options(genes: dict[str, float]) -> tuple[str, ...]:
    """SUMO options of the round's driver, with junction blockers ignored after BLOCKER_S and collisions as warnings."""
    individual = tuple(genes.get(gene.name, gene.default) for gene in GENES)
    options = list(behaviour_options(individual))
    if "--ignore-junction-blocker" in options:
        options[options.index("--ignore-junction-blocker") + 1] = str(BLOCKER_S)
    else:
        options += ["--ignore-junction-blocker", str(BLOCKER_S)]
    return (*options, "--collision.action", "warn")


def record_stuck(stuck: dict[str, dict], vehicle: str, waited: float, lane: str, front: bool, time: float, seed: int) -> None:
    row = stuck.get(vehicle)
    if row is None:
        stuck[vehicle] = {
            "vehicle": vehicle,
            "seed": seed,
            "first_s": time,
            "lane": lane,
            "front": front,
            "longest_s": waited,
            "standing_at_end": False,
        }
        return
    row["longest_s"] = max(row["longest_s"], waited)
    if front and not row["front"]:
        row["front"], row["lane"] = True, lane


def watched_run(
    net: Path,
    routes: Path,
    additional: tuple[Path, ...],
    run_dir: Path,
    seed: int,
    extra: tuple[str, ...],
    end: int,
    rerouting: bool = True,
) -> dict:
    """One SUMO run without teleports, watched through TraCI for vehicles standing still STUCK_S seconds."""
    tools = Path(os.environ["SUMO_HOME"]) / "tools"
    if str(tools) not in sys.path:
        sys.path.append(str(tools))
    import traci
    import traci.constants as tc

    run_dir.mkdir(parents=True, exist_ok=True)
    log, stats, tripinfo = run_dir / "sim.log", run_dir / "sim.stats.xml", run_dir / "tripinfo.xml"
    command = [
        "sumo",
        "-n",
        str(net),
        "-r",
        str(routes),
        *sumo_options(end, -1, additional, extra, seed, rerouting),
        "--no-step-log",
        "true",
        "--duration-log.statistics",
        "true",
        "--log",
        str(log),
        "--statistic-output",
        str(stats),
        "--tripinfo-output",
        str(tripinfo),
        "--tripinfo-output.write-unfinished",
        "true",
    ]
    label = f"final-seed{seed}"
    with START_LOCK:
        traci.start(command, label=label, stdout=subprocess.DEVNULL)
        connection = traci.getConnection(label)
    variables = (tc.VAR_WAITING_TIME, tc.VAR_LANE_ID, tc.VAR_LANEPOSITION)
    watched: set[str] = set()
    lengths: dict[str, float] = {}
    stuck: dict[str, dict] = {}
    time = 0
    try:
        while time < end:
            time = min(end, time + CHECK_S)
            connection.simulationStep(float(time))
            for vehicle, values in connection.vehicle.getAllSubscriptionResults().items():
                waited = values.get(tc.VAR_WAITING_TIME, 0.0)
                if waited < STUCK_S:
                    continue
                lane = values[tc.VAR_LANE_ID]
                if lane not in lengths:
                    lengths[lane] = connection.lane.getLength(lane)
                record_stuck(stuck, vehicle, waited, lane, lengths[lane] - values[tc.VAR_LANEPOSITION] <= FRONT_M, time, seed)
                if time >= end:
                    stuck[vehicle]["standing_at_end"] = True
            for vehicle in set(connection.vehicle.getIDList()) - watched:
                connection.vehicle.subscribe(vehicle, variables)
                watched.add(vehicle)
            if time % 600 == 0:
                print(f"  seed {seed}: {time // 60} min, {sum(r['front'] for r in stuck.values())} stuck at a lane front so far", flush=True)
    finally:
        connection.close()
    result = read_statistics(stats)
    result["seed"] = seed
    result["stuck"] = sorted(stuck.values(), key=lambda row: row["first_s"])
    result["stuck_front"] = sum(row["front"] for row in stuck.values())
    result["stuck_queue"] = sum(not row["front"] for row in stuck.values())
    result["standing_at_end"] = sum(row["standing_at_end"] for row in stuck.values())
    result["collision_warnings"] = log.read_text(encoding="utf-8", errors="replace").count("collision with vehicle")
    return result


def describe(result: dict) -> str:
    tt = result.get("travel_time", {})
    return (
        f"teleported {result['teleports']}, stuck {STUCK_S} s at a lane front {result['stuck_front']} "
        f"(further back in a queue {result['stuck_queue']}, still standing at the end {result['standing_at_end']}), "
        f"collisions {result['collisions']} (warnings only), inserted {result['inserted']} of {result['loaded']}, "
        f"not inserted {result['not_inserted_percent']} %, travel-time bias {tt.get('bias_percent')} % "
        f"(MRE {tt.get('mre_percent')} %)"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--round", type=Path, required=True, help="Last repair round: repaired.net.xml, signals.add.xml, behaviour/")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--end", type=int, default=3600)
    parser.add_argument("--workers", type=int, default=2, help="Seeds simulated at the same time")
    parser.add_argument("--skip-network", action="store_true", help="Reuse repaired.net.xml and signals.add.xml in --out-dir")
    parser.add_argument("--skip-run", action="store_true", help="Only write the network and final.sumocfg")
    parser.add_argument(
        "--settings-only",
        action="store_true",
        help="No forced network: the round's network and signals with only the no-teleport settings, for comparison",
    )
    args = parser.parse_args(argv)
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    behaviour = args.round / "behaviour"
    summary = json.loads((behaviour / "summary.json").read_text(encoding="utf-8"))
    best = summary["best"]
    seeds = summary.get("seeds") or [FIRST_SEED]
    run_dirs = {seed: behaviour / (best["label"] if seed == FIRST_SEED else f"{best['label']}-seed{seed}") for seed in seeds}
    routes = behaviour / "best.rou.xml"
    demand = behaviour / "routes" / "routed.rou.xml"
    output = out / "repaired.net.xml"
    signals_out = out / "signals.add.xml"
    if args.settings_only:
        output, signals_out = args.round / "repaired.net.xml", args.round / "signals.add.xml"
    report: dict = {"round": str(args.round), "best": best["label"], "seeds": seeds, "settings_only": args.settings_only}
    previous = out / "final.json"
    if args.skip_network and previous.is_file():
        report["network"] = json.loads(previous.read_text(encoding="utf-8")).get("network")

    if not (args.skip_network or args.settings_only):
        print(f"reading {args.round / 'repaired.net.xml'}", flush=True)
        net = sumolib.net.readNet(str(args.round / "repaired.net.xml"))
        runs = {
            seed: ((path / "sim.log").read_text(encoding="utf-8", errors="replace"), path / "vehroutes.xml.gz")
            for seed, path in run_dirs.items()
        }
        print(f"1 stuck places in {best['label']}, seeds {', '.join(map(str, seeds))}", flush=True)
        changes, rows = forced_changes(net, runs)
        write_csv(out / "forced.csv", rows)

        print("2 forced network", flush=True)
        signals = signal_nodes(net)
        node_patches, lane_counts, connections, ban_edges = {}, {}, [], set()
        protected, greens = [], []
        widened = {key[1] for key in changes if key[0] == "extra lane"}
        for key, change in changes.items():
            action = key[0]
            if action == "signal":
                node_patches[key[1]] = {"type": "traffic_light", "tlType": "actuated"}
                joined = nearby_signal(net.getNode(key[1]), signals)
                if joined:
                    node_patches[key[1]]["tl"] = joined
            elif action == "zipper":
                node_patches[key[1]] = {"type": "zipper"}
            elif action == "protected turn":
                protected.append((key[1], key[2]))
            elif action == "longer green":
                greens.append((key[1], key[2]))
            elif action == "extra lane":
                lane_counts[key[1]] = net.getEdge(key[1]).getLaneNumber() + 1
                connections += extra_lane_connections(net, key[1], widened)
            elif action == "every lane":
                connections += every_lane_connections(net, key[1], key[2], change["lanes"])
                ban_edges.add(key[1])
        for action, count in Counter(row["change"] for row in rows).most_common():
            print(f"  {count:3d} {action}", flush=True)
        patches = write_patches(out, node_patches, lane_counts, connections)
        command = ["netconvert", "-s", str(args.round / "repaired.net.xml"), *patches, *NETCONVERT_OPTIONS, "-o", str(output)]
        completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=3600)
        (out / "netconvert.txt").write_text(completed.stdout + completed.stderr, encoding="utf-8")
        if completed.returncode != 0 or not output.is_file():
            raise SystemExit((completed.stdout + completed.stderr)[-2000:])
        lifted = lift_lane_change_bans(output, ban_edges)

        print("3 signal programs: carried over, Webster for new signals, longer greens, protected turns", flush=True)
        new_net = sumolib.net.readNet(str(output))
        carried = carry_signals(net, new_net, args.round / "signals.add.xml", signals_out)
        untimed = untimed_signals(new_net, signals_out)
        timed: list[str] = []
        if untimed:
            try:
                timed = time_signals(output, demand, signals_out, out, untimed)
            except RuntimeError as exc:
                print(f"  Webster failed, netconvert's actuated programs stay: {str(exc)[-300:]}", flush=True)
        signal_lines = force_signals(new_net, signals_out, protected, greens)
        yielding = safe_greens(new_net, signals_out)
        for line in [*carried["dropped"], *signal_lines]:
            print(f"  {line}", flush=True)
        print(
            f"  {carried['kept']} programs unchanged, {len(carried['remapped'])} renumbered, {len(carried['dropped'])} dropped; "
            f"Webster for {len(timed)} of {len(untimed)} untimed signals",
            flush=True,
        )
        report["network"] = {
            "node_types": node_patches,
            "lane_counts": lane_counts,
            "connections": [list(c) for c in sorted(set(connections))],
            "lane_change_bans_lifted": lifted,
            "signals": carried,
            "webster_timed": timed,
            "untimed_after": sorted(set(untimed) - set(timed)),
            "yielding_greens": yielding,
            "signal_changes": signal_lines,
        }
        print(
            f"wrote {output}: {len(node_patches)} junction types, {len(lane_counts)} extra lanes, "
            f"{len(set(connections))} connections, {lifted} lane-change bans lifted",
            flush=True,
        )

    extra = final_options(best["genes"])
    config = write_config(out / "final.sumocfg", output, routes, args.end, -1, (signals_out,), extra, FIRST_SEED)
    report["options"] = list(extra)
    report["gui_config"] = str(config)
    if args.skip_run:
        (out / "final.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"open in the GUI: sumo-gui -c {config}")
        return

    print(f"4 simulating seeds {', '.join(map(str, seeds))} without teleports ({' '.join(extra)})", flush=True)
    google_file = behaviour / "google-times.json"
    google = {}
    if google_file.is_file():
        google = {k: v for k, v in json.loads(google_file.read_text(encoding="utf-8")).items() if not k.startswith("_") and v}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {seed: pool.submit(watched_run, output, routes, (signals_out,), out / f"seed{seed}", seed, extra, args.end) for seed in seeds}
        results = {seed: future.result() for seed, future in futures.items()}
    final_net = sumolib.net.readNet(str(output))
    for seed, result in results.items():
        tripinfo = out / f"seed{seed}" / "tripinfo.xml"
        result["travel_time"] = travel_time_error(sumo_travel_times(tripinfo, set(google)), google)
        tripinfo.unlink(missing_ok=True)
        stuck_rows = result.pop("stuck")
        for row in stuck_rows:
            edge_id = row["lane"].rsplit("_", 1)[0]
            row["street"] = final_net.getEdge(edge_id).getName() if final_net.hasEdge(edge_id) else ""
        write_csv(out / f"seed{seed}" / "stuck.csv", stuck_rows)

    before = {}
    for seed in seeds:
        stats = run_dirs[seed] / "sim.stats.xml"
        if stats.is_file():
            before[seed] = read_statistics(stats)
    report["results"] = {str(seed): result for seed, result in results.items()}
    report["before"] = {str(seed): result for seed, result in before.items()}
    (out / "final.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print()
    tt = best.get("travel_time", {})
    print(f"{args.round.name} {best['label']}: travel-time bias {tt.get('bias_percent')} % (MRE {tt.get('mre_percent')} %), mean of the seeds")
    for seed in seeds:
        if seed in before:
            row = before[seed]
            print(
                f"seed {seed} before: teleported {row['teleports']} ({row['teleported_percent']} % of {row['inserted']} inserted), "
                f"collisions {row['collisions']}, not inserted {row['not_inserted_percent']} %"
            )
        print(f"seed {seed} final:  {describe(results[seed])}")
    if not args.settings_only:
        print(f"forced changes: {out / 'forced.csv'}")
    print(f"stuck vehicles: {out}/seed*/stuck.csv")
    print(f"open in the GUI: sumo-gui -c {config}")


if __name__ == "__main__":
    main()
