"""Map of simulated against counted traffic per counting cross-section.

Every cross-section of ``geh.csv`` (``geh_analysis``) is drawn on its matched
edges and as a circle whose area grows with the counted volume. Red means the
simulation carries fewer vehicles than were counted, blue more; the darker the
colour, the larger the difference. The rest of the network around the counts is
drawn in grey.

    python -m matsim_sumo.network.count_map --geh counts/geh/geh.csv --out-dir counts/geh

``--net`` defaults to the network of the first ``*.sumocfg`` found below the
counts folder (the parent of the folder of ``geh.csv``). ``--measure percent``
(default) colours by (simulated - counted) / counted, ``--measure difference``
by vehicles per hour and ``--measure geh`` by the GEH value, signed like the
difference.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from matsim_sumo.network.count_check import geh

MEASURES = {
    "percent": ("Simulated - counted [% of counted]", 100.0),
    "difference": ("Simulated - counted [veh/h]", None),
    "geh": ("GEH, negative where the simulation is lower", 15.0),
}


def read_sections(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["count"] = float(row["count"])
        row["simulated"] = float(row["simulated"])
        row["edges"] = row["edges"].split()
    return rows


def value(row: dict, measure: str) -> float:
    difference = row["simulated"] - row["count"]
    if measure == "percent":
        return 100.0 * difference / row["count"] if row["count"] else 0.0
    if measure == "difference":
        return difference
    sign = -1.0 if difference < 0 else 1.0
    return sign * geh(row["simulated"], row["count"])


def network_of(counts_dir: Path) -> Path:
    for config in sorted(counts_dir.rglob("*.sumocfg")):
        found = re.search(r'net-file value="([^"]+)"', config.read_text(encoding="utf-8"))
        if found:
            net = Path(found.group(1))
            return net if net.is_absolute() else config.parent / net
    raise SystemExit(f"no *.sumocfg with a net-file below {counts_dir}; pass --net")


def edge_shapes(net: Path) -> dict[str, list[tuple[float, float]]]:
    """Shape of every normal edge: its own shape, otherwise the shape of its first lane."""
    shapes = {}
    for _event, element in ET.iterparse(net, events=("end",)):
        if element.tag != "edge":
            continue
        if element.get("function") is None:
            shape = element.get("shape")
            if shape is None:
                lane = element.find("lane")
                shape = lane.get("shape") if lane is not None else None
            if shape:
                shapes[element.get("id")] = [tuple(map(float, point.split(","))) for point in shape.split()]
        element.clear()
    return shapes


def centre(points: list[tuple[float, float]]) -> tuple[float, float]:
    return (sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points))


def draw(rows: list[dict], shapes: dict, target: Path, measure: str, title: str, margin: float = 1500.0) -> dict:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    from matplotlib.colors import Normalize

    placed = []
    for row in rows:
        points = [point for edge in row["edges"] for point in shapes.get(edge, [])]
        if points:
            placed.append((row, centre(points)))
    if not placed:
        raise SystemExit("none of the counted edges is in the network")

    xs = [x for _, (x, _) in placed]
    ys = [y for _, (_, y) in placed]
    box = (min(xs) - margin, max(xs) + margin, min(ys) - margin, max(ys) + margin)

    def inside(points):
        return any(box[0] <= x <= box[1] and box[2] <= y <= box[3] for x, y in points)

    label, limit = MEASURES[measure]
    values = [value(row, measure) for row, _ in placed]
    if limit is None:
        limit = max(abs(v) for v in values) or 1.0
    norm = Normalize(-limit, limit)
    colours = plt.get_cmap("RdBu")

    fig, ax = plt.subplots(figsize=(10, 10))
    ax.add_collection(LineCollection(
        [points for points in shapes.values() if inside(points)], colors="0.82", linewidths=0.4, zorder=1))

    order = sorted(range(len(placed)), key=lambda i: abs(values[i]))
    segments, segment_colours = [], []
    for i in order:
        row, _ = placed[i]
        for edge in row["edges"]:
            if edge in shapes:
                segments.append(shapes[edge])
                segment_colours.append(colours(norm(values[i])))
    ax.add_collection(LineCollection(segments, colors=segment_colours, linewidths=3.0, zorder=2))

    largest = max(row["count"] for row, _ in placed)
    scatter = ax.scatter(
        [placed[i][1][0] for i in order], [placed[i][1][1] for i in order],
        s=[20 + 280 * placed[i][0]["count"] / largest for i in order],
        c=[values[i] for i in order], cmap=colours, norm=norm,
        edgecolors="black", linewidths=0.6, zorder=3,
    )
    bar = fig.colorbar(scatter, ax=ax, shrink=0.7, extend="both")
    bar.set_label(label)
    bar.ax.text(0.5, -0.09, "fewer than counted", transform=bar.ax.transAxes, ha="center", va="top", fontsize=8)
    bar.ax.text(0.5, 1.09, "more than counted", transform=bar.ax.transAxes, ha="center", va="bottom", fontsize=8)

    volumes = [v for v in (500, 1500, 3000) if v <= largest * 1.2] or [round(largest)]
    for volume in volumes:
        ax.scatter([], [], s=20 + 280 * volume / largest, facecolors="white", edgecolors="black",
                   label=f"{volume} veh/h counted")
    ax.legend(loc="lower left", fontsize=8, frameon=True, labelspacing=1.2)

    ax.set_xlim(box[0], box[1])
    ax.set_ylim(box[2], box[3])
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(title, fontsize=11)
    ax.text(0.99, 0.01, "1 km", transform=ax.transAxes, ha="right", va="bottom", fontsize=8)
    ax.plot([box[1] - 1000 - 0.01 * (box[1] - box[0]), box[1] - 0.01 * (box[1] - box[0])],
            [box[2] + 0.04 * (box[3] - box[2])] * 2, color="black", linewidth=2)

    fig.tight_layout()
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("png", "pdf"):
        fig.savefig(target.with_suffix(f".{suffix}"), dpi=220)
    plt.close(fig)
    return {
        "sections": len(placed),
        "not_in_network": len(rows) - len(placed),
        "fewer_than_counted": sum(v < 0 for v in values),
        "more_than_counted": sum(v > 0 for v in values),
        "measure": measure,
        "colour_limit": limit,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--geh", type=Path, required=True, help="geh.csv of geh_analysis")
    parser.add_argument("--net", type=Path, help="SUMO network of the counted run")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--measure", choices=sorted(MEASURES), default="percent")
    parser.add_argument("--title", default="Simulated against counted traffic, 07:00-08:00")
    args = parser.parse_args(argv)

    net = args.net or network_of(args.geh.parent.parent)
    rows = read_sections(args.geh)
    result = draw(rows, edge_shapes(net), args.out_dir / f"count-map-{args.measure}", args.measure, args.title)
    result["net"] = str(net)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
