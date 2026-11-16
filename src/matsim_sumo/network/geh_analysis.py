"""GEH analysis of one or more ``count_check`` results, with a line of best fit.

Every ``counts.csv`` (one per seed) gives the simulated volume of each matched
counting cross-section. The simulated volume is the mean over the seeds, and the
GEH statistic is computed from that mean. The line of best fit is the
least-squares line of the simulated against the counted volume, once with an
intercept and once through the origin; R^2 is the share of the variance of the
simulated volumes it explains.

    python -m matsim_sumo.network.geh_analysis --counts a/counts.csv b/counts.csv --out-dir geh

``--daily`` reads ``count_check --daily`` results (daily totals, 00:00-24:00).
The main statistic is the GEH of the daily totals divided by 24,
GEH_day/24 < 5 (GEH_day < 120). The hourly-equivalent GEH of the mean hour,
GEH(M/24, C/24) = GEH_day/sqrt(24), is reported beside it as a secondary
number. The scatter plot and the line of best fit use the daily totals, and the
shaded band is GEH_day/24 < 5. Outputs: geh-daily.csv, geh-daily.json and
geh-daily-scatter.png/.pdf.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path

from matsim_sumo.network.count_check import GEH_LIMIT, daily_geh, geh

HOURS_PER_DAY = 24
DAILY_GEH_LIMIT = GEH_LIMIT * HOURS_PER_DAY
HOURLY_EQUIVALENT_LIMIT = GEH_LIMIT * math.sqrt(HOURS_PER_DAY)


def read(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8") as handle:
        return {row["zsid"]: row for row in csv.DictReader(handle)}


def merge(paths: list[Path]) -> list[dict]:
    runs = [read(path) for path in paths]
    common = sorted(set.intersection(*(set(run) for run in runs)))
    rows = []
    for zsid in common:
        first = runs[0][zsid]
        simulated = [float(run[zsid]["simulated"]) for run in runs]
        mean = statistics.fmean(simulated)
        counted = float(first["count"])
        rows.append({
            "zsid": zsid,
            "name": first["name"],
            "street": first["street"],
            "directions": int(first["directions"]),
            "edges": first["edges"],
            "count": counted,
            **{f"simulated_{i + 1}": value for i, value in enumerate(simulated)},
            "simulated": round(mean, 1),
            "geh": round(geh(mean, counted), 2),
        })
    return sorted(rows, key=lambda row: -row["geh"])


def fit(x: list[float], y: list[float]) -> dict:
    n = len(x)
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sxx = sum((a - mx) ** 2 for a in x)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    syy = sum((b - my) ** 2 for b in y)
    slope = sxy / sxx
    intercept = my - slope * mx
    origin = sum(a * b for a, b in zip(x, y)) / sum(a * a for a in x)
    residual_origin = sum((b - origin * a) ** 2 for a, b in zip(x, y))
    return {
        "n": n,
        "slope": round(slope, 4),
        "intercept": round(intercept, 1),
        "r2": round(sxy * sxy / (sxx * syy), 4),
        "slope_origin": round(origin, 4),
        "r2_origin": round(1 - residual_origin / syy, 4),
        "pearson_r": round(sxy / math.sqrt(sxx * syy), 4),
        "rmse": round(math.sqrt(sum((b - a) ** 2 for a, b in zip(x, y)) / n), 1),
    }


def summary(rows: list[dict], runs: list[list[dict]]) -> dict:
    x = [row["count"] for row in rows]
    y = [row["simulated"] for row in rows]
    gehs = [row["geh"] for row in rows]
    good = sum(1 for value in gehs if value < GEH_LIMIT)
    per_seed = []
    for i in range(len(runs)):
        values = [geh(row[f"simulated_{i + 1}"], row["count"]) for row in rows]
        per_seed.append({
            "under_5": sum(1 for value in values if value < GEH_LIMIT),
            "under_5_percent": round(100 * sum(1 for value in values if value < GEH_LIMIT) / len(values), 1),
            "median_geh": round(statistics.median(values), 2),
            "simulated": round(sum(row[f"simulated_{i + 1}"] for row in rows)),
        })
    return {
        "sections": len(rows),
        "under_5": good,
        "under_5_percent": round(100 * good / len(rows), 1),
        "under_10": sum(1 for value in gehs if value < 10),
        "under_10_percent": round(100 * sum(1 for value in gehs if value < 10) / len(rows), 1),
        "median_geh": round(statistics.median(gehs), 2),
        "mean_geh": round(statistics.fmean(gehs), 2),
        "counted": round(sum(x)),
        "simulated": round(sum(y)),
        "difference_percent": round(100 * (sum(y) - sum(x)) / sum(x), 1),
        "fit": fit(x, y),
        "per_seed": per_seed,
    }


def geh_envelope(counted: float, limit: float = GEH_LIMIT) -> tuple[float, float]:
    """Simulated volumes with GEH exactly ``limit`` for a counted volume."""
    a = limit * limit
    root = math.sqrt(a * a + 16 * a * counted)
    return max(0.0, counted + (a - root) / 4), counted + (a + root) / 4


def plot(rows: list[dict], result: dict, target: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x = [row["count"] for row in rows]
    y = [row["simulated"] for row in rows]
    top = 1.05 * max(x + y)
    grid = [top * i / 200 for i in range(201)]
    low, high = zip(*(geh_envelope(c) for c in grid))
    f = result["fit"]

    figure, axes = plt.subplots(figsize=(6.4, 6.0))
    axes.fill_between(grid, low, high, color="0.88", label=f"GEH < {GEH_LIMIT:g}")
    axes.plot(grid, grid, color="0.35", linestyle="--", linewidth=1, label="simulated = counted")
    good = [row for row in rows if row["geh"] < GEH_LIMIT]
    bad = [row for row in rows if row["geh"] >= GEH_LIMIT]
    axes.scatter([r["count"] for r in good], [r["simulated"] for r in good], s=22,
                 color="#009E73", edgecolor="white", linewidth=0.5, zorder=3,
                 label=f"GEH < {GEH_LIMIT:g} ({len(good)})")
    axes.scatter([r["count"] for r in bad], [r["simulated"] for r in bad], s=22,
                 color="#D55E00", edgecolor="white", linewidth=0.5, zorder=3,
                 label=f"GEH \u2265 {GEH_LIMIT:g} ({len(bad)})")
    axes.plot(grid, [f["intercept"] + f["slope"] * c for c in grid], color="#0072B2", linewidth=1.8,
              label=f"best fit: y = {f['slope']:.2f}x {f['intercept']:+.0f}, R\u00b2 = {f['r2']:.2f}")
    axes.set_xlim(0, top)
    axes.set_ylim(0, top)
    axes.set_aspect("equal")
    axes.set_xlabel("Counted vehicles, 07:00\u201308:00")
    axes.set_ylabel("Simulated vehicles, 07:00\u201308:00")
    axes.set_title(title, fontsize=10)
    axes.grid(color="0.92", linewidth=0.6)
    axes.legend(loc="upper left", fontsize=8, frameon=True)
    figure.tight_layout()
    for suffix in (".png", ".pdf"):
        figure.savefig(target.with_suffix(suffix), dpi=200)
    plt.close(figure)


def merge_daily(paths: list[Path]) -> list[dict]:
    """Daily totals averaged over the seeds, with GEH_day, GEH_day/24 and the hourly-equivalent GEH of the mean."""
    rows = []
    for row in merge(paths):
        values = daily_geh(row["simulated"], row["count"])
        row["geh_day"] = round(values["geh_day"], 2)
        row["geh"] = round(values["geh_day_24"], 3)
        row["geh_hourly_equivalent"] = round(values["geh_hourly_equivalent"], 2)
        rows.append(row)
    return sorted(rows, key=lambda row: -row["geh_day"])


def _shares(values: list[float], limit: float) -> dict:
    good = sum(1 for value in values if value < limit)
    return {"under_5": good, "under_5_percent": round(100 * good / len(values), 1),
            "median": round(statistics.median(values), 3), "mean": round(statistics.fmean(values), 3)}


def summary_daily(rows: list[dict], seeds: int) -> dict:
    x = [row["count"] for row in rows]
    y = [row["simulated"] for row in rows]
    per_seed = []
    for i in range(seeds):
        values = [daily_geh(row[f"simulated_{i + 1}"], row["count"]) for row in rows]
        per_seed.append({
            "geh_day_24": _shares([value["geh_day_24"] for value in values], GEH_LIMIT),
            "geh_hourly_equivalent": _shares([value["geh_hourly_equivalent"] for value in values], GEH_LIMIT),
            "simulated": round(sum(row[f"simulated_{i + 1}"] for row in rows)),
        })
    return {
        "period": "00:00-24:00 (daily totals)",
        "sections": len(rows),
        "main_statistic": {
            "name": "GEH_day/24",
            "definition": "GEH = sqrt(2 (M - C)^2 / (M + C)) of the daily totals M (simulated) and C (counted), divided by 24",
            "limit": f"GEH_day/24 < {GEH_LIMIT:g}, i.e. GEH_day < {DAILY_GEH_LIMIT:g}",
            **_shares([row["geh"] for row in rows], GEH_LIMIT),
        },
        "secondary_hourly_equivalent": {
            "name": "GEH(M/24, C/24) = GEH_day/sqrt(24)",
            "definition": "GEH of the mean hourly volumes, daily totals divided by 24 before the GEH",
            "limit": f"GEH(M/24, C/24) < {GEH_LIMIT:g}, i.e. GEH_day < {HOURLY_EQUIVALENT_LIMIT:.1f}",
            **_shares([row["geh_hourly_equivalent"] for row in rows], GEH_LIMIT),
        },
        "geh_day": {"median": round(statistics.median(row["geh_day"] for row in rows), 2),
                    "mean": round(statistics.fmean(row["geh_day"] for row in rows), 2)},
        "counted": round(sum(x)),
        "simulated": round(sum(y)),
        "difference_percent": round(100 * (sum(y) - sum(x)) / sum(x), 1),
        "fit": fit(x, y),
        "seeds": seeds,
        "per_seed": per_seed,
    }


def plot_daily(rows: list[dict], result: dict, target: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x = [row["count"] for row in rows]
    y = [row["simulated"] for row in rows]
    top = 1.05 * max(x + y)
    grid = [top * i / 200 for i in range(201)]
    low, high = zip(*(geh_envelope(c, DAILY_GEH_LIMIT) for c in grid))
    low_eq, high_eq = zip(*(geh_envelope(c, HOURLY_EQUIVALENT_LIMIT) for c in grid))
    f = result["fit"]

    figure, axes = plt.subplots(figsize=(6.4, 6.0))
    axes.fill_between(grid, low, high, color="0.88",
                      label=f"GEH_day/24 < {GEH_LIMIT:g} (GEH_day < {DAILY_GEH_LIMIT:g})")
    axes.plot(grid, low_eq, color="0.55", linestyle=":", linewidth=1,
              label=f"hourly-equivalent GEH(M/24, C/24) < {GEH_LIMIT:g}")
    axes.plot(grid, high_eq, color="0.55", linestyle=":", linewidth=1)
    axes.plot(grid, grid, color="0.35", linestyle="--", linewidth=1, label="simulated = counted")
    good = [row for row in rows if row["geh"] < GEH_LIMIT]
    bad = [row for row in rows if row["geh"] >= GEH_LIMIT]
    axes.scatter([r["count"] for r in good], [r["simulated"] for r in good], s=22,
                 color="#009E73", edgecolor="white", linewidth=0.5, zorder=3,
                 label=f"GEH_day/24 < {GEH_LIMIT:g} ({len(good)})")
    axes.scatter([r["count"] for r in bad], [r["simulated"] for r in bad], s=22,
                 color="#D55E00", edgecolor="white", linewidth=0.5, zorder=3,
                 label=f"GEH_day/24 \u2265 {GEH_LIMIT:g} ({len(bad)})")
    axes.plot(grid, [f["intercept"] + f["slope"] * c for c in grid], color="#0072B2", linewidth=1.8,
              label=f"best fit: y = {f['slope']:.2f}x {f['intercept']:+.0f}, R\u00b2 = {f['r2']:.2f}")
    axes.set_xlim(0, top)
    axes.set_ylim(0, top)
    axes.set_aspect("equal")
    axes.set_xlabel("Counted vehicles per day (typical weekday, 00:00\u201324:00)")
    axes.set_ylabel("Simulated vehicles per day (00:00\u201324:00)")
    axes.set_title(title, fontsize=10)
    axes.grid(color="0.92", linewidth=0.6)
    axes.ticklabel_format(style="plain")
    axes.legend(loc="upper left", fontsize=7.5, frameon=True)
    figure.tight_layout()
    for suffix in (".png", ".pdf"):
        figure.savefig(target.with_suffix(suffix), dpi=200)
    plt.close(figure)


def main_daily(args: argparse.Namespace) -> None:
    rows = merge_daily(args.counts)
    result = summary_daily(rows, len(args.counts))
    with (args.out_dir / "geh-daily.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (args.out_dir / "geh-daily.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    plot_daily(rows, result, args.out_dir / "geh-daily-scatter", args.title)
    print(json.dumps(result, indent=2))
    for row in rows[:10]:
        print(f"  GEH_day {row['geh_day']:6.1f} (/24 {row['geh']:.2f})  {row['name']}: "
              f"simulated {row['simulated']:.0f}, counted {row['count']:.0f} per day")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--counts", type=Path, nargs="+", required=True, help="counts.csv of count_check, one per seed")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--title", default="Simulated against counted traffic")
    parser.add_argument("--daily", action="store_true", help="counts.csv of count_check --daily: daily totals, GEH_day/24")
    args = parser.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.daily:
        main_daily(args)
        return

    rows = merge(args.counts)
    result = summary(rows, [read(path) for path in args.counts])
    with (args.out_dir / "geh.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (args.out_dir / "geh.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    plot(rows, result, args.out_dir / "geh-scatter", args.title)
    print(json.dumps(result, indent=2))
    for row in rows[:10]:
        print(f"  GEH {row['geh']:5.1f}  {row['name']}: simulated {row['simulated']:.0f}, counted {row['count']:.0f}")


if __name__ == "__main__":
    main()
