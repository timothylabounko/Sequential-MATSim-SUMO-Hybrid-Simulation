"""Better routes before forcing: iterative assignment, then no teleports only if vehicles are still stuck.

Runs after the last repair round, on its network, signals and calibrated
driver. Neither the network nor the demand is changed.

1. Assignment. SUMO's ``duaIterate.py`` simulates the hour ``--iterations``
   times. After each run duarouter gives every trip a set of route
   alternatives with the travel times of that run, and Gawron's route choice
   moves part of the drivers to the faster ones, until travel times settle
   (dynamic user equilibrium). Every iteration uses the settings of
   ``simulation.py`` and the round's driver, without the rerouting device,
   which would replace the assigned routes at departure.
2. Choice. The iteration with the fewest teleports (then the fewest vehicles
   not inserted) gives the routes, without iteration 0 (empty-network
   shortest paths). Routes that need a lane change the markings forbid are
   replaced (``lane_routes.py``), as after every routing.
3. Check. The routes are simulated with each seed of the behaviour search,
   teleporting after ``--teleport`` seconds, and compared with the Google
   travel times of the behaviour search.
4. Fallback. If a vehicle is still teleported with any seed, the routes are
   simulated again with teleporting off, junction blockers ignored and
   collisions as warnings (``zero_teleport_final``), and vehicles standing
   still ``STUCK_S`` seconds are counted instead.
``final.sumocfg`` repeats the run that is kept: the check when it had no
teleports, otherwise the fallback.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import sumolib

from matsim_sumo.network.behaviour_calibration import (
    GENES,
    sumo_options as behaviour_options,
    sumo_travel_times,
    travel_time_error,
    with_behaviour,
)
from matsim_sumo.network.lane_routes import fix_routes
from matsim_sumo.network.national_repair import split_kinds
from matsim_sumo.network.simulation import JAM_THRESHOLD_S, read_statistics, simulate, write_config
from matsim_sumo.network.turn_evolution import parse_gridlocks, summarise
from matsim_sumo.network.zero_teleport_final import FIRST_SEED, STUCK_S, final_options, watched_run

DEMAND_NAME = "demand"
ROUTING_THREADS = 4


def driver_individual(genes: dict[str, float]) -> tuple[float, ...]:
    return tuple(genes.get(gene.name, gene.default) for gene in GENES)


def dua_command(net: Path, signals: Path, trips: Path, iterations: int, end: int, teleport: int, extra: tuple[str, ...]) -> list[str]:
    """``duaIterate.py`` with the settings of ``simulation.py`` (no rerouting device) and the round's driver."""
    tool = Path(os.environ["SUMO_HOME"]) / "tools" / "assign" / "duaIterate.py"
    options = [
        ("--seed", str(FIRST_SEED)),
        ("--tls.actuated.jam-threshold", str(JAM_THRESHOLD_S)),
        ("--ignore-route-errors", "true"),
        ("--statistic-output", "stats.xml"),
        ("--duration-log.statistics", "true"),
        *zip(extra[0::2], extra[1::2]),
    ]
    # option=value, so that a value such as -1 is not read as an option of duaIterate
    sumo_args = [f"sumo{flag}={value}" for flag, value in options]
    return [
        sys.executable,
        str(tool),
        "-n",
        str(net.resolve()),
        "-+",
        str(signals.resolve()),
        "-t",
        str(trips.resolve()),
        "-b",
        "0",
        "-e",
        str(end),
        "-l",
        str(iterations),
        "--time-to-teleport",
        str(teleport),
        "--time-to-teleport.highways",
        str(teleport),
        "--continue-on-unbuild",
        "--disable-summary",
        "--disable-tripinfos",
        "--clean-alt",
        *sumo_args,
        f"duarouter--routing-threads={ROUTING_THREADS}",
    ]


def iteration_rows(work: Path) -> list[dict]:
    rows = []
    for step_dir in sorted(path for path in work.iterdir() if path.is_dir() and re.fullmatch(r"\d{3}", path.name)):
        stats = step_dir / "stats.xml"
        if not stats.is_file():
            continue
        result = read_statistics(stats)
        rows.append(
            {
                "iteration": int(step_dir.name),
                "teleports": result["teleports"],
                "jam": result["teleports_jam"],
                "yield": result["teleports_yield"],
                "wrong_lane": result["teleports_wrongLane"],
                "collision": result["teleports_collision"],
                "inserted": result["inserted"],
                "not_inserted_percent": result["not_inserted_percent"],
                "mean_trip_s": result["trip_duration"],
                "routes": str(step_dir / f"{DEMAND_NAME}_{step_dir.name}.rou.xml.gz"),
            }
        )
    return rows


def best_iteration(rows: list[dict]) -> dict | None:
    """Fewest teleports, then fewest not inserted, of the iterations after the empty-network routes."""
    candidates = [row for row in rows if row["iteration"] > 0 and row["teleports"] is not None and Path(row["routes"]).is_file()]
    if not candidates:
        return None
    return min(candidates, key=lambda row: (row["teleports"], row["not_inserted_percent"] or 0, row["iteration"]))


def unpack_routes(source: Path, target: Path) -> Path:
    with gzip.open(source, "rb") as handle, target.open("wb") as out:
        shutil.copyfileobj(handle, out)
    return target


def check_run(net: Path, routes: Path, signals: Path, run_dir: Path, seed: int, extra: tuple[str, ...], end: int, teleport: int, google: dict) -> dict:
    """The assigned routes with teleporting on, as in the repair rounds but without the rerouting device."""
    tripinfo = run_dir / "tripinfo.xml"
    stats = simulate(
        net,
        routes,
        run_dir / "sim.log",
        end,
        teleport,
        (signals,),
        (*extra, "--tripinfo-output", str(tripinfo), "--tripinfo-output.write-unfinished", "true"),
        seed,
        rerouting=False,
    )
    by_kind = summarise(parse_gridlocks((run_dir / "sim.log").read_text(encoding="utf-8", errors="replace")))["by_kind"]
    stats["by_kind"] = by_kind
    stats["network_errors"], stats["capacity"] = split_kinds(by_kind)
    stats["travel_time"] = travel_time_error(sumo_travel_times(tripinfo, set(google)), google)
    stats["seed"] = seed
    tripinfo.unlink(missing_ok=True)
    return stats


def describe_check(result: dict) -> str:
    tt = result.get("travel_time", {})
    return (
        f"teleported {result['teleports']} ({result['teleported_percent']} % of {result['inserted']} inserted: jam "
        f"{result['teleports_jam']}, yield {result['teleports_yield']}, wrong lane {result['teleports_wrongLane']}, "
        f"collision {result['teleports_collision']}), not inserted {result['not_inserted_percent']} %, "
        f"travel-time bias {tt.get('bias_percent')} % (MRE {tt.get('mre_percent')} %)"
    )


def describe_fallback(result: dict) -> str:
    tt = result.get("travel_time", {})
    return (
        f"teleported {result['teleports']}, stood still {STUCK_S} s at a lane front {result['stuck_front']} "
        f"(further back in a queue {result['stuck_queue']}, still standing at the end {result['standing_at_end']}), "
        f"collisions {result['collisions']} (warnings only), not inserted {result['not_inserted_percent']} %, "
        f"travel-time bias {tt.get('bias_percent')} % (MRE {tt.get('mre_percent')} %)"
    )


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["empty"])
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--round", type=Path, required=True, help="Last repair round: repaired.net.xml, signals.add.xml, behaviour/")
    parser.add_argument("--trips", type=Path, required=True, help="Scaled trips with vType definitions (plans.full.trips.xml)")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--end", type=int, default=3600)
    parser.add_argument("--teleport", type=int, default=300)
    parser.add_argument("--workers", type=int, default=2, help="Seeds simulated at the same time")
    parser.add_argument("--skip-assignment", action="store_true", help="Reuse the iterations in --out-dir/assignment")
    parser.add_argument("--no-fallback", action="store_true", help="Stop after the check even if vehicles are teleported")
    args = parser.parse_args(argv)
    out = args.out_dir
    work = out / "assignment"
    work.mkdir(parents=True, exist_ok=True)
    behaviour = args.round / "behaviour"
    summary = json.loads((behaviour / "summary.json").read_text(encoding="utf-8"))
    best = summary["best"]
    seeds = summary.get("seeds") or [FIRST_SEED]
    net, signals = args.round / "repaired.net.xml", args.round / "signals.add.xml"
    individual = driver_individual(best["genes"])
    driver = tuple(behaviour_options(individual))
    google_file = behaviour / "google-times.json"
    google = {}
    if google_file.is_file():
        google = {k: v for k, v in json.loads(google_file.read_text(encoding="utf-8")).items() if not k.startswith("_") and v}

    if not args.skip_assignment:
        trips = work / f"{DEMAND_NAME}.trips.xml"
        trips.write_text(with_behaviour(args.trips.read_text(encoding="utf-8"), individual), encoding="utf-8")
        command = dua_command(net, signals, trips, args.iterations, args.end, args.teleport, driver)
        print(f"1 iterative assignment, {args.iterations} iterations ({best['label']} driver, no rerouting device)", flush=True)
        with (work / "duaIterate.txt").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in process.stdout:
                log.write(line)
                if line.startswith("> Executing step"):
                    done = iteration_rows(work)
                    if done:
                        row = done[-1]
                        print(
                            f"  iteration {row['iteration']}: teleported {row['teleports']} (jam {row['jam']}, yield {row['yield']}, "
                            f"wrong lane {row['wrong_lane']}, collision {row['collision']}), not inserted {row['not_inserted_percent']} %",
                            flush=True,
                        )
            if process.wait() != 0:
                raise SystemExit(f"duaIterate failed; see {work / 'duaIterate.txt'}")

    rows = iteration_rows(work)
    write_csv(out / "assignment.csv", rows)
    if rows:
        row = rows[-1]
        print(f"  iteration {row['iteration']}: teleported {row['teleports']}, not inserted {row['not_inserted_percent']} %", flush=True)
    chosen = best_iteration(rows)
    if chosen is None:
        raise SystemExit(f"no finished iteration with routes in {work}")
    print(f"2 routes of iteration {chosen['iteration']} ({chosen['teleports']} teleports in its run)", flush=True)
    routes = unpack_routes(Path(chosen["routes"]), out / "assigned.rou.xml")
    lanes = fix_routes(net, routes)
    print(f"  {lanes['undrivable']} of {lanes['routes']} routes need a banned lane change, {lanes['rerouted']} replaced", flush=True)

    print(f"3 check with seeds {', '.join(map(str, seeds))}, teleporting after {args.teleport} s", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            seed: pool.submit(check_run, net, routes, signals, out / f"check-seed{seed}", seed, driver, args.end, args.teleport, google)
            for seed in seeds
        }
        checks = {seed: future.result() for seed, future in futures.items()}
    for seed, result in checks.items():
        print(f"  seed {seed}: {describe_check(result)}", flush=True)
    report = {
        "round": str(args.round),
        "driver": best["label"],
        "seeds": seeds,
        "iterations": rows,
        "chosen_iteration": chosen["iteration"],
        "lane_routes": {k: v for k, v in lanes.items() if k != "blocked"},
        "check": {str(seed): result for seed, result in checks.items()},
    }

    teleported = sum(result["teleports"] or 0 for result in checks.values())
    if teleported == 0 or args.no_fallback:
        config = write_config(out / "final.sumocfg", net, routes, args.end, args.teleport, (signals,), driver, FIRST_SEED, rerouting=False)
        report["kept"] = "check" if teleported == 0 else "check (vehicles still teleported; fallback skipped)"
    else:
        extra = final_options(best["genes"])
        print(f"4 still {teleported} teleports over the seeds: again without teleports ({' '.join(extra)})", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                seed: pool.submit(watched_run, net, routes, (signals,), out / f"no-teleport-seed{seed}", seed, extra, args.end, False)
                for seed in seeds
            }
            fallbacks = {seed: future.result() for seed, future in futures.items()}
        final_net = sumolib.net.readNet(str(net))
        for seed, result in fallbacks.items():
            run_dir = out / f"no-teleport-seed{seed}"
            tripinfo = run_dir / "tripinfo.xml"
            result["travel_time"] = travel_time_error(sumo_travel_times(tripinfo, set(google)), google)
            tripinfo.unlink(missing_ok=True)
            stuck_rows = result.pop("stuck")
            for row in stuck_rows:
                edge_id = row["lane"].rsplit("_", 1)[0]
                row["street"] = final_net.getEdge(edge_id).getName() if final_net.hasEdge(edge_id) else ""
            write_csv(run_dir / "stuck.csv", stuck_rows)
        report["fallback"] = {str(seed): result for seed, result in fallbacks.items()}
        config = write_config(out / "final.sumocfg", net, routes, args.end, -1, (signals,), extra, FIRST_SEED, rerouting=False)
        report["kept"] = "fallback"
    report["gui_config"] = str(config)
    (out / "assignment.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print()
    tt = best.get("travel_time", {})
    print(
        f"{args.round.name} {best['label']} (en-route rerouting, mean of the seeds): teleported {best['teleports']} "
        f"({best['teleported_percent']} %), not inserted {best['not_inserted_percent']} %, travel-time bias {tt.get('bias_percent')} %"
    )
    for seed in seeds:
        print(f"seed {seed} assigned routes: {describe_check(checks[seed])}")
        if "fallback" in report:
            print(f"seed {seed} assigned routes, no teleports: {describe_fallback(report['fallback'][str(seed)])}")
    print(f"kept: {report['kept']}")
    print(f"open in the GUI: sumo-gui -c {config}")


if __name__ == "__main__":
    main()
