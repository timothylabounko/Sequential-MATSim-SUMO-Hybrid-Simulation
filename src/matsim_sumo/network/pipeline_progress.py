"""Teleported vehicles after every iteration of the zero-teleport pipeline.

``teleports DIR`` prints the teleport count of one stage folder, which
``scripts/mac_zero_teleport_pipeline.sh`` uses to decide whether to go on:

* a behaviour search (``DIR/behaviour/summary.json``): its best setting, the
  mean of its seeds;
* a plain run (``DIR/plain.stats.xml``);
* a route assignment (``DIR/assignment.json``): the check, mean of its seeds;
* a network repair (``DIR/summary.json`` of ``national_repair``): its final run;
* the no-teleport run (``DIR/final.json``): always 0.

``table`` writes ``progress.csv`` with one row per iteration of all stages:
the demand level, teleported vehicles, inserted vehicles, their share, and the
lowest share reached so far at that demand level. Shares at different demand
levels are not compared, so the lowest share starts again at every level.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

from matsim_sumo.network.simulation import read_statistics


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def routed_vehicles(work_dir: Path) -> int | None:
    routes = work_dir / "final.rou.xml"
    if not routes.is_file():
        return None
    return len(re.findall(r"<vehicle\b", routes.read_text(encoding="utf-8", errors="replace")))


def teleports(stage_dir: Path) -> float:
    if (stage_dir / "final.json").is_file():
        return 0.0
    if (stage_dir / "assignment.json").is_file():
        check = _json(stage_dir / "assignment.json")["check"]
        return _mean([float(run["teleports"] or 0) for run in check.values()])
    if (stage_dir / "behaviour" / "summary.json").is_file():
        return float(_json(stage_dir / "behaviour" / "summary.json")["best"]["teleports"])
    if (stage_dir / "plain.stats.xml").is_file():
        return float(read_statistics(stage_dir / "plain.stats.xml")["teleports"])
    if (stage_dir / "summary.json").is_file():
        summary = _json(stage_dir / "summary.json")
        if "final_stuck" in summary:
            return float(summary["final_stuck"])
    raise SystemExit(f"no stage result in {stage_dir}")


def _row(stage: int, step: str, teleported: float | None, inserted: float | None) -> dict:
    share = round(100.0 * teleported / inserted, 2) if teleported is not None and inserted else None
    return {"stage": stage, "step": step, "teleported": teleported, "inserted": inserted, "teleported_percent": share}


def repair_rows(stage: int, work_dir: Path) -> list[dict]:
    summary = _json(work_dir / "summary.json")
    routed = routed_vehicles(work_dir)
    return [
        _row(stage, f"round {r['round']}", r.get("stuck"), r.get("inserted") or routed)
        for r in summary.get("rounds", [])
    ]


def level_percent(work_dir: Path) -> int | None:
    """Demand level of a repair folder named like ``level-025``."""
    match = re.search(r"(\d+)$", work_dir.name)
    return int(match.group(1)) if match else None


def table(args) -> list[dict]:
    rows: list[dict] = []
    for index, work_dir in enumerate(getattr(args, "levels", None) or []):
        percent = level_percent(work_dir)
        for row in repair_rows(1 if index == 0 else 2, work_dir):
            row["demand_percent"] = percent
            row["step"] = f"{percent} % demand, {row['step']}"
            rows.append(row)
    if args.stage1:
        rows += [dict(row, demand_percent="sample") for row in repair_rows(1, args.stage1)]
    if args.stage2:
        rows += [dict(row, demand_percent=100) for row in repair_rows(2, args.stage2)]
    first_full = len(rows)
    if args.signals and (args.signals / "summary.json").is_file():
        summary = _json(args.signals / "summary.json")
        result = summary["results"][summary["variant"]]
        rows.append(_row(3, "signal timing", result["teleports"], result["inserted"]))
    if args.behaviour and (args.behaviour / "summary.json").is_file():
        best = _json(args.behaviour / "summary.json")["best"]
        rows.append(_row(3, "driver behaviour (best)", best["teleports"], best["inserted"]))
    if args.junctions and (args.junctions / "plain.stats.xml").is_file():
        stats = read_statistics(args.junctions / "plain.stats.xml")
        rows.append(_row(3, "persistent junctions", stats["teleports"], stats["inserted"]))
    for index, round_dir in enumerate(args.rounds or [], 1):
        summary_path = round_dir / "behaviour" / "summary.json"
        if summary_path.is_file():
            best = _json(summary_path)["best"]
            rows.append(_row(4, f"zero-teleport round {index}", best["teleports"], best["inserted"]))
    if args.assignment and (args.assignment / "assignment.json").is_file():
        report = _json(args.assignment / "assignment.json")
        for item in report["iterations"]:
            rows.append(_row(5, f"assignment iteration {item['iteration']}", item["teleports"], item["inserted"]))
        check = list(report["check"].values())
        rows.append(_row(5, "check of the chosen routes", _mean([r["teleports"] for r in check]), _mean([r["inserted"] for r in check])))
    if args.final and (args.final / "final.json").is_file():
        results = list(_json(args.final / "final.json")["results"].values())
        inserted = _mean([r["inserted"] for r in results])
        row = _row(6, "teleporting off", 0.0, inserted)
        front = _mean([r["stuck_front"] for r in results])
        row["stood_still_300s"] = front
        row["stood_still_percent"] = round(100.0 * front / inserted, 2) if inserted else None
        rows.append(row)
    for row in rows[first_full:]:
        row["demand_percent"] = 100
    lowest: dict = {}
    for number, row in enumerate(rows, 1):
        row["iteration"] = number
        level = row["demand_percent"]
        share = row["teleported_percent"]
        if share is not None:
            lowest[level] = share if lowest.get(level) is None else min(lowest[level], share)
        row["lowest_so_far_percent"] = lowest.get(level)
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("teleports", help="Teleport count of one stage folder")
    one.add_argument("stage_dir", type=Path)
    many = commands.add_parser("table", help="progress.csv over all stages")
    for name in ("stage1", "stage2", "signals", "behaviour", "junctions", "assignment", "final"):
        many.add_argument(f"--{name}", type=Path)
    many.add_argument("--levels", type=Path, nargs="*", help="Repair folders of the demand levels in order (level-NNN)")
    many.add_argument("--rounds", type=Path, nargs="*", help="Zero-teleport round folders in order")
    many.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "teleports":
        print(f"{teleports(args.stage_dir):g}")
        return
    rows = table(args)
    fields = ["iteration", "stage", "demand_percent", "step", "teleported", "inserted", "teleported_percent",
              "lowest_so_far_percent", "stood_still_300s", "stood_still_percent"]
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    for row in rows:
        share = "-" if row["teleported_percent"] is None else f"{row['teleported_percent']:.2f} %"
        extra = f", stood still 300 s: {row['stood_still_percent']} %" if "stood_still_percent" in row else ""
        print(f"  {row['iteration']:2d}  stage {row['stage']}  {row['step']:36s} teleported {share:>8s}"
              f"  (lowest so far {row['lowest_so_far_percent']} %){extra}")
    print(f"table: {args.output}")


if __name__ == "__main__":
    main()
