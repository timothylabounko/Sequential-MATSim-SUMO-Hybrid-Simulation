from pathlib import Path

import geopandas as gpd
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union


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


def compute_and_save_sumo_boundary(
    sumo_network_geo: gpd.GeoDataFrame,
    out_path: str | Path,
    network_buffer_m: float = 35.0,
    gap_close_m: float = 150.0,
    inward_buffer_m: float = 50.0,
    simplify_m: float = 40.0,
    outward_buffer_m: float = 100.0,
) -> Path:
    poly = build_sumo_boundary(
        sumo_network_geo,
        network_buffer_m=network_buffer_m,
        gap_close_m=gap_close_m,
        inward_buffer_m=inward_buffer_m,
        simplify_m=simplify_m,
        outward_buffer_m=outward_buffer_m,
    )

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    crs = sumo_network_geo.crs or "EPSG:2056"
    gpd.GeoDataFrame({"name": [out.stem]}, geometry=[poly], crs=crs).to_file(out, driver="GeoJSON")
    return out
