"""Signal timing from the simulated flows, repeated while it improves the run.

Stage 3 times every signal once with Webster's method, from the routes of the
empty network. With en-route rerouting the vehicles then spread differently,
and the queues of the full demand show where a green is too short: at the
signals behind motorway exits, queues reach back to the boundary of the
network, and vehicles cannot enter at all. This stage repeats Webster with the
flows of the last accepted run, as in iterative signal-assignment optimisation
(Allsop and Charlesworth, 1977). It uses only the simulation, never the
traffic counts, so the count comparison stays an independent check.

Every iteration:

1. Demand per signal link: the last route of every vehicle of the accepted run
   (``--vehroute-output``, unfinished vehicles included) and the planned route
   of every vehicle that could not enter, averaged over the seeds. A vehicle is
   counted on every signal link of its route, whether or not it reached it, so
   an approach with a queue keeps its whole demand. A truck counts as
   ``TRUCK_PCU`` passenger cars; a movement served by several lanes is split
   evenly between them.
2. Webster for each program of ``signals.add.xml``. A lane's flow is the sum
   of its links; a green phase's critical ratio y is the largest flow /
   ``SATURATION_PCU_H`` of the lanes whose longest green it is. The intergreen
   L is the sum of the yellow and red phases, the cycle
   C = (1.5 L + 5) / (1 - Y) within ``--min-cycle``-``--max-cycle`` (the
   longest when Y >= ``Y_MAX``), and each green is (C - L) y / Y, at least
   its minimum green.
3. Candidates: the accepted greens moved by ``step`` of the way towards
   Webster's, for each step of ``--steps``. An actuated phase may again be
   extended to ``--max-green-factor`` times its green.
4. Every candidate is simulated with each seed of the round and scored as in
   the behaviour search without the travel-time term:
   ``TELEPORT_WEIGHT`` x teleported % + not inserted %, mean of the seeds. The
   best candidate is accepted only if it lowers the accepted score. When no
   candidate does, the steps are divided by four; after ``--patience`` such
   iterations, or ``--iterations`` in all, the stage stops.

The output folder has the layout of a zero-teleport round (``repaired.net.xml``,
``signals.add.xml``, ``behaviour/``), so the assignment and the no-teleport run
start from it unchanged. ``calibration.json`` lists every candidate; with
``--counts`` it also gives the GEH statistic of every run against the traffic
counts, as a report only.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import json
import shutil
import statistics
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import sumolib

from matsim_sumo.network.behaviour_calibration import sumo_options as behaviour_options
from matsim_sumo.network.behaviour_calibration import sumo_travel_times, travel_time_error
from matsim_sumo.network.count_check import GEH_LIMIT, geh, match_sections, read_edge_counts
from matsim_sumo.network.national_repair import split_kinds
from matsim_sumo.network.route_assignment import driver_individual
from matsim_sumo.network.simulation import simulate, write_config
from matsim_sumo.network.turn_evolution import parse_gridlocks, summarise

SATURATION_PCU_H = 1800.0
TRUCK_PCU = 2.0
Y_MAX = 0.9
TELEPORT_WEIGHT = 10.0
LABEL = "calibrated"


def is_green(state: str) -> bool:
    return ("G" in state or "g" in state) and "y" not in state


def score(result: dict) -> float:
    return round(TELEPORT_WEIGHT * (result["teleported_percent"] or 0) + (result["not_inserted_percent"] or 0), 3)


def read_programs(path: Path) -> dict[str, list[dict]]:
    programs = {}
    for logic in ET.parse(path).getroot().iter("tlLogic"):
        phases = []
        for phase in logic.iter("phase"):
            item = {"state": phase.get("state"), "duration": float(phase.get("duration"))}
            if phase.get("minDur") is not None and phase.get("maxDur") is not None:
                item["minDur"], item["maxDur"] = float(phase.get("minDur")), float(phase.get("maxDur"))
            phases.append(item)
        programs[logic.get("id")] = phases
    return programs


def write_programs(source: Path, programs: dict[str, list[dict]], output: Path) -> Path:
    """``source`` with the phase durations of ``programs``; everything else unchanged."""
    tree = ET.parse(source)
    for logic in tree.getroot().iter("tlLogic"):
        phases = programs.get(logic.get("id"))
        if phases is None:
            continue
        for element, phase in zip(logic.iter("phase"), phases):
            element.set("duration", f"{phase['duration']:g}")
            if "minDur" in phase:
                element.set("minDur", f"{phase['minDur']:g}")
                element.set("maxDur", f"{phase['maxDur']:g}")
    tree.write(output, encoding="UTF-8", xml_declaration=True)
    return output


def signal_links(net) -> tuple[dict[tuple[str, str], list[tuple[str, int]]], dict[str, dict[int, str]]]:
    """Signal links per pair of consecutive edges, and the incoming lane of every link."""
    by_pair: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
    lanes: dict[str, dict[int, str]] = defaultdict(dict)
    for tls in net.getTrafficLights():
        for in_lane, out_lane, index in tls.getConnections():
            pair = (in_lane.getEdge().getID(), out_lane.getEdge().getID())
            by_pair[pair].append((tls.getID(), index))
            lanes[tls.getID()][index] = in_lane.getID()
    return by_pair, lanes


def _vehicles(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as handle:
        vehicle_id, vehicle_type = None, None
        for event, element in ET.iterparse(handle, events=("start", "end")):
            if event == "start" and element.tag == "vehicle":
                vehicle_id, vehicle_type = element.get("id"), element.get("type", "")
            elif event == "end" and element.tag == "route" and vehicle_id is not None and element.get("edges"):
                yield vehicle_id, vehicle_type, element.get("edges").split()
                vehicle_id = None
            elif event == "end" and element.tag == "vehicle":
                element.clear()


def link_demand(by_pair, runs: list[Path], planned: Path) -> dict[str, dict[int, float]]:
    """Passenger-car units per signal link and hour, mean over ``runs``; vehicles missing in a run use ``planned``."""
    plan = {vehicle_id: (vehicle_type, edges) for vehicle_id, vehicle_type, edges in _vehicles(planned)}
    demand: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for run in runs:
        seen = set()
        for vehicle_id, vehicle_type, edges in _vehicles(run):
            seen.add(vehicle_id)
            _add(demand, by_pair, vehicle_type, edges, 1.0 / len(runs))
        for vehicle_id, (vehicle_type, edges) in plan.items():
            if vehicle_id not in seen:
                _add(demand, by_pair, vehicle_type, edges, 1.0 / len(runs))
    return demand


def _add(demand, by_pair, vehicle_type: str, edges: list[str], weight: float) -> None:
    pcu = (TRUCK_PCU if vehicle_type.startswith("truck") else 1.0) * weight
    for pair in zip(edges, edges[1:]):
        links = by_pair.get(pair)
        if links:
            for tls_id, index in links:
                demand[tls_id][index] += pcu / len(links)


def webster_greens(phases: list[dict], lanes: dict[int, str], demand: dict[int, float], min_cycle: float, max_cycle: float) -> dict[int, float] | None:
    """Webster's green per green phase of one program, or None without demand."""
    greens = [i for i, phase in enumerate(phases) if is_green(phase["state"])]
    if not greens:
        return None
    lane_links: dict[str, list[int]] = defaultdict(list)
    for index, lane in lanes.items():
        lane_links[lane].append(index)
    ratio = {i: 0.0 for i in greens}
    for links in lane_links.values():
        flow = sum(demand.get(index, 0.0) for index in links)
        if flow <= 0:
            continue
        serving = [i for i in greens if any(index < len(phases[i]["state"]) and phases[i]["state"][index] in "Gg" for index in links)]
        if serving:
            main = max(serving, key=lambda i: phases[i]["duration"])
            ratio[main] = max(ratio[main], flow / SATURATION_PCU_H)
    total = sum(ratio.values())
    if total <= 0:
        return None
    intergreen = sum(phase["duration"] for i, phase in enumerate(phases) if i not in ratio)
    cycle = max_cycle if total >= Y_MAX else min(max_cycle, max(min_cycle, (1.5 * intergreen + 5) / (1 - total)))
    lowest = {i: phases[i].get("minDur", min(phases[i]["duration"], 5.0)) for i in greens}
    return {i: max(lowest[i], (cycle - intergreen) * ratio[i] / total) for i in greens}


def candidate_programs(programs, links, demand, step: float, min_cycle: float, max_cycle: float, max_green_factor: float) -> tuple[dict, int]:
    """Programs with every green moved ``step`` of the way to Webster's; and the number of programs changed."""
    result, changed = {}, 0
    for tls_id, phases in programs.items():
        target = webster_greens(phases, links.get(tls_id, {}), demand.get(tls_id, {}), min_cycle, max_cycle)
        if target is None:
            result[tls_id] = phases
            continue
        new = []
        for i, phase in enumerate(phases):
            phase = dict(phase)
            if i in target:
                duration = max(1.0, round(phase["duration"] + step * (target[i] - phase["duration"])))
                if "minDur" in phase:
                    duration = max(duration, phase["minDur"])
                    phase["maxDur"] = max(phase["minDur"] + 1, round(max_green_factor * duration))
                phase["duration"] = duration
            new.append(phase)
        if any(a["duration"] != b["duration"] for a, b in zip(new, phases)):
            changed += 1
        result[tls_id] = new
    return result, changed


def run(net: Path, routes: Path, signals: Path, run_dir: Path, seed: int, driver: tuple[str, ...], end: int, teleport: int, google: dict) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    edge_data = run_dir / "edgedata.xml"
    meandata = run_dir / "edgedata.add.xml"
    meandata.write_text(
        f'<additional><edgeData id="hour" file="{edge_data.resolve()}" begin="0" end="{end}" excludeEmpty="true"/></additional>\n',
        encoding="utf-8",
    )
    tripinfo = run_dir / "tripinfo.xml"
    extra = (
        *driver,
        "--tripinfo-output", str(tripinfo),
        "--tripinfo-output.write-unfinished", "true",
        "--vehroute-output", str(run_dir / "vehroutes.xml.gz"),
        "--vehroute-output.last-route", "true",
        "--vehroute-output.write-unfinished", "true",
    )
    stats = simulate(net, routes, run_dir / "sim.log", end, teleport, (signals, meandata), extra, seed)
    by_kind = summarise(parse_gridlocks((run_dir / "sim.log").read_text(encoding="utf-8", errors="replace")))["by_kind"]
    stats["by_kind"] = by_kind
    stats["network_errors"], stats["capacity"] = split_kinds(by_kind)
    stats["travel_time"] = travel_time_error(sumo_travel_times(tripinfo, set(google)), google)
    stats["seed"] = seed
    tripinfo.unlink(missing_ok=True)
    return stats


def mean_of(runs: list[dict]) -> dict:
    keys = ("loaded", "inserted", "running", "waiting", "teleports", "teleports_jam", "teleports_yield",
            "teleports_wrongLane", "teleports_collision", "collisions", "trip_duration", "trip_timeLoss",
            "teleported_percent", "not_inserted_percent")
    result = {key: round(statistics.fmean(float(r[key] or 0) for r in runs), 2) for key in keys}
    biases = [r["travel_time"].get("bias_percent") for r in runs if r.get("travel_time", {}).get("bias_percent") is not None]
    result["travel_time"] = {"bias_percent": round(statistics.fmean(biases), 2) if biases else None}
    result["score"] = score(result)
    return result


def geh_report(sections: list[dict], run_dirs: list[Path]) -> dict | None:
    if not sections:
        return None
    counts = [read_edge_counts(path / "edgedata.xml") for path in run_dirs if (path / "edgedata.xml").is_file()]
    if not counts:
        return None
    values = []
    for section in sections:
        simulated = statistics.fmean(sum(c.get(edge, 0) for edge in section["edges"].split()) for c in counts)
        values.append(geh(simulated, section["count"]))
    good = sum(1 for value in values if value < GEH_LIMIT)
    return {"sections": len(values), "under_5": good, "under_5_percent": round(100 * good / len(values), 1),
            "median": round(statistics.median(values), 2)}


def describe(result: dict) -> str:
    return (
        f"teleported {result['teleports']:g} ({result['teleported_percent']} %), not inserted "
        f"{result['not_inserted_percent']} %, score {result['score']}"
    )


def write_round(out: Path, source: Path, net: Path, routes: Path, signals: Path, summary: dict, best: dict, run_dirs: dict[int, Path], end: int, teleport: int, driver) -> None:
    """``out`` laid out like a zero-teleport round, with the accepted signals and runs."""
    behaviour = out / "behaviour"
    behaviour.mkdir(parents=True, exist_ok=True)
    target_net = out / "repaired.net.xml"
    if not target_net.exists():
        target_net.symlink_to(net.resolve())
    shutil.copyfile(signals, out / "signals.add.xml")
    for name in ("best.rou.xml", "routes"):
        link = behaviour / name
        if not link.exists() and (source / "behaviour" / name).exists():
            link.symlink_to((source / "behaviour" / name).resolve())
    if (source / "behaviour" / "google-times.json").is_file():
        shutil.copyfile(source / "behaviour" / "google-times.json", behaviour / "google-times.json")
    first = min(run_dirs)
    for seed, path in run_dirs.items():
        name = LABEL if seed == first else f"{LABEL}-seed{seed}"
        target = behaviour / name
        if target.is_symlink() or target.exists():
            target.unlink() if target.is_symlink() else shutil.rmtree(target)
        target.symlink_to(path.resolve())
    previous = summary["best"]
    summary = dict(summary)
    summary["best"] = {**previous, **best, "label": LABEL, "genes": previous["genes"], "seeds": sorted(run_dirs)}
    summary["signals"] = str((out / "signals.add.xml").resolve())
    config = write_config(behaviour / "best.sumocfg", target_net, routes, end, teleport, (out / "signals.add.xml",), driver, first)
    summary["gui_config"] = str(config)
    (behaviour / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--round", type=Path, required=True, help="Best zero-teleport round: repaired.net.xml, signals.add.xml, behaviour/")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--patience", type=int, default=2)
    parser.add_argument("--steps", type=float, nargs="+", default=[1.0, 0.5])
    parser.add_argument("--min-cycle", type=float, default=60)
    parser.add_argument("--max-cycle", type=float, default=120)
    parser.add_argument("--max-green-factor", type=float, default=1.25)
    parser.add_argument("--end", type=int, default=3600)
    parser.add_argument("--teleport", type=int, default=300)
    parser.add_argument("--workers", type=int, default=4, help="Simulations at the same time")
    parser.add_argument("--counts", type=Path, help="Reference counts (count_check counts-YYYY-07h.csv), for the GEH report only")
    args = parser.parse_args(argv)
    out = args.out_dir
    runs_dir = out / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    summary = json.loads((args.round / "behaviour" / "summary.json").read_text(encoding="utf-8"))
    seeds = summary.get("seeds") or [23]
    driver = tuple(behaviour_options(driver_individual(summary["best"]["genes"])))
    net_file = args.round / "repaired.net.xml"
    routes = args.round / "behaviour" / "best.rou.xml"
    google_file = args.round / "behaviour" / "google-times.json"
    google = {}
    if google_file.is_file():
        google = {k: v for k, v in json.loads(google_file.read_text(encoding="utf-8")).items() if not k.startswith("_") and v}

    print(f"reading {net_file}", flush=True)
    net = sumolib.net.readNet(str(net_file))
    by_pair, links = signal_links(net)
    sections = []
    if args.counts and args.counts.is_file():
        with args.counts.open(encoding="utf-8") as handle:
            sites = [{**row, "count": float(row["count"]), "days": int(row["days"]), "lat": float(row["lat"]), "lon": float(row["lon"])} for row in csv.DictReader(handle)]
        sections, _ = match_sections(net, sites)
        print(f"GEH report against {len(sections)} counting cross-sections (not used for the decisions)", flush=True)

    report_path = out / "calibration.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {"round": str(args.round), "seeds": seeds, "iterations": []}
    done = {(row["iteration"], row["step"]): row for row in report["iterations"]}

    def evaluate(iteration: int, step: float, signals: Path) -> tuple[dict, dict[int, Path]]:
        key = (iteration, step)
        run_dirs = {seed: runs_dir / f"it{iteration:02d}-step{step:g}-seed{seed}" for seed in seeds}
        if key in done and all((path / "sim.stats.xml").is_file() for path in run_dirs.values()):
            return done[key], run_dirs
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {seed: pool.submit(run, net_file, routes, signals, path, seed, driver, args.end, args.teleport, google) for seed, path in run_dirs.items()}
            results = {seed: future.result() for seed, future in futures.items()}
        row = {"iteration": iteration, "step": step, "signals": str(signals), **mean_of(list(results.values())),
               "seeds": {str(seed): {k: r[k] for k in ("teleports", "teleported_percent", "not_inserted_percent", "collisions")} for seed, r in results.items()}}
        row["geh"] = geh_report(sections, list(run_dirs.values()))
        return row, run_dirs

    def record(row: dict) -> None:
        report["iterations"] = [r for r in report["iterations"] if (r["iteration"], r["step"]) != (row["iteration"], row["step"])] + [row]
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"iteration 0: the round as it is ({args.round}), seeds {', '.join(map(str, seeds))}", flush=True)
    start_signals = out / "signals.it00.add.xml"
    if not start_signals.is_file():
        shutil.copyfile(args.round / "signals.add.xml", start_signals)
    best_row, best_dirs = evaluate(0, 0.0, start_signals)
    best_row["accepted"] = True
    record(best_row)
    best_signals = start_signals
    print(f"  {describe(best_row)}" + (f"; GEH < 5 at {best_row['geh']['under_5_percent']} %" if best_row.get("geh") else ""), flush=True)

    steps = list(args.steps)
    stale = 0
    for iteration in range(1, args.iterations + 1):
        demand = link_demand(by_pair, [path / "vehroutes.xml.gz" for path in best_dirs.values()], routes)
        programs = read_programs(best_signals)
        candidates = []
        for step in steps:
            new, changed = candidate_programs(programs, links, demand, step, args.min_cycle, args.max_cycle, args.max_green_factor)
            path = out / f"signals.it{iteration:02d}-step{step:g}.add.xml"
            write_programs(best_signals, new, path)
            candidates.append((step, path, changed))
        print(f"iteration {iteration}: steps {', '.join(f'{s:g}' for s, _p, _c in candidates)} towards Webster with the flows of the accepted run "
              f"({candidates[0][2]} of {len(programs)} programs change)", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers // len(seeds))) as pool:
            futures = [(step, path, pool.submit(evaluate, iteration, step, path)) for step, path, _changed in candidates]
            results = [(step, path, *future.result()) for step, path, future in futures]
        winner = min(results, key=lambda item: item[2]["score"])
        for step, _path, row, _dirs in results:
            row["accepted"] = row is winner[2] and row["score"] < best_row["score"]
            record(row)
            geh_text = f"; GEH < 5 at {row['geh']['under_5_percent']} %" if row.get("geh") else ""
            print(f"  step {step:g}: {describe(row)}{geh_text}{'  <- accepted' if row['accepted'] else ''}", flush=True)
        if winner[2]["accepted"]:
            best_row, best_signals, best_dirs = winner[2], winner[1], winner[3]
            stale = 0
        else:
            stale += 1
            steps = [step / 4 for step in steps]
            print(f"  no candidate lowers the score {best_row['score']}; smaller steps next ({stale} of {args.patience})", flush=True)
            if stale >= args.patience:
                break

    write_round(out, args.round, net_file, routes, best_signals, summary, best_row, best_dirs, args.end, args.teleport, driver)
    report["accepted"] = {"iteration": best_row["iteration"], "step": best_row["step"], "signals": str(best_signals)}
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    first = next(r for r in report["iterations"] if r["iteration"] == 0)
    print()
    print(f"start:    {describe(first)}")
    print(f"accepted: iteration {best_row['iteration']} (step {best_row['step']:g}): {describe(best_row)}")
    print(f"signals: {out / 'signals.add.xml'}; open in the GUI: sumo-gui -c {out / 'behaviour' / 'best.sumocfg'}")


if __name__ == "__main__":
    main()
