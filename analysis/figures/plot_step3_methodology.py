from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(".matplotlib-cache").resolve()))
os.environ.setdefault("MPLBACKEND", "Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Polygon


ROOT = Path(__file__).resolve().parents[2]
OUT_PNG = ROOT / "docs" / "step3_methodology.png"
OUT_PDF = ROOT / "docs" / "step3_methodology.pdf"


def _arrow(ax, start: tuple[float, float], end: tuple[float, float], color: str = "#333333") -> None:
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=14,
            linewidth=1.8,
            color=color,
            shrinkA=2,
            shrinkB=2,
        )
    )


def _box(ax, xy: tuple[float, float], text: str, face: str, edge: str = "#394047") -> None:
    x, y = xy
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            2.15,
            0.72,
            boxstyle="round,pad=0.05,rounding_size=0.04",
            linewidth=1.3,
            edgecolor=edge,
            facecolor=face,
        )
    )
    ax.text(x + 1.075, y + 0.36, text, ha="center", va="center", fontsize=9.5, color="#1f252b")


def main() -> None:
    fig = plt.figure(figsize=(11.4, 5.8), facecolor="white")
    gs = fig.add_gridspec(1, 2, width_ratios=[1.28, 1.0], wspace=0.12)
    ax = fig.add_subplot(gs[0, 0])
    flow = fig.add_subplot(gs[0, 1])

    fig.suptitle("Step 3: SUMO endpoint map matching", fontsize=16, fontweight="bold", y=0.96)

    ax.set_xlim(0, 10)
    ax.set_ylim(0, 6)
    ax.set_aspect("equal")
    ax.axis("off")

    route = np.array(
        [
            [1.0, 1.3],
            [2.3, 2.1],
            [3.8, 2.75],
            [5.2, 3.05],
            [6.8, 3.55],
            [8.9, 4.35],
        ]
    )
    upper = route + np.array([0.0, 0.62])
    lower = route[::-1] - np.array([0.0, 0.62])
    corridor = np.vstack([upper, lower])
    ax.add_patch(
        Polygon(corridor, closed=True, facecolor="#d9ecf7", edgecolor="#76a9c9", linewidth=1.2, alpha=0.85)
    )
    ax.plot(route[:, 0], route[:, 1], color="#276fbf", linewidth=3.0, linestyle=(0, (7, 4)), zorder=4)
    _arrow(ax, tuple(route[-2]), tuple(route[-1]), "#276fbf")

    sumo_edges = [
        ((1.2, 1.52), (2.4, 2.22), "#279b55", 4.2, "selected start"),
        ((2.55, 2.22), (3.65, 2.7), "#89939e", 2.7, "candidate"),
        ((3.85, 2.9), (5.15, 3.16), "#89939e", 2.7, "candidate"),
        ((5.3, 3.26), (6.65, 3.68), "#89939e", 2.7, "candidate"),
        ((6.9, 3.74), (8.45, 4.33), "#279b55", 4.2, "selected end"),
        ((4.15, 1.55), (5.7, 1.4), "#c94b4b", 2.5, "rejected"),
        ((6.1, 4.75), (4.9, 4.4), "#c94b4b", 2.5, "wrong direction"),
    ]
    for start, end, color, lw, _ in sumo_edges:
        ax.plot([start[0], end[0]], [start[1], end[1]], color=color, linewidth=lw, solid_capstyle="round", zorder=5)
        _arrow(ax, start, end, color)

    ax.axvline(0.86, color="#555555", linewidth=1.3, linestyle=(0, (4, 4)))
    ax.axvline(9.02, color="#555555", linewidth=1.3, linestyle=(0, (4, 4)))
    ax.text(0.86, 5.28, "entry\nboundary", ha="center", va="bottom", fontsize=8.5, color="#333333")
    ax.text(9.02, 5.28, "exit\nboundary", ha="center", va="bottom", fontsize=8.5, color="#333333")

    ax.scatter([1.2, 8.45], [1.52, 4.33], s=70, color="#279b55", zorder=7)
    ax.text(1.05, 0.72, "from_edge", ha="left", va="center", fontsize=10, color="#1f6f3f", fontweight="bold")
    ax.text(8.05, 4.86, "to_edge", ha="left", va="center", fontsize=10, color="#1f6f3f", fontweight="bold")

    legend_x = 1.35
    legend_y = 5.72
    legend_gap = 0.36
    ax.plot([legend_x, legend_x + 0.55], [legend_y, legend_y], color="#276fbf", linewidth=2.5, linestyle=(0, (7, 4)))
    ax.text(legend_x + 0.72, legend_y, "MATSim route", va="center", fontsize=8.8)
    ax.add_patch(
        Polygon(
            [
                [legend_x, legend_y - legend_gap - 0.11],
                [legend_x + 0.55, legend_y - legend_gap - 0.11],
                [legend_x + 0.55, legend_y - legend_gap + 0.11],
                [legend_x, legend_y - legend_gap + 0.11],
            ],
            facecolor="#d9ecf7",
            edgecolor="#76a9c9",
        )
    )
    ax.text(legend_x + 0.72, legend_y - legend_gap, "route corridor", va="center", fontsize=8.8)
    ax.plot(
        [legend_x + 3.1, legend_x + 3.65],
        [legend_y, legend_y],
        color="#279b55",
        linewidth=4.2,
        solid_capstyle="round",
    )
    ax.text(legend_x + 3.82, legend_y, "selected SUMO edge", va="center", fontsize=8.8)
    ax.plot(
        [legend_x + 3.1, legend_x + 3.65],
        [legend_y - legend_gap, legend_y - legend_gap],
        color="#c94b4b",
        linewidth=3.0,
        solid_capstyle="round",
    )
    ax.text(legend_x + 3.82, legend_y - legend_gap, "rejected edge", va="center", fontsize=8.8)

    ax.text(
        5.0,
        0.28,
        "Only corridor candidates are tested.\nDirection and metadata refine the final endpoint edges.",
        ha="center",
        va="center",
        fontsize=9.2,
        color="#30363d",
    )

    flow.set_xlim(0, 3.4)
    flow.set_ylim(0, 6)
    flow.axis("off")

    _box(flow, (0.62, 4.92), "1. Build route corridor", "#d9ecf7")
    _box(flow, (0.62, 3.72), "2. Keep SUMO candidates", "#eef2f5")
    _box(flow, (0.62, 2.52), "3. Select first / last edge", "#dcf3e4")
    _box(flow, (0.62, 1.32), "4. Drop invalid matches", "#f7e0df")
    _box(flow, (0.62, 0.12), "5. Write matched trips", "#f4f0d7")

    for y0, y1 in [(4.88, 4.48), (3.68, 3.28), (2.48, 2.08), (1.28, 0.88)]:
        _arrow(flow, (1.7, y0), (1.7, y1), "#394047")

    flow.text(
        2.98,
        4.1,
        "Filters:\nspace\norientation\nroad type\nOSM/name\nrouteability",
        ha="left",
        va="center",
        fontsize=9,
        color="#30363d",
        linespacing=1.35,
    )
    flow.text(
        2.98,
        1.05,
        "Diagnostics:\ndistance percentiles\ndrop reasons",
        ha="left",
        va="center",
        fontsize=9,
        color="#30363d",
        linespacing=1.35,
    )

    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PNG, dpi=220, bbox_inches="tight")
    fig.savefig(OUT_PDF, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {OUT_PNG}")
    print(f"Wrote {OUT_PDF}")


if __name__ == "__main__":
    main()
