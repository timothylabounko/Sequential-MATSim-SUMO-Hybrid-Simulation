from pathlib import Path

import geopandas as gpd
from shapely.geometry import Polygon
from shapely.ops import unary_union


def compute_and_save_sumo_boundary(sumo_network_geo: gpd.GeoDataFrame, out_path: str | Path) -> Path:
    if sumo_network_geo.empty:
        raise ValueError("Cannot build SUMO boundary from an empty preprocessed network.")

    poly = unary_union(sumo_network_geo.geometry.buffer(35)).buffer(150).buffer(-150)
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda p: p.area)
    poly = Polygon(poly.exterior).buffer(-100).simplify(100).buffer(300)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame({"name": [out.stem]}, geometry=[poly], crs="EPSG:2056").to_file(out, driver="GeoJSON")
    return out
