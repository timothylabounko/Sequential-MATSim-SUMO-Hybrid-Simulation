from pathlib import Path

import geopandas as gpd
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from matsim_sumo.disk_cache import disk_cache, file_dep, output_needs_refresh, param_dep


def _largest_polygon(geom) -> Polygon:
    if isinstance(geom, Polygon):
        return geom
    if isinstance(geom, MultiPolygon):
        return max(geom.geoms, key=lambda p: p.area)
    raise ValueError(f"SUMO boundary build produced no polygon geometry: {geom.geom_type}")


def build_sumo_boundary(
    sumo_network_geo: gpd.GeoDataFrame,
    network_buffer_m: float = 35.0,
    gap_close_m: float = 150.0,
    inward_buffer_m: float = 50.0,
    simplify_m: float = 40.0,
    outward_buffer_m: float = 100.0,
) -> Polygon:
    if sumo_network_geo.empty:
        raise ValueError("Cannot build SUMO boundary from an empty preprocessed network.")

    geometries = sumo_network_geo.geometry.dropna()
    if geometries.empty:
        raise ValueError("Cannot build SUMO boundary because all SUMO geometries are missing.")

    poly = unary_union(geometries.buffer(float(network_buffer_m))).buffer(float(gap_close_m)).buffer(
        -float(gap_close_m)
    )
    poly = Polygon(_largest_polygon(poly).exterior)
    eroded = poly.buffer(-float(inward_buffer_m))
    if not eroded.is_empty:
        poly = _largest_polygon(eroded)
    poly = poly.simplify(float(simplify_m), preserve_topology=True).buffer(float(outward_buffer_m)).buffer(0)
    poly = _largest_polygon(poly)
    if poly.is_empty:
        raise ValueError("SUMO boundary build produced an empty polygon.")
    return poly


@disk_cache(
    depends_on=lambda sumo_network_geo, sumo_network_path, params, vclasses: [
        file_dep(sumo_network_path),
        param_dep("boundary_params", params),
        param_dep("vclasses", tuple(vclasses or ())),
    ],
    step_name="sumo_boundary",
)
def _build_sumo_boundary_cached(
    sumo_network_geo: gpd.GeoDataFrame,
    sumo_network_path: str | Path,
    params: dict[str, float],
    vclasses: tuple[str, ...] | None,
) -> Polygon:
    del sumo_network_path
    return build_sumo_boundary(sumo_network_geo, **params)


def _write_sumo_boundary(poly: Polygon, out_path: str | Path, crs) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame({"name": [out.stem]}, geometry=[poly], crs=crs).to_file(out, driver="GeoJSON")
    return out


def compute_and_save_sumo_boundary(
    sumo_network_geo: gpd.GeoDataFrame,
    out_path: str | Path,
    network_buffer_m: float = 35.0,
    gap_close_m: float = 150.0,
    inward_buffer_m: float = 50.0,
    simplify_m: float = 40.0,
    outward_buffer_m: float = 100.0,
    sumo_network_path: str | Path | None = None,
    vclasses: tuple[str, ...] | None = None,
    no_cache: bool = False,
) -> Path:
    params = {
        "network_buffer_m": float(network_buffer_m),
        "gap_close_m": float(gap_close_m),
        "inward_buffer_m": float(inward_buffer_m),
        "simplify_m": float(simplify_m),
        "outward_buffer_m": float(outward_buffer_m),
    }
    if sumo_network_path is None:
        poly = build_sumo_boundary(sumo_network_geo, **params)
        should_write = True
    else:
        poly = _build_sumo_boundary_cached(sumo_network_geo, sumo_network_path, params, vclasses, no_cache=no_cache)
        should_write = output_needs_refresh(_build_sumo_boundary_cached, out_path)

    crs = sumo_network_geo.crs or "EPSG:2056"
    if should_write:
        return _write_sumo_boundary(poly, out_path, crs)
    return Path(out_path)
