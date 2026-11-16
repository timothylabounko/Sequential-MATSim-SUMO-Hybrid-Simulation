"""Repair the junctions that stayed stuck in both behaviour seeds.

The network written by the signal stage (``--source``) is only read. Short links that cannot
hold a car are joined into one junction. Lane connections are then changed:

* Brunaustrasse, Bergstrasse / Dufourplatz and Selnaustrasse lose the stub
  that vehicles were hit on.
* Thurgauerstrasse lane 0 becomes a right turn only. The through lanes stay
  on the middle lane. The actuated program is not replaced.
* Motorway 789008273 keeps one through lane per approach lane, and opens the
  extra lane from the left lane instead of forking the right lane.
* Motorway 255099140's right lane can reach the exit that only the right-hand
  carriageway could reach before.
* Motorway 1478465935's exit lane can also continue on the mainline, so a
  vehicle that missed the exit is not trapped in it.

The same pass also clears the collisions and wrong-lane teleports that
remained with the plain driver. Short Forchstrasse, Allmendstrasse and
Hochbordstrasse stubs are joined. Through traffic on Schaffhauserstrasse,
Rotbuchstrasse, Binzstrasse and Seilergraben can use the lane it is already
in. On the motorway, exit lanes keep a mainline continuation, and the
authority-only lane-change ban is removed so drivers can move across before
the split.

The joins and connections were found on the Zurich network written by the
signal stage. A join or connection whose junctions or edges are not in
``--source`` is skipped and reported. The signal programs of ``--signals`` are
carried over to the repaired network link by link, signals left without a
program get Webster's, and the repaired network is simulated once with the
SUMO default driver (``plain.*``), which the zero-teleport rounds start from.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import sumolib

from matsim_sumo.network.simulation import route_trips, simulate, write_config
from matsim_sumo.network.zero_teleport_repair import carry_signals, safe_greens, time_signals, untimed_signals

SIM = Path.home() / "zurich-sim" / "national-morning"
SEED = 23

# from-node, to-node of each stub. Joining them removes the stub.
# None of these pairs is a traffic light, so the actuated programs stay put.
JOINS = (
    ("150550261", "150550149"),  # Dufourplatz, 0.2 m, after Bergstrasse
    ("848441326", "150550261"),  # Dufourplatz, 2.5 m, before Bergstrasse
    ("cluster_5255883857_88993792", "5103538135"),  # Selnaustrasse, 3.6 m
    ("cluster_623430507_623430518", "623430504"),  # Brunaustrasse, 14 m
    ("75393811", "15998909"),  # 16 m motorway stub before 1478465935
    ("7511563535", "1679924000", "8742004156"),  # Forchstrasse, 16 m and 3.7 m
    ("27488706", "12037563620"),  # Allmendstrasse, 0.2 m
    ("5198358207", "4352614390"),  # Hochbordstrasse, 0.2 m
    # Limmattalstrasse: the 0.2 m triangle and the 6 m merge are one junction.
    ("cluster_13567357461_13567357462", "1929025814", "1929025819", "1929025821"),
    # Geeringstrasse: 0.2 m and 7.7 m stubs in front of a 62 m approach.
    ("5649674261", "12652696274", "cluster_12652696267_2179344672"),
    # General-Guisan-Quai: 0.2 m side road joining the through lanes.
    ("5199926616", "43601942"),
)

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

# (tag, from, to, fromLane, toLane). delete removes a connection; connection adds one.
CONNECTIONS = (
    # Thurgauerstrasse: the right lane no longer also goes straight.
    ("delete", "758118664#1", "524060354#2", 0, 0),
    # The middle lane feeds both through lanes.
    ("connection", "758118664#1", "524060354#2", 1, 0),
    # 789008273: stop the right lane forking into two lanes.
    ("delete", "789008273", "1474284209", 0, 1),
    ("delete", "789008273", "1474284209", 1, 2),
    ("delete", "789008273", "1474284209", 2, 3),
    ("connection", "789008273", "1474284209", 1, 1),
    ("connection", "789008273", "1474284209", 2, 2),
    ("connection", "789008273", "1474284209", 2, 3),
    # 255099140: the right lane can reach the exit on the merged carriageway.
    ("connection", "255099140", "46703860", 0, 1),
    # 1478465935: the exit lane can still continue on the motorway.
    ("connection", "1478465935", "43945815", 0, 0),
    # Schaffhauserstrasse: the main lane can enter the through lane, not only the Ringstrasse pocket.
    ("connection", "-113568249", "-566127208#1", 0, 0),
    # Rotbuchstrasse: the approach can go straight, and the lane it is forced into can too.
    ("connection", "-536824257#1", "-32242234", 0, 0),
    ("connection", "-32242234", "-32242185", 1, 0),
    # Binzstrasse: the right lane can still reach Forchstrasse.
    ("connection", "-654068862#2", "48959043", 0, 0),
    # Seilergraben: Hirschengraben can enter a through lane, not only Mühlegasse.
    ("connection", "341018763#1", "-190714064#2", 0, 0),
    # Thurgauerstrasse, ahead of the signal: the middle lane can reach the right-turn lane.
    ("connection", "603676603#0", "615489933", 1, 0),
    # Schaffhauserstrasse at the 0.2 m stub: the right lanes can continue straight.
    ("connection", "190760564#0", "8420664", 0, 1),
    ("connection", "190760564#0", "8420664", 1, 1),
    # Motorway exit lanes can still continue on the mainline.
    ("connection", "194211719", "789008272", 0, 0),
    ("connection", "128029223", "438516268", 0, 0),
    # Forchstrasse: the right lane can continue straight, not only turn off.
    ("connection", "944205056", "944205060#0", 0, 0),
    # Hochbordstrasse: the right lane can continue straight as well as turn off.
    ("connection", "40396782#0", "4403183", 0, 0),
    # Hardstrasse: the right lane of the 0.2 m stub can reach the left-turn lane.
    ("connection", "293141408", "297215099#0", 0, 2),
    # Schaffhauserstrasse: the middle lane can go straight, not only onto Ringstrasse.
    ("connection", "-566127208#1", "-549325299#2", 1, 0),
    # Limmattalstrasse: the left lane must keep a through connection after the join.
    ("connection", "118960620", "182545309#0", 2, 0),
    # Geeringstrasse: the right lane can still continue straight.
    ("connection", "285303242", "1366269194", 0, 0),
)

# Passenger cars cannot change lanes on these edges, so a lane drop at the
# junction traps them. Dropping the authority-only ban lets them move over
# on the long approach instead.
UNBAN_EDGES = (
    "1475431852",
    "1475431853",
    "1242400745",
    "46703860",
    "1242400744",
    "1242400743",
    "255099139",
    "255099140",
    "194211719",
    "194211722",
    "10559134",
    "128029223",
)


def run_netconvert(command: list[str], log_path: Path) -> None:
    completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=3600)
    log_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError((completed.stdout + completed.stderr)[-2000:])


def allow_lane_changes(net_path: Path, edges: tuple[str, ...] = UNBAN_EDGES) -> int:
    """Let ordinary vehicles change lanes on the motorway edges that banned it."""
    tree = ET.parse(net_path)
    cleared = 0
    wanted = set(edges)
    for edge in tree.getroot().iter("edge"):
        if edge.get("id") not in wanted:
            continue
        for lane in edge.findall("lane"):
            for attr in ("changeLeft", "changeRight"):
                if lane.get(attr):
                    del lane.attrib[attr]
                    cleared += 1
    tree.write(net_path, encoding="UTF-8", xml_declaration=True)
    return cleared


def join_groups(pairs: tuple[tuple[str, ...], ...]) -> list[str]:
    """One cluster per connected set. A node listed twice is otherwise ignored."""
    groups: list[set[str]] = []
    for pair in pairs:
        nodes = set(pair)
        merged = [group for group in groups if group & nodes]
        for group in merged:
            groups.remove(group)
            nodes |= group
        groups.append(nodes)
    return [" ".join(sorted(group)) for group in groups]


def present_joins(net, joins: tuple[tuple[str, ...], ...]) -> tuple[list[tuple[str, ...]], list[str]]:
    kept, skipped = [], []
    for group in joins:
        missing = [node for node in group if not net.hasNode(node)]
        if missing:
            skipped.append(f"join {' '.join(group)}: junction {', '.join(missing)} not in the network")
        else:
            kept.append(group)
    return kept, skipped


def present_connections(net, connections: tuple[tuple, ...]) -> tuple[list[tuple], list[str]]:
    kept, skipped = [], []
    for tag, from_edge, to_edge, from_lane, to_lane in connections:
        if not (net.hasEdge(from_edge) and net.hasEdge(to_edge)):
            skipped.append(f"{tag} {from_edge} -> {to_edge}: edge not in the network")
        elif from_lane >= net.getEdge(from_edge).getLaneNumber() or to_lane >= net.getEdge(to_edge).getLaneNumber():
            skipped.append(f"{tag} {from_edge}_{from_lane} -> {to_edge}_{to_lane}: lane not in the network")
        else:
            kept.append((tag, from_edge, to_edge, from_lane, to_lane))
    return kept, skipped


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=SIM / "full-traffic/signals/final.net.xml", help="Network of the signal stage")
    parser.add_argument("--signals", type=Path, help="Signal programs of --source (default: signals.add.xml beside it)")
    parser.add_argument("--trips", type=Path, default=SIM / "plans.full.trips.xml", help="Full-demand trips for the plain run")
    parser.add_argument("--out-dir", type=Path, default=SIM / "full-traffic/junction-repair/iteration3")
    parser.add_argument("--end", type=int, default=3600)
    parser.add_argument("--teleport", type=int, default=300)
    parser.add_argument("--no-simulate", action="store_true", help="Only write the network and its signals")
    args = parser.parse_args(argv)
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    signals_in = args.signals or args.source.parent / "signals.add.xml"
    joined, output = out / "joined.net.xml", out / "repaired.net.xml"

    source_net = sumolib.net.readNet(str(args.source))
    joins, skipped_joins = present_joins(source_net, JOINS)
    connections, skipped_connections = present_connections(source_net, CONNECTIONS)
    for line in skipped_joins + skipped_connections:
        print(f"skipped {line}", flush=True)

    join_path = out / "joins.nod.xml"
    groups = join_groups(tuple(joins))
    join_path.write_text(
        "<nodes>\n" + "".join(f'    <join nodes="{group}"/>\n' for group in groups) + "</nodes>\n",
        encoding="utf-8",
    )
    run_netconvert(
        ["netconvert", "-s", str(args.source), "-n", str(join_path), *NETCONVERT_OPTIONS, "-o", str(joined)],
        out / "join.netconvert.txt",
    )
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<connections>"]
    for tag, from_edge, to_edge, from_lane, to_lane in connections:
        lines.append(
            f'    <{tag} from="{from_edge}" to="{to_edge}" fromLane="{from_lane}" toLane="{to_lane}"/>'
        )
    lines += ["</connections>", ""]
    connection_path = out / "repaired.con.xml"
    connection_path.write_text("\n".join(lines), encoding="utf-8")
    run_netconvert(
        [
            "netconvert",
            "-s",
            str(joined),
            "--connection-files",
            str(connection_path),
            *NETCONVERT_OPTIONS,
            "-o",
            str(output),
        ],
        out / "connections.netconvert.txt",
    )
    cleared = allow_lane_changes(output, tuple(edge for edge in UNBAN_EDGES if source_net.hasEdge(edge)))
    print(f"repaired network: {output}: {len(groups)} joins, {len(connections)} connection edits, "
          f"{cleared} lane-change bans cleared", flush=True)

    new_net = sumolib.net.readNet(str(output))
    signals_out = out / "signals.add.xml"
    carried = carry_signals(source_net, new_net, signals_in, signals_out)
    print(f"signals: {carried['kept']} unchanged, {len(carried['remapped'])} renumbered, {len(carried['dropped'])} dropped", flush=True)
    if args.no_simulate:
        return

    routes = out / "plain.rou.xml"
    print("routing the trips on the repaired network", flush=True)
    route_trips(output, args.trips, routes)
    untimed = untimed_signals(new_net, signals_out)
    if untimed:
        timed = time_signals(output, routes, signals_out, out, untimed)
        print(f"Webster for {len(timed)} of {len(untimed)} signals without a program", flush=True)
    safe_greens(new_net, signals_out)
    print("simulating the repaired network with the SUMO default driver", flush=True)
    stats = simulate(output, routes, out / "plain.log", args.end, args.teleport, (signals_out,), seed=SEED)
    write_config(out / "plain.sumocfg", output, routes, args.end, args.teleport, (signals_out,), seed=SEED)
    print(f"teleported {stats['teleports']} of {stats['inserted']} inserted ({stats['teleported_percent']} %)", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise
