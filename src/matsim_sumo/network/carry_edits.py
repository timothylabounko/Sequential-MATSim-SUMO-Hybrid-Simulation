"""Carry the edits of an earlier repair run onto another network.

The repair rounds only change connections (turns added or deleted, lanes
connected, OpenStreetMap continuations) and lift lane-change bans. The edits
of a run are therefore the difference between the network it started from
(``--base``) and its best network (``--edited``). Each of them is applied to
``--onto`` (the network of the level below after its signal, junction and
behaviour stages) where it still fits: a connection is added when both of
its lanes exist and it is missing, deleted when it is there, and a ban is
lifted on an edge that still has one. Everything else the stages changed is
kept. The repair simulates ``--onto`` without the carried edits as round 0
(``national_repair --fallback-net``) and keeps the better of the two.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from matsim_sumo.network.google_turn_search import apply_patch, lift_lane_bans

CONNECTION_TAG = re.compile(r"<connection\b[^>]*>")
LANE_TAG = re.compile(r'<lane id="([^"]+)_(\d+)"[^>]*>')
LANE_CHANGE_ATTR = re.compile(r' change(?:Left|Right)="[^"]*"')
ATTRIBUTE = re.compile(r'(\w+)="([^"]*)"')

Connection = tuple[str, str, int, int]


def connections(text: str) -> set[Connection]:
    """Connections between normal edges: (from, to, from lane, to lane)."""
    found: set[Connection] = set()
    for match in CONNECTION_TAG.finditer(text):
        attributes = dict(ATTRIBUTE.findall(match.group(0)))
        if attributes.get("from", ":").startswith(":") or "fromLane" not in attributes:
            continue
        found.add((attributes["from"], attributes["to"], int(attributes["fromLane"]), int(attributes["toLane"])))
    return found


def lanes(text: str) -> set[tuple[str, int]]:
    return {(edge, int(index)) for edge, index in LANE_TAG.findall(text) if not edge.startswith(":")}


def banned_lanes(text: str) -> set[tuple[str, int]]:
    return {
        (match.group(1), int(match.group(2)))
        for match in LANE_TAG.finditer(text)
        if not match.group(1).startswith(":") and LANE_CHANGE_ATTR.search(match.group(0))
    }


def edits_between(base_text: str, edited_text: str) -> dict:
    base, edited = connections(base_text), connections(edited_text)
    lifted = banned_lanes(base_text) - banned_lanes(edited_text)
    return {
        "added": sorted(edited - base),
        "deleted": sorted(base - edited),
        "bans_lifted": sorted({edge for edge, _index in lifted}),
    }


def best_network(level_dir: Path, network_weight: float = 10.0) -> Path:
    """The best network of a repair folder: ``final.net.xml`` of a finished run, otherwise the round
    whose census has the lowest score (network errors weighted), the earlier round on a tie."""
    final = level_dir / "final.net.xml"
    if (level_dir / "summary.json").is_file() and final.is_file():
        return final
    from matsim_sumo.network.turn_evolution import parse_gridlocks, weighted_score

    best: tuple[float, int, Path] | None = None
    for round_dir in level_dir.glob("round*"):
        census, net = round_dir / "census.log", round_dir / "osm.net.xml"
        number = round_dir.name[len("round") :]
        if not (number.isdigit() and census.is_file() and net.is_file()):
            continue
        routing = census.with_suffix(".routing.txt")
        unroutable = routing.read_text(encoding="utf-8", errors="replace").count("has no valid route") if routing.is_file() else 0
        score = weighted_score(parse_gridlocks(census.read_text(encoding="utf-8", errors="replace")), unroutable, network_weight)
        if best is None or (score, int(number)) < (best[0], best[1]):
            best = (score, int(number), net)
    if best is None:
        raise FileNotFoundError(f"{level_dir} has no finished round")
    return best[2]


def carry(base: Path, edited: Path, onto: Path, output: Path) -> dict:
    """Write ``onto`` with the edits from ``base`` to ``edited``; returns what was carried and what did not fit."""
    edits = edits_between(base.read_text(encoding="utf-8"), edited.read_text(encoding="utf-8"))
    target_text = onto.read_text(encoding="utf-8")
    present, target_lanes, target_bans = connections(target_text), lanes(target_text), banned_lanes(target_text)
    add = [c for c in edits["added"] if c not in present and (c[0], c[2]) in target_lanes and (c[1], c[3]) in target_lanes]
    delete = [c for c in edits["deleted"] if c in present]
    lift = [edge for edge in edits["bans_lifted"] if any(lane[0] == edge for lane in target_bans)]
    output.parent.mkdir(parents=True, exist_ok=True)
    if add or delete:
        lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<connections>"]
        for tag, items in (("connection", add), ("delete", delete)):
            for from_edge, to_edge, from_lane, to_lane in items:
                lines.append(
                    f'    <{tag} from="{from_edge}" to="{to_edge}" fromLane="{from_lane}" toLane="{to_lane}"/>'
                )
        lines += ["</connections>", ""]
        apply_patch(onto, "\n".join(lines), output)
    else:
        shutil.copyfile(onto, output)
    if lift:
        output.write_text(lift_lane_bans(output.read_text(encoding="utf-8"), set(lift)), encoding="utf-8")
    return {
        "base": str(base),
        "edited": str(edited),
        "onto": str(onto),
        "output": str(output),
        "connections_added": [list(c) for c in add],
        "connections_deleted": [list(c) for c in delete],
        "bans_lifted": lift,
        "already_there": sum(c in present for c in edits["added"])
        + sum(c not in present for c in edits["deleted"])
        + sum(edge not in lift for edge in edits["bans_lifted"]),
        "did_not_fit": [list(c) for c in edits["added"] if c not in add and c not in present],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True, help="Network the earlier run started from")
    parser.add_argument(
        "--edited", type=Path, required=True, help="Best network of the earlier run, or its repair folder (best round)"
    )
    parser.add_argument("--onto", type=Path, required=True, help="Network the edits are carried onto (read only)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--network-weight", type=float, default=10.0, help="Score of a network error, to pick the best round")
    args = parser.parse_args(argv)
    edited = best_network(args.edited, args.network_weight) if args.edited.is_dir() else args.edited
    report = carry(args.base, edited, args.onto, args.output)
    args.output.with_suffix(".carried.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"carried from {args.edited}: {len(report['connections_added'])} connections added, "
        f"{len(report['connections_deleted'])} deleted, lane-change bans lifted on {len(report['bans_lifted'])} edges; "
        f"{report['already_there']} already in the network; "
        f"{len(report['did_not_fit'])} added connections whose lanes are gone"
    )


if __name__ == "__main__":
    main()
