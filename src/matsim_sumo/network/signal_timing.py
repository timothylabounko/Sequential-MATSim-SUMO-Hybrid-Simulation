"""Traffic-light timing from the full demand, after the network repair.

The signal programs of the network are not the real Zürich programs: netconvert
guessed which junctions have signals from OpenStreetMap and gave each an
actuated program with standard phase lengths. This stage replaces those
lengths with standard engineering methods. It never tunes a signal to the
number of stuck vehicles.

1. Junction split. An edge shorter than one car (``SHORT_EDGE_M``) between two
   junctions cannot hold a stopped vehicle, so it stands across the junction
   behind it and is hit there. Where the last simulation of the repair shows at
   least ``MIN_COLLISIONS`` collision pairs on such an edge, its two junctions
   are joined into one (netconvert ``<join>``). A joined signal gets a new
   guessed actuated program, as every other signal has.
2. Webster. ``tlsCycleAdaptation.py`` (SUMO) computes the cycle length and the
   green splits of every signal with Webster's method from the routed demand
   (07:00-08:00, saturation headway 2 s, lost time 4 s per phase, cycle
   ``--min-cycle``-``--max-cycle`` s). Yellow and red phases keep their lengths.
3. Programs, one file per variant:

   * ``actuated`` (default): the signals stay actuated. A green phase starts
     with Webster's green, keeps its minimum green, and may be extended by the
     detectors up to ``--max-green-factor`` times Webster's green, as the
     maximum green of an actuated controller is usually set at 1.25-1.5 times
     the green of a fixed-time plan (FHWA Signal Timing Manual, 2008).
   * ``coordinated``: fixed-time Webster plans with offsets for green waves from
     ``tlsCoordinator.py`` (SUMO), along the routes with the most vehicles.
   * ``guessed``: the netconvert programs, for comparison.

Every variant is simulated with the same settings (``simulation.py``), and the
chosen one (``--signals``) is written to ``signals.add.xml`` next to the
network it belongs to (``final.net.xml``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import sumolib

from matsim_sumo.network.national_repair import describe_gridlocks, split_kinds
from matsim_sumo.network.simulation import route_trips, simulate
from matsim_sumo.network.turn_evolution import parse_gridlocks, summarise

SHORT_EDGE_M = 7.5
MIN_COLLISIONS = 2
SEED = 23
VARIANTS = ("guessed", "actuated", "coordinated")
COLLISION_PAIR_RE = re.compile(r"'([^']+)'; (?:junction )?collision with vehicle '([^']+)', lane='([^']+)'")
NETCONVERT_OPTIONS = (
    "--tls.default-type",
    "actuated",
    "--output.street-names",
    "true",
    "--output.original-names",
    "true",
    "--no-turnarounds",
    "true",
    "--junctions.corner-detail",
    "5",
    "--junctions.limit-turn-speed",
    "5.5",
    "--rectangular-lane-cut",
    "false",
    "--walkingareas",
    "false",
    "--offset.disable-normalization",
    "true",
    "--geometry.avoid-overlap",
    "0",
)


def sumo_tool(name: str) -> Path:
    home = os.environ.get("SUMO_HOME")
    candidates = [Path(home) / "tools" / name] if home else []
    candidates.append(Path("/usr/share/sumo/tools") / name)
    for path in candidates:
        if path.is_file():
            return path
    raise SystemExit(f"{name} not found; set SUMO_HOME to the SUMO installation")


def _edge_of_lane(lane_id: str) -> str:
    return lane_id.rsplit("_", 1)[0]


def short_edge_collisions(net, log_text: str, max_length: float = SHORT_EDGE_M) -> dict[str, int]:
    """Collision pairs per edge shorter than ``max_length`` between two junctions.

    A collision on an internal lane is charged to the short edges that enter
    that junction; a collision on a normal lane to that edge if it is short.
    """
    pairs: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for first, second, lane in COLLISION_PAIR_RE.findall(log_text):
        pair = tuple(sorted((first, second)))
        if lane.startswith(":"):
            node_id = lane[1:].rsplit("_", 2)[0]
            if not net.hasNode(node_id):
                continue
            edges = [edge for edge in net.getNode(node_id).getIncoming() if edge.getFunction() != "internal"]
        else:
            edge_id = _edge_of_lane(lane)
            edges = [net.getEdge(edge_id)] if net.hasEdge(edge_id) else []
        for edge in edges:
            if edge.getLength() < max_length and edge.getFromNode().getID() != edge.getToNode().getID():
                pairs[edge.getID()].add(pair)
    return {edge_id: len(found) for edge_id, found in pairs.items()}


def joins_for(net, collisions: dict[str, int], min_collisions: int = MIN_COLLISIONS) -> list[dict]:
    """Junction pairs to join, one per short edge with repeated collisions; overlapping pairs are merged."""
    groups: list[set[str]] = []
    rows = []
    for edge_id, count in sorted(collisions.items(), key=lambda item: -item[1]):
        if count < min_collisions:
            continue
        edge = net.getEdge(edge_id)
        nodes = {edge.getFromNode().getID(), edge.getToNode().getID()}
        if any(net.getNode(node).getType() in ("rail_crossing", "rail_signal", "dead_end") for node in nodes):
            continue
        lon, lat = net.convertXY2LonLat(*edge.getToNode().getCoord())
        rows.append(
            {
                "edge": edge_id,
                "street": edge.getName(),
                "length_m": round(edge.getLength(), 2),
                "collision_pairs": count,
                "nodes": sorted(nodes),
                "lat": round(lat, 6),
                "lon": round(lon, 6),
            }
        )
        for group in [group for group in groups if group & nodes]:
            nodes |= group
            groups.remove(group)
        groups.append(nodes)
    for row in rows:
        row["joined"] = sorted(next(group for group in groups if set(row["nodes"]) <= group))
    return rows


def join_junctions(source: Path, joins: list[dict], output: Path) -> Path:
    groups = {tuple(row["joined"]) for row in joins}
    join_file = output.with_suffix(".join.nod.xml")
    join_file.write_text(
        "<nodes>\n" + "".join(f'    <join nodes="{" ".join(group)}"/>\n' for group in sorted(groups)) + "</nodes>\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["netconvert", "-s", str(source), "-n", str(join_file), *NETCONVERT_OPTIONS, "-o", str(output)],
        text=True,
        capture_output=True,
        check=False,
        timeout=3600,
    )
    output.with_suffix(".netconvert.txt").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode != 0 or not output.is_file():
        raise RuntimeError((completed.stdout + completed.stderr)[-1500:])
    return output


def webster(net: Path, routes: Path, output: Path, min_cycle: int, max_cycle: int) -> Path:
    """Run SUMO's tlsCycleAdaptation.py (Webster) for every signal with traffic."""
    command = [
        sys.executable,
        str(sumo_tool("tlsCycleAdaptation.py")),
        "-n",
        str(net),
        "-r",
        str(routes),
        "-b",
        "0",
        "-o",
        str(output),
        "-p",
        "webster",
        "--min-cycle",
        str(min_cycle),
        "--max-cycle",
        str(max_cycle),
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=7200)
    output.with_suffix(".txt").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode != 0 or not output.is_file():
        raise RuntimeError((completed.stdout + completed.stderr)[-1500:])
    return output


def _phases(program) -> list[dict]:
    return [
        {"duration": float(phase.duration), "state": phase.state, "minDur": phase.minDur, "maxDur": phase.maxDur}
        for phase in program.getPhases()
    ]


def _is_actuated(phase: dict) -> bool:
    low, high = phase["minDur"], phase["maxDur"]
    return low is not None and high is not None and low >= 0 and high > low


def _is_green(state: str) -> bool:
    return ("G" in state or "g" in state) and "y" not in state


def plan_programs(net, webster_file: Path, max_green_factor: float) -> dict[str, dict]:
    """Webster's green per phase, with the guessed program it replaces."""
    guessed = {tls.getID(): tls for tls in net.getTrafficLights()}
    plans = {}
    for logic in ET.parse(webster_file).getroot().iter("tlLogic"):
        tls = guessed.get(logic.get("id"))
        if tls is None or not tls.getPrograms():
            continue
        program = next(iter(tls.getPrograms().values()))
        old = _phases(program)
        new = [float(phase.get("duration")) for phase in logic.iter("phase")]
        if len(new) != len(old):
            continue
        phases = []
        for before, green in zip(old, new):
            if _is_green(before["state"]):
                low = before["minDur"] if _is_actuated(before) else min(before["duration"], 5.0)
                phases.append(
                    {
                        "state": before["state"],
                        "duration": max(green, low),
                        "minDur": low,
                        "maxDur": max(low + 1, round(max_green_factor * green)),
                    }
                )
            else:
                phases.append({"state": before["state"], "duration": before["duration"]})
        plans[tls.getID()] = {"type": program._type, "offset": float(program._offset), "phases": phases}
    return plans


def write_programs(plans: dict[str, dict], output: Path, actuated: bool, offsets: dict[str, float] | None = None) -> Path:
    lines = ["<additional>"]
    for tls_id, plan in sorted(plans.items()):
        offset = (offsets or {}).get(tls_id, 0.0)
        kind = "actuated" if actuated else "static"
        lines.append(f'    <tlLogic id="{tls_id}" programID="{"webster" if actuated else "webster-fixed"}" type="{kind}" offset="{offset:.2f}">')
        for phase in plan["phases"]:
            text = f'        <phase duration="{phase["duration"]:g}" state="{phase["state"]}"'
            if actuated and "minDur" in phase:
                text += f' minDur="{phase["minDur"]:g}" maxDur="{phase["maxDur"]:g}"'
            lines.append(text + "/>")
        lines.append("    </tlLogic>")
    lines += ["</additional>", ""]
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def coordinate(net: Path, routes: Path, fixed: Path, output: Path) -> dict[str, float]:
    """Offsets from SUMO's tlsCoordinator.py for the fixed-time Webster plans."""
    command = [sys.executable, str(sumo_tool("tlsCoordinator.py")), "-n", str(net), "-r", str(routes), "-a", str(fixed), "-o", str(output)]
    completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=7200)
    output.with_suffix(".txt").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode != 0 or not output.is_file():
        raise RuntimeError((completed.stdout + completed.stderr)[-1500:])
    return {logic.get("id"): float(logic.get("offset", "0")) for logic in ET.parse(output).getroot().iter("tlLogic")}


def describe(result: dict) -> str:
    return (
        f"teleported {result['teleported_percent']} % ({result['teleports']} of {result['inserted']}: "
        f"jam {result['teleports_jam']}, yield {result['teleports_yield']}, wrong lane {result['teleports_wrongLane']}, "
        f"collision {result['teleports_collision']}), not inserted {result['not_inserted_percent']} % "
        f"({result['waiting']} of {result['loaded']}), mean trip {result['trip_duration']} s, "
        f"time loss {result['trip_timeLoss']} s"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--net", type=Path, required=True, help="Repaired network (read only)")
    parser.add_argument("--routes", type=Path, required=True, help="Scaled trips with vType definitions")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--collision-log", type=Path, help="SUMO log of the repaired network; simulated here if missing")
    parser.add_argument("--signals", choices=VARIANTS, default="actuated", help="Variant written to signals.add.xml")
    parser.add_argument("--end", type=int, default=3600)
    parser.add_argument("--teleport", type=int, default=300)
    parser.add_argument("--min-cycle", type=int, default=60)
    parser.add_argument("--max-cycle", type=int, default=120)
    parser.add_argument("--max-green-factor", type=float, default=1.25)
    parser.add_argument("--skip-compare", action="store_true", help="Only simulate the chosen variant")
    args = parser.parse_args(argv)
    work = args.work_dir
    work.mkdir(parents=True, exist_ok=True)

    print("routing the trips on the repaired network", flush=True)
    routes = work / "routes" / "repaired.rou.xml"
    route_trips(args.net, args.routes, routes)
    log = args.collision_log
    if log is None or not log.is_file():
        print("simulating the repaired network to find collisions", flush=True)
        log = work / "repaired" / "sim.log"
        simulate(args.net, routes, log, args.end, args.teleport, seed=SEED)
    net = sumolib.net.readNet(str(args.net))
    joins = joins_for(net, short_edge_collisions(net, log.read_text(encoding="utf-8", errors="replace")))
    final_net = work / "final.net.xml"
    if joins:
        print(f"joining {len(joins)} junction pairs split by an edge shorter than {SHORT_EDGE_M} m with collisions", flush=True)
        for row in joins:
            print(f"  {row['street'] or row['edge']} ({row['length_m']} m, {row['collision_pairs']} collision pairs) {row['lat']},{row['lon']}")
        join_junctions(args.net, joins, final_net)
        route_trips(final_net, args.routes, routes)
    else:
        print("no short edge with repeated collisions", flush=True)
        shutil.copyfile(args.net, final_net)

    print("Webster cycle lengths and green splits (tlsCycleAdaptation.py)", flush=True)
    net = sumolib.net.readNet(str(final_net), withPrograms=True)
    raw = webster(final_net, routes, work / "webster.raw.add.xml", args.min_cycle, args.max_cycle)
    plans = plan_programs(net, raw, args.max_green_factor)
    files = {"guessed": None}
    files["actuated"] = write_programs(plans, work / "signals.actuated.add.xml", actuated=True)
    fixed = write_programs(plans, work / "signals.fixed.add.xml", actuated=False)
    print("green-wave offsets for the fixed-time plans (tlsCoordinator.py)", flush=True)
    offsets = coordinate(final_net, routes, fixed, work / "coordinator.offsets.add.xml")
    files["coordinated"] = write_programs(plans, work / "signals.coordinated.add.xml", actuated=False, offsets=offsets)
    print(f"  {len(plans)} of {len(net.getTrafficLights())} signals have traffic and a Webster plan, {len(offsets)} offsets", flush=True)

    results = {}
    for variant in VARIANTS if not args.skip_compare else (args.signals,):
        print(f"simulating signals: {variant}", flush=True)
        extra = (files[variant],) if files[variant] else ()
        variant_log = work / variant / "sim.log"
        result = simulate(final_net, routes, variant_log, args.end, args.teleport, additional=extra, seed=SEED)
        text = variant_log.read_text(encoding="utf-8", errors="replace")
        result["by_kind"] = summarise(parse_gridlocks(text))["by_kind"]
        result["network_errors"], result["capacity"] = split_kinds(result["by_kind"])
        result["at_signals"] = at_signals(net, text)
        results[variant] = result
        print(f"  {variant}: {describe(result)}", flush=True)

    chosen = files[args.signals]
    signals = work / "signals.add.xml"
    if chosen:
        shutil.copyfile(chosen, signals)
    else:
        signals.write_text("<additional/>\n", encoding="utf-8")
    summary = {
        "net": str(final_net),
        "signals": str(signals),
        "variant": args.signals,
        "joins": joins,
        "webster_signals": len(plans),
        "coordinated_offsets": len(offsets),
        "max_green_factor": args.max_green_factor,
        "cycle_s": [args.min_cycle, args.max_cycle],
        "results": results,
    }
    (work / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print()
    for variant, result in results.items():
        marker = "*" if variant == args.signals else " "
        print(f"{marker} {variant:12s} {describe(result)}; stuck at signals {result['at_signals']}")
    print(f"signals used from here on: {args.signals} -> {signals}")


def at_signals(net, log_text: str) -> str:
    """Stuck vehicles on approaches that end at a traffic light, and in total."""
    rows = describe_gridlocks(net, log_text)
    signal = sum(row["vehicles"] for row in rows if net.getNode(row["junction"]).getType().startswith("traffic_light"))
    return f"{signal} of {sum(row['vehicles'] for row in rows)}"


if __name__ == "__main__":
    main()
