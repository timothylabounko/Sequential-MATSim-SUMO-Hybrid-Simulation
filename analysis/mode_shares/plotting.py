from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

import os
import tempfile

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "matplotlib"))
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from analysis.mode_shares.common import COMPARISON_MODES, MATSIM_COLOR, MZ_COLOR


def plot_mode_share_comparison(df: pd.DataFrame, title: str, out_path: Path) -> None:
    plot_df = df.copy()
    plot_df["mode"] = pd.Categorical(plot_df["mode"], categories=COMPARISON_MODES, ordered=True)
    plot_df = plot_df.sort_values("mode")

    x = np.arange(len(plot_df))
    width = 0.36
    fig, ax = plt.subplots(figsize=(8.8, 3.5))
    bars_mz = ax.bar(x - width / 2, plot_df["Microcensus_share"], width=width, color=MZ_COLOR, label="Microcensus")
    bars_matsim = ax.bar(x + width / 2, plot_df["MATSim_share"], width=width, color=MATSIM_COLOR, label="MATSim")

    ax.set_title(title)
    ax.set_xlabel("mode")
    ax.set_ylabel("Mode Share")
    ax.set_xticks(x)
    ax.set_xticklabels(plot_df["mode"].astype(str))
    ax.grid(axis="y", linestyle="--", linewidth=0.7, alpha=0.55, zorder=3)
    ax.set_axisbelow(False)
    ax.legend(loc="upper right", frameon=True)

    ymax = max(float(plot_df[["Microcensus_share", "MATSim_share"]].max().max()), 0.01)
    ax.set_ylim(0.0, ymax * 1.22)
    for bars in (bars_mz, bars_matsim):
        for bar in bars:
            h = bar.get_height()
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                h + ymax * 0.02,
                f"{100.0 * h:.1f}%",
                ha="center",
                va="bottom",
                fontsize=8,
            )

    fig.tight_layout()
    fig.savefig(out_path, dpi=200)
    plt.close(fig)
