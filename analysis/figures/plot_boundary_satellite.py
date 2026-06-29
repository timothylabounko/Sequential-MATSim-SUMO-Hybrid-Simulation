#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Iterable

os.environ.setdefault(
    "MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "matsim_sumo_matplotlib")
)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
from pyproj import Transformer


PROJECT_ROOT = Path(__file__).resolve().parents[2]

BOUNDARY_COLOR = "#d7191c"
DEFAULT_TILE_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
WEB_MERCATOR_RADIUS = 6378137.0
WEB_MERCATOR_ORIGIN_SHIFT = math.pi * WEB_MERCATOR_RADIUS
TILE_SIZE = 256


def _project_path(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _iter_polygon_rings(geometry: dict) -> Iterable[list[list[float]]]:
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if geom_type == "Polygon":
        yield from coords
    elif geom_type == "MultiPolygon":
        for polygon in coords:
            yield from polygon
    else:
        raise ValueError(f"Unsupported geometry type for boundary plotting: {geom_type}")


def _load_boundary_rings(path: Path) -> tuple[list[list[tuple[float, float]]], str]:
    with path.open() as f:
        data = json.load(f)

    crs_name = (
        data.get("crs", {})
        .get("properties", {})
        .get("name", "EPSG:2056")
        .split("::")[-1]
    )
    if crs_name.isdigit():
        crs_name = f"EPSG:{crs_name}"

    features = data.get("features")
    if features is None:
        features = [{"geometry": data.get("geometry", data)}]

    rings: list[list[tuple[float, float]]] = []
    for feature in features:
        geometry = feature.get("geometry")
        if not geometry:
            continue
        for ring in _iter_polygon_rings(geometry):
            rings.append([(float(x), float(y)) for x, y, *_ in ring])

    if not rings:
        raise ValueError(f"No polygon rings found in boundary file: {path}")
    return rings, crs_name


def _meters_to_tile(mx: float, my: float, zoom: int) -> tuple[int, int]:
    resolution = 2 * math.pi * WEB_MERCATOR_RADIUS / (TILE_SIZE * 2**zoom)
    px = (mx + WEB_MERCATOR_ORIGIN_SHIFT) / resolution
    py = (WEB_MERCATOR_ORIGIN_SHIFT - my) / resolution
    return int(px // TILE_SIZE), int(py // TILE_SIZE)


def _tile_bounds(tx: int, ty: int, zoom: int) -> tuple[float, float, float, float]:
    resolution = 2 * math.pi * WEB_MERCATOR_RADIUS / (TILE_SIZE * 2**zoom)
    left = tx * TILE_SIZE * resolution - WEB_MERCATOR_ORIGIN_SHIFT
    right = (tx + 1) * TILE_SIZE * resolution - WEB_MERCATOR_ORIGIN_SHIFT
    top = WEB_MERCATOR_ORIGIN_SHIFT - ty * TILE_SIZE * resolution
    bottom = WEB_MERCATOR_ORIGIN_SHIFT - (ty + 1) * TILE_SIZE * resolution
    return left, bottom, right, top


def _tile_ranges(
    bbox_3857: tuple[float, float, float, float], zoom: int
) -> tuple[list[int], list[int]]:
    minx, miny, maxx, maxy = bbox_3857
    x0, y1 = _meters_to_tile(minx, miny, zoom)
    x1, y0 = _meters_to_tile(maxx, maxy, zoom)
    return (
        list(range(min(x0, x1), max(x0, x1) + 1)),
        list(range(min(y0, y1), max(y0, y1) + 1)),
    )


def _download_tile(tile_url: str, zoom: int, x: int, y: int) -> Image.Image:
    url = tile_url.format(z=zoom, x=x, y=y)
    request = urllib.request.Request(
        url, headers={"User-Agent": "matsim-sumo-pipeline-plot/1.0"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return Image.open(BytesIO(response.read())).convert("RGB")


def _load_satellite_canvas(
    bbox_3857: tuple[float, float, float, float],
    zoom: int,
    max_tiles: int,
    tile_url: str,
) -> tuple[Image.Image, tuple[float, float, float, float], int, tuple[int, int]]:
    while zoom >= 1:
        x_tiles, y_tiles = _tile_ranges(bbox_3857, zoom)
        if len(x_tiles) * len(y_tiles) <= max_tiles:
            break
        zoom -= 1
    else:
        raise ValueError("Could not find a tile zoom level within --max-tiles")

    canvas = Image.new("RGB", (len(x_tiles) * TILE_SIZE, len(y_tiles) * TILE_SIZE))
    for ix, tile_x in enumerate(x_tiles):
        for iy, tile_y in enumerate(y_tiles):
            tile = _download_tile(tile_url, zoom, tile_x, tile_y)
            canvas.paste(tile, (ix * TILE_SIZE, iy * TILE_SIZE))

    left, _, _, top = _tile_bounds(x_tiles[0], y_tiles[0], zoom)
    _, bottom, right, _ = _tile_bounds(x_tiles[-1], y_tiles[-1], zoom)
    return canvas, (left, bottom, right, top), zoom, (len(x_tiles), len(y_tiles))


def _nice_tick_step(span: float, target_ticks: int = 6) -> int:
    raw_step = span / target_ticks
    for step in (100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000):
        if step >= raw_step:
            return step
    return 100000


def _nice_ticks(minimum: float, maximum: float, step: int | None = None) -> list[int]:
    if step is None:
        step = _nice_tick_step(maximum - minimum)
    start = math.ceil(minimum / step) * step
    end = math.floor(maximum / step) * step
    return list(range(start, end + step, step))


def plot_boundary_satellite(
    boundary_path: Path,
    output_path: Path,
    title: str,
    zoom: int,
    margin_fraction: float,
    max_tiles: int,
    tile_url: str,
) -> Path:
    rings_2056, boundary_crs = _load_boundary_rings(boundary_path)
    to_3857 = Transformer.from_crs(boundary_crs, "EPSG:3857", always_xy=True)
    from_3857 = Transformer.from_crs("EPSG:3857", boundary_crs, always_xy=True)

    rings_3857 = [
        [to_3857.transform(x, y) for x, y in ring]
        for ring in rings_2056
    ]
    all_x_3857 = [x for ring in rings_3857 for x, _ in ring]
    all_y_3857 = [y for ring in rings_3857 for _, y in ring]
    all_x_2056 = [x for ring in rings_2056 for x, _ in ring]
    all_y_2056 = [y for ring in rings_2056 for _, y in ring]

    minx, maxx = min(all_x_3857), max(all_x_3857)
    miny, maxy = min(all_y_3857), max(all_y_3857)
    margin = max(maxx - minx, maxy - miny) * margin_fraction
    bbox_3857 = (minx - margin, miny - margin, maxx + margin, maxy + margin)

    canvas, image_extent, used_zoom, tile_shape = _load_satellite_canvas(
        bbox_3857, zoom, max_tiles, tile_url
    )
    image_left, image_bottom, image_right, image_top = image_extent
    image_width, image_height = canvas.size

    def web_mercator_to_pixel(x: float, y: float) -> tuple[float, float]:
        px = (x - image_left) / (image_right - image_left) * image_width
        py = (image_top - y) / (image_top - image_bottom) * image_height
        return px, py

    rings_px = [[web_mercator_to_pixel(x, y) for x, y in ring] for ring in rings_3857]
    bbox_left_px, bbox_bottom_px = web_mercator_to_pixel(bbox_3857[0], bbox_3857[1])
    bbox_right_px, bbox_top_px = web_mercator_to_pixel(bbox_3857[2], bbox_3857[3])

    fig, ax = plt.subplots(figsize=(9.5, 7.0), dpi=300)
    ax.patch.set_alpha(0)
    ax.imshow(
        np.asarray(canvas),
        extent=(0, image_width, image_height, 0),
        origin="upper",
        zorder=1,
    )

    for ring in rings_px:
        xs = [x for x, _ in ring]
        ys = [y for _, y in ring]
        ax.plot(
            xs,
            ys,
            color=BOUNDARY_COLOR,
            linewidth=2.5,
            solid_joinstyle="round",
            label="Boundary",
            zorder=5,
        )

    ax.set_xlim(bbox_left_px, bbox_right_px)
    ax.set_ylim(bbox_bottom_px, bbox_top_px)
    ax.set_aspect("equal", adjustable="box")
    if title:
        ax.set_title(title, fontsize=15, color="black", pad=12)
    ax.set_xlabel("Easting (CH1903+ / LV95) [m]", color="black")
    ax.set_ylabel("Northing (CH1903+ / LV95) [m]", color="black")

    center_easting = sum(all_x_2056) / len(all_x_2056)
    center_northing = sum(all_y_2056) / len(all_y_2056)
    visible_corners_2056 = [
        from_3857.transform(x, y)
        for x in (bbox_3857[0], bbox_3857[2])
        for y in (bbox_3857[1], bbox_3857[3])
    ]
    visible_x_2056 = [x for x, _ in visible_corners_2056]
    visible_y_2056 = [y for _, y in visible_corners_2056]
    easting_step = _nice_tick_step(max(visible_x_2056) - min(visible_x_2056), 8)
    northing_step = _nice_tick_step(max(visible_y_2056) - min(visible_y_2056), 8)
    easting_ticks = _nice_ticks(min(visible_x_2056), max(visible_x_2056), easting_step)
    northing_ticks = _nice_ticks(
        min(visible_y_2056), max(visible_y_2056), northing_step
    )
    if len(easting_ticks) >= 2:
        ax.set_xticks(
            [
                web_mercator_to_pixel(to_3857.transform(x, center_northing)[0], 0)[0]
                for x in easting_ticks
            ]
        )
        ax.set_xticklabels([str(x) for x in easting_ticks])
    if len(northing_ticks) >= 2:
        ax.set_yticks(
            [
                web_mercator_to_pixel(0, to_3857.transform(center_easting, y)[1])[1]
                for y in northing_ticks
            ]
        )
        ax.set_yticklabels([str(y) for y in northing_ticks])

    ax.tick_params(axis="both", colors="black", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("black")
        spine.set_linewidth(1.0)
    ax.grid(color="#d0d0d0", linewidth=0.45, alpha=0.65, zorder=2)

    handles, labels = ax.get_legend_handles_labels()
    unique = dict(zip(labels, handles))
    legend = ax.legend(
        unique.values(),
        unique.keys(),
        loc="upper right",
        frameon=True,
        framealpha=0.92,
        fontsize=8,
    )
    legend.get_frame().set_facecolor("white")
    legend.get_frame().set_edgecolor("black")
    for text in legend.get_texts():
        text.set_color("black")

    ax.text(
        0.99,
        0.01,
        "Satellite imagery: Esri World Imagery",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=6.5,
        color="black",
        bbox={
            "facecolor": "white",
            "edgecolor": "black",
            "alpha": 0.9,
            "pad": 2.0,
        },
        zorder=9,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote: {output_path}")
    print(f"Satellite tiles: zoom={used_zoom}, grid={tile_shape[0]}x{tile_shape[1]}")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plot a SUMO boundary on a satellite image background."
    )
    parser.add_argument(
        "--boundary",
        default="results/run_40_scenario_sihlhoezli_final/0_sumo_boundary.geojson",
        help="Boundary GeoJSON path, normally in CH1903+ / LV95 (EPSG:2056).",
    )
    parser.add_argument(
        "--output-dir",
        default="data/1_scenario_sihlhoelzli",
        help="Directory for the output PNG.",
    )
    parser.add_argument(
        "--output-name",
        default="scenario_sihlhoezli_boundary_satellite.png",
        help="Output PNG filename.",
    )
    parser.add_argument("--title", default="Scenario Sihlhoezli boundary")
    parser.add_argument("--zoom", type=int, default=16, help="Requested XYZ tile zoom.")
    parser.add_argument(
        "--margin-fraction",
        type=float,
        default=0.18,
        help="Map margin as a fraction of the larger boundary dimension.",
    )
    parser.add_argument(
        "--max-tiles",
        type=int,
        default=80,
        help="Automatically reduce zoom until at most this many tiles are downloaded.",
    )
    parser.add_argument(
        "--tile-url",
        default=DEFAULT_TILE_URL,
        help="XYZ tile URL template with {z}, {x}, and {y}.",
    )
    args = parser.parse_args()

    output_path = _project_path(args.output_dir) / args.output_name
    plot_boundary_satellite(
        boundary_path=_project_path(args.boundary),
        output_path=output_path,
        title=args.title,
        zoom=args.zoom,
        margin_fraction=args.margin_fraction,
        max_tiles=args.max_tiles,
        tile_url=args.tile_url,
    )


if __name__ == "__main__":
    main()
