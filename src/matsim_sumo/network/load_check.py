"""Simulate one network with the scaled National demand and list where vehicles stop.

Used for the network before the repair, so the full-traffic run of the
repaired network has something to be compared with. Nothing is edited.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import sumolib

from matsim_sumo.network.google_turn_search import gridlock_count, inserted_vehicles
from matsim_sumo.network.national_repair import _unroutable, describe_gridlocks, split_kinds, teleport_share
from matsim_sumo.network.turn_evolution import parse_gridlocks, summarise


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--net", type=Path, required=True)
    parser.add_argument("--routes", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--end", type=int, default=3600)
    parser.add_argument("--teleport", type=int, default=120, help="Seconds without moving before a vehicle counts as stuck")
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args(argv)

    log = args.work_dir / "sim.log"
    score = gridlock_count(args.net, args.routes, log, args.end, args.teleport, statistics=True)
    text = log.read_text(encoding="utf-8", errors="replace")
    by_kind = summarise(parse_gridlocks(text))["by_kind"]
    unroutable = _unroutable(log)
    network, capacity = split_kinds(by_kind)
    inserted = inserted_vehicles(log)
    rows = describe_gridlocks(sumolib.net.readNet(str(args.net)), text)
    summary = {
        "net": str(args.net),
        "stuck": score - unroutable,
        "unroutable": unroutable,
        "inserted": inserted,
        "teleported_percent": teleport_share(score - unroutable, inserted),
        "teleport_after_s": args.teleport,
        "by_kind": by_kind,
        "network_errors": network,
        "capacity": capacity,
        "gridlocks": rows,
    }
    (args.work_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"stuck vehicles: {score - unroutable} {by_kind}")
    print(f"unroutable trips: {unroutable}")
    print(f"teleported share: {summary['teleported_percent']} % of {inserted} inserted vehicles")
    print(f"  network errors (wrong lane, no connection): {network}")
    print(f"  capacity (jam, yield) and other: {capacity}")
    print("vehicles  approach -> junction with  (what happened)")
    for row in rows[: args.top]:
        print(f"{row['vehicles']:8d}  {row['approach']} -> {row['junction_with']}  ({row['kinds']})")


if __name__ == "__main__":
    main()
