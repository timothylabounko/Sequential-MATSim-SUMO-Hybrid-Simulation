from pathlib import Path
import os

os.environ.setdefault("MPLCONFIGDIR", "/private/tmp/matplotlib")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

MODELS = (
    ("matsim", "MATSim", "#1f77b4"),
    ("sumo", "SUMO", "#d62728"),
)


def _frame(data):
    return pd.read_csv(data) if isinstance(data, (str, Path)) else data.copy()


def _fit_label(x, y):
    if len(x) < 2:
        return None
    m, b = np.polyfit(x, y, 1)
    r = np.corrcoef(x, y)[0, 1]
    sign = "+" if b >= 0 else "-"
    return m, b, f"{m:.2f}x {sign} {abs(b):.2f}, R={r:.2f}"


def plot_scatter(data, out_path, label="All day", sumo_label="SUMO"):
    df = _frame(data)
    fig, ax = plt.subplots(figsize=(5.8, 5.4), constrained_layout=True)

    max_vals = []
    fit_labels = []
    for key, name, color in MODELS:
        display_name = sumo_label if key == "sumo" else name
        cols = ["tomtom_travel_time_min", f"{key}_travel_time_min"]
        if not set(cols).issubset(df.columns):
            continue
        xy = df[cols].apply(pd.to_numeric, errors="coerce").dropna()
        if xy.empty:
            continue
        max_vals.extend([xy[cols[0]].max(), xy[cols[1]].max()])
        ax.scatter(xy[cols[0]], xy[cols[1]], s=16, alpha=0.45, linewidth=0, color=color, label=f"{display_name} (n={len(xy)})")
        fit = _fit_label(xy[cols[0]].to_numpy(), xy[cols[1]].to_numpy())
        if fit:
            m, b, text = fit
            fit_labels.append((display_name, color, m, b, text))

    limit = max(max_vals) * 1.05 if max_vals else 1
    xx = np.array([0, limit])
    for _, color, m, b, _ in fit_labels:
        ax.plot(xx, m * xx + b, color=color, linewidth=1.8)
    ax.plot([0, limit], [0, limit], color="black", linewidth=0.9, label="1:1")
    ax.set_xlim(0, limit)
    ax.set_ylim(0, limit)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("TomTom travel time [min]")
    ax.set_ylabel("Model travel time [min]")
    ax.set_title(f"Travel Time Comparison - {label}")
    ax.grid(True, alpha=0.25)
    ax.legend(frameon=False, loc="upper left")
    if fit_labels:
        ax.text(0.98, 0.035, "\n\n".join(" " * 28 for _ in fit_labels), transform=ax.transAxes,
                ha="right", va="bottom", fontsize=9,
                bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.75, "pad": 3})
        for i, (name, color, _, _, fit) in enumerate(reversed(fit_labels)):
            ax.text(0.98, 0.04 + i * 0.055, f"{name}: {fit}", transform=ax.transAxes,
                    ha="right", va="bottom", fontsize=9, color=color)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=250)
    plt.close(fig)
    return out_path
