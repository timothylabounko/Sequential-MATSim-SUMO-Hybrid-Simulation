"""A whole simulated day (00:00-24:00) on the final network, for the daily count comparison.

The morning pipeline ends with ``final/final.sumocfg``: the calibrated network,
signals and driver, teleporting off, junction blockers ignored after 60 s,
collisions as warnings and rerouting every five minutes. ``route`` routes a
daily trip file on that network as the morning trips were routed
(``simulation.route_trips``: duarouter, then lane-drivable routes) and gives
every vehicle type the calibrated driver: the vTypes of the final run's routes
(``--vtypes best.rou.xml``, copied verbatim) or the best genes of the behaviour
stage (``--behaviour summary.json``). ``config`` writes a configuration with every
setting of ``final.sumocfg`` but the daily routes, the end of the day and an
edgeData output: one interval 00:00-24:00 and one per hour.

    python -m matsim_sumo.network.daily_run route --net N --trips T --vtypes B/best.rou.xml --output R
    python -m matsim_sumo.network.daily_run config --final final.sumocfg --routes R --out-dir D [--begin 0 --end 86400]
"""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from matsim_sumo.network.behaviour_calibration import GENES, with_behaviour
from matsim_sumo.network.count_check import DAY_S, edge_data_additional
from matsim_sumo.network.simulation import route_trips


def best_genes(summary: Path) -> dict[str, float]:
    return json.loads(summary.read_text(encoding="utf-8"))["best"]["genes"]


def driver_individual(genes: dict[str, float]) -> tuple[float, ...]:
    """As ``route_assignment.driver_individual``: the genes in ``GENES`` order, defaults for missing ones."""
    return tuple(genes.get(gene.name, gene.default) for gene in GENES)


VTYPE = re.compile(r'<vType\b[^>]*?\bid="([^"]+)"[^>]*?/>')


def copy_vtypes(routes_text: str, source_text: str) -> tuple[str, int]:
    """``routes_text`` with every vType replaced by the vType of the same id in ``source_text``."""
    source = {match.group(1): match.group(0) for match in VTYPE.finditer(source_text[:200_000])}
    replaced = 0

    def swap(match: re.Match) -> str:
        nonlocal replaced
        if match.group(1) not in source:
            return match.group(0)
        replaced += 1
        return source[match.group(1)]

    return VTYPE.sub(swap, routes_text), replaced


def route(net: Path, trips: Path, output: Path, summary: Path | None = None, vtypes: Path | None = None) -> dict:
    """Route ``trips``; the driver is copied from the vTypes of ``vtypes`` or else set from ``summary``'s best genes."""
    missing = route_trips(net, trips, output, timeout=6 * 3600)
    text = output.read_text(encoding="utf-8")
    if vtypes is not None:
        with vtypes.open(encoding="utf-8") as handle:
            text, replaced = copy_vtypes(text, handle.read(200_000))
        result = {"without_route": missing, "driver": f"{replaced} vTypes copied from {vtypes}"}
    else:
        genes = best_genes(summary)
        text = with_behaviour(text, driver_individual(genes))
        result = {"without_route": missing, "driver": genes}
    output.write_text(text, encoding="utf-8")
    return result


def daily_config(final: Path, routes: Path, out_dir: Path, begin: int = 0, end: int = DAY_S) -> Path:
    """``final`` with the daily routes, ``begin``-``end`` and the edgeData, statistic and summary outputs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    edge_data = out_dir / "edgedata.add.xml"
    edge_data.write_text(
        edge_data_additional(out_dir / "edgedata.xml", end, out_dir / "edgedata-hourly.xml").replace(
            'begin="0"', f'begin="{begin}"'
        ),
        encoding="utf-8",
    )
    root = ET.parse(final).getroot()
    inputs = root.find("input")
    inputs.find("route-files").set("value", str(routes.resolve()))
    additional = inputs.find("additional-files")
    additional.set("value", ",".join(filter(None, [additional.get("value", ""), str(edge_data.resolve())])))
    for name, value in (("begin", begin), ("end", end)):
        element = root.find(name)
        if element is None:
            element = ET.SubElement(root, name)
        element.set("value", str(value))
    for name, value in (
        ("statistic-output", out_dir / "sim.stats.xml"),
        ("summary-output", out_dir / "summary.xml"),
        ("log", out_dir / "sim.log"),
    ):
        ET.SubElement(root, name).set("value", str(value.resolve()))
    ET.SubElement(root, "duration-log.statistics").set("value", "true")
    ET.SubElement(root, "no-step-log").set("value", "true")
    ET.indent(root, space="    ")
    target = out_dir / "daily.sumocfg"
    ET.ElementTree(root).write(target, encoding="UTF-8", xml_declaration=True)
    return target


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    routing = sub.add_parser("route", help="Route the daily trips and set the calibrated driver")
    routing.add_argument("--net", type=Path, required=True)
    routing.add_argument("--trips", type=Path, required=True)
    driver = routing.add_mutually_exclusive_group(required=True)
    driver.add_argument("--vtypes", type=Path, help="Routes of the final run (best.rou.xml) whose vTypes are copied")
    driver.add_argument("--behaviour", type=Path, help="summary.json of the behaviour calibration (best genes)")
    routing.add_argument("--output", type=Path, required=True)
    config = sub.add_parser("config", help="Daily configuration from final.sumocfg")
    config.add_argument("--final", type=Path, required=True)
    config.add_argument("--routes", type=Path, required=True)
    config.add_argument("--out-dir", type=Path, required=True)
    config.add_argument("--begin", type=int, default=0)
    config.add_argument("--end", type=int, default=DAY_S)
    args = parser.parse_args(argv)
    if args.command == "route":
        result = route(args.net, args.trips, args.output, args.behaviour, args.vtypes)
        print(f"routed {args.trips} -> {args.output}: {result['without_route']} trips without a route; driver: {result['driver']}")
    else:
        print(daily_config(args.final, args.routes, args.out_dir, args.begin, args.end))


if __name__ == "__main__":
    main()
