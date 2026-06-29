from pathlib import Path
import os

os.environ.setdefault("MPLCONFIGDIR", "/private/tmp/matplotlib")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd

try:
    from matsim_sumo.visualization.crossing_types import TYPE_COLORS
except ImportError:
    TYPE_COLORS = {
        "Passing-through": "#a6611a",
        "Inside": "#dfc27d",
        "Entering": "#80cdc1",
        "Leaving": "#018571",
    }

MODELS = (
    ("matsim", "MATSim", TYPE_COLORS["Entering"]),
    ("sumo", "SUMO", TYPE_COLORS["Passing-through"]),
)

SUMMARY_TABLE_COLUMNS = [
    "Source",
    "N",
    "Mean TT [min]",
    "Mean dist [km]",
    "Abs. mean [min]",
    "RMSE [min]",
    "Rel. mean [%]",
    "MAE [%]",
]

SUMMARY_TABLE_ROWS = [
    {
        "Source": "TomTom (reference)",
        "N": "2500",
        "Mean TT [min]": "7.0",
        "Mean dist [km]": "1.70",
        "Abs. mean [min]": "\u2014",
        "RMSE [min]": "\u2014",
        "Rel. mean [%]": "\u2014",
        "MAE [%]": "\u2014",
    },
    {
        "Source": "MATSim",
        "N": "2500",
        "Mean TT [min]": "5.7",
        "Mean dist [km]": "1.61",
        "Abs. mean [min]": "\u22121.3",
        "RMSE [min]": "1.9",
        "Rel. mean [%]": "\u221218.3",
        "MAE [%]": "22.2",
    },
    {
        "Source": "SUMO",
        "N": "2500",
        "Mean TT [min]": "7.4",
        "Mean dist [km]": "1.74",
        "Abs. mean [min]": "+0.3",
        "RMSE [min]": "1.6",
        "Rel. mean [%]": "+5.9",
        "MAE [%]": "17.8",
    },
]


def _frame(data):
    return pd.read_csv(data) if isinstance(data, (str, Path)) else data.copy()


def _errors(df):
    rows = []
    for key, label, color in MODELS:
        time_cols = ["tomtom_travel_time_min", f"{key}_travel_time_min"]
        if not set(time_cols).issubset(df.columns):
            continue
        distance_col = f"{key}_distance_km" if f"{key}_distance_km" in df.columns else "tomtom_distance_km"
        cols = time_cols + ([distance_col] if distance_col in df.columns else [])
        tmp = df[cols].apply(pd.to_numeric, errors="coerce").dropna(subset=time_cols)
        tmp = tmp[tmp["tomtom_travel_time_min"] > 0].copy()
        if tmp.empty:
            continue
        tmp["model"] = label
        tmp["color"] = color
        tmp["tomtom_min"] = tmp["tomtom_travel_time_min"]
        tmp["model_min"] = tmp[time_cols[1]]
        tmp["diff_min"] = tmp[time_cols[1]] - tmp[time_cols[0]]
        tmp["diff_pct"] = 100 * tmp["diff_min"] / tmp[time_cols[0]]
        tmp["distance_km"] = tmp[distance_col] if distance_col in tmp.columns else np.nan
        tmp = tmp[np.isfinite(tmp["diff_min"]) & np.isfinite(tmp["diff_pct"])]
        rows.append(tmp[["model", "color", "tomtom_min", "model_min", "diff_min", "diff_pct", "distance_km"]])
    cols = ["model", "color", "tomtom_min", "model_min", "diff_min", "diff_pct", "distance_km"]
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=cols)


def _rmse(values):
    return float(np.sqrt(np.mean(np.square(values)))) if len(values) else np.nan


def _fmt_signed(value):
    return f"{value:+.1f}".replace("-", "\u2212")


def summary_table_rows_from_data(data):
    df = _frame(data)
    rows = []

    tomtom_cols = ["tomtom_travel_time_min"]
    if "tomtom_distance_km" in df.columns:
        tomtom_cols.append("tomtom_distance_km")
    tomtom = df[tomtom_cols].apply(pd.to_numeric, errors="coerce").dropna(
        subset=["tomtom_travel_time_min"]
    )
    tomtom = tomtom[tomtom["tomtom_travel_time_min"] > 0]
    rows.append({
        "Source": "TomTom (reference)",
        "N": str(len(tomtom)),
        "Mean TT [min]": f"{tomtom['tomtom_travel_time_min'].mean():.1f}",
        "Mean dist [km]": f"{tomtom['tomtom_distance_km'].mean():.2f}" if "tomtom_distance_km" in tomtom else "\u2014",
        "Abs. mean [min]": "\u2014",
        "RMSE [min]": "\u2014",
        "Rel. mean [%]": "\u2014",
        "MAE [%]": "\u2014",
    })

    for key, label, _ in MODELS:
        time_cols = ["tomtom_travel_time_min", f"{key}_travel_time_min"]
        if not set(time_cols).issubset(df.columns):
            continue
        distance_col = f"{key}_distance_km" if f"{key}_distance_km" in df.columns else "tomtom_distance_km"
        cols = time_cols + ([distance_col] if distance_col in df.columns else [])
        tmp = df[cols].apply(pd.to_numeric, errors="coerce").dropna(subset=time_cols)
        tmp = tmp[tmp["tomtom_travel_time_min"] > 0].copy()
        if tmp.empty:
            continue
        diff_min = tmp[time_cols[1]] - tmp[time_cols[0]]
        diff_pct = 100 * diff_min / tmp[time_cols[0]]
        finite = np.isfinite(diff_min) & np.isfinite(diff_pct)
        tmp = tmp[finite]
        diff_min = diff_min[finite]
        diff_pct = diff_pct[finite]
        rows.append({
            "Source": label,
            "N": str(len(tmp)),
            "Mean TT [min]": f"{tmp[time_cols[1]].mean():.1f}",
            "Mean dist [km]": f"{tmp[distance_col].mean():.2f}" if distance_col in tmp else "\u2014",
            "Abs. mean [min]": _fmt_signed(diff_min.mean()),
            "RMSE [min]": f"{_rmse(diff_min):.1f}",
            "Rel. mean [%]": _fmt_signed(diff_pct.mean()),
            "MAE [%]": f"{np.mean(np.abs(diff_pct)):.1f}",
        })

    return rows


def _set_scientific_axes(ax):
    ax.set_axisbelow(True)
    ax.grid(axis="y", color="#d1d5db", linestyle="--", linewidth=0.7, alpha=0.75)
    ax.grid(axis="x", visible=False)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#4b5563")
        ax.spines[side].set_linewidth(0.9)
    ax.tick_params(colors="#111827", labelsize=9)


def _draw(ax, data, df, col, ylabel, title):
    groups = []
    for _, label, _ in MODELS:
        model_data = data.loc[data["model"].eq(label)]
        values = model_data[col].to_numpy()
        distances = model_data["distance_km"].to_numpy()
        travel_times = model_data["model_min"].to_numpy()
        if len(values):
            groups.append((label, values, distances, travel_times))
    if not groups:
        ax.text(0.5, 0.5, "No routed trips", transform=ax.transAxes, ha="center", va="center")
        ax.set_xticks([])
    else:
        labels, values, distances, travel_times = zip(*groups)
        positions = np.arange(1, len(labels) + 1)
        parts = ax.violinplot(values, positions=positions, showmeans=False, showextrema=False, widths=0.78)
        for body, label in zip(parts["bodies"], labels):
            color = next(color for _, name, color in MODELS if name == label)
            body.set_facecolor(color)
            body.set_edgecolor("#1f2937")
            body.set_linewidth(0.8)
            body.set_alpha(0.68)
        box = ax.boxplot(
            values,
            positions=positions,
            widths=0.18,
            whis=(5, 95),
            showfliers=False,
            showmeans=False,
            patch_artist=True,
            medianprops={"color": "#111827", "linewidth": 1.0},
            boxprops={"facecolor": "white", "edgecolor": "#111827", "linewidth": 0.9, "alpha": 0.92},
            whiskerprops={"color": "#111827", "linewidth": 0.85},
            capprops={"color": "#111827", "linewidth": 0.85},
        )
        for patch in box["boxes"]:
            patch.set_zorder(3)
        for line in box["medians"]:
            line.set_zorder(4)
        for line in [*box["whiskers"], *box["caps"]]:
            line.set_zorder(4)
        ax.set_xticks(positions, labels)
    ax.axhline(0, color="#111827", linewidth=0.9)
    ax.set_title(title, fontsize=11, pad=8)
    ax.set_ylabel(ylabel)
    _set_scientific_axes(ax)


def _draw_summary_table(ax, rows):
    ax.axis("off")
    col_widths = np.array([0.19, 0.07, 0.13, 0.13, 0.13, 0.11, 0.12, 0.12], dtype=float)
    col_widths = col_widths / col_widths.sum()
    col_edges = np.concatenate(([0.0], np.cumsum(col_widths)))
    table_top = 0.94
    table_bottom = 0.08
    row_count = len(rows) + 1
    row_height = (table_top - table_bottom) / row_count
    pad = 0.008

    row_values = [dict(zip(SUMMARY_TABLE_COLUMNS, SUMMARY_TABLE_COLUMNS))] + rows
    for row_idx, row in enumerate(row_values):
        y = table_top - (row_idx + 1) * row_height
        if row_idx == 0:
            facecolor = "#000000"
            text_color = "white"
            weight = "bold"
        else:
            facecolor = "#f2f2f2" if row_idx % 2 else "#d9d9d9"
            text_color = "#111827"
            weight = "normal"

        ax.add_patch(
            Rectangle(
                (0.0, y),
                1.0,
                row_height,
                transform=ax.transAxes,
                facecolor=facecolor,
                edgecolor="none",
                zorder=0,
            )
        )

        for col_idx, column in enumerate(SUMMARY_TABLE_COLUMNS):
            left = col_edges[col_idx]
            right = col_edges[col_idx + 1]
            ha = "left" if col_idx == 0 else "right"
            x = left + pad if col_idx == 0 else right - pad
            ax.text(
                x,
                y + row_height / 2,
                row[column],
                transform=ax.transAxes,
                ha=ha,
                va="center",
                fontsize=8.5,
                color=text_color,
                fontfamily="DejaVu Sans",
                fontweight=weight,
            )


def plot_violin(data, out_path, label="All day", summary_table_rows=None):
    df = _frame(data)
    errors = _errors(df)
    with plt.rc_context({
        "font.family": "DejaVu Sans",
        "axes.titlesize": 11,
        "axes.labelsize": 10,
        "figure.titlesize": 13,
    }):
        has_table = bool(summary_table_rows)
        if has_table:
            fig = plt.figure(figsize=(10.8, 5.9), constrained_layout=False)
            gs = fig.add_gridspec(
                2,
                2,
                height_ratios=[4.6, 1.0],
                hspace=0.18,
                wspace=0.25,
            )
            axes = [fig.add_subplot(gs[0, 0]), fig.add_subplot(gs[0, 1])]
            table_ax = fig.add_subplot(gs[1, :])
        else:
            fig, axes = plt.subplots(1, 2, figsize=(10.8, 4.8), constrained_layout=True)
            table_ax = None

        _draw(axes[0], errors, df, "diff_min", "Absolute error [min]", "Absolute error")
        _draw(axes[1], errors, df, "diff_pct", "Relative error [%]", "Relative error")
        if table_ax is not None:
            _draw_summary_table(table_ax, summary_table_rows)
            fig.subplots_adjust(left=0.07, right=0.985, bottom=0.06, top=0.94)
        else:
            fig.suptitle(f"Travel-time error distributions relative to TomTom ({label})", y=1.08)

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, dpi=300, bbox_inches="tight")
        plt.close(fig)
    return out_path
