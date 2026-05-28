import geopandas as gpd
import matsim
import pandas as pd
from shapely.geometry import Point

from matsim_sumo.csv_writer import CsvWriter
from matsim_sumo.disk_cache import disk_cache, file_dep, output_needs_refresh, param_dep
from matsim_sumo.pipeline_state import PipelineState


# Compute cut link IDs from network geometry and boundary
def compute_cut_link_ids_from_geo(geo: gpd.GeoDataFrame, boundary) -> pd.Series:
    start_inside = geo["geometry"].apply(lambda g: boundary.covers(Point(g.coords[0])))
    end_inside = geo["geometry"].apply(lambda g: boundary.covers(Point(g.coords[-1])))
    crosses = (start_inside ^ end_inside) | (
        geo["geometry"].intersects(boundary.boundary) & ~geo["geometry"].covered_by(boundary)
    )
    return (
        geo.loc[crosses, "link_id"]
        .astype(str)
        .str.extract(r"^(\d+)$", expand=False)
        .dropna()
        .drop_duplicates()
        .reset_index(drop=True)
    )


@disk_cache(
    depends_on=lambda network, boundary, network_path, boundary_path, projection: [
        file_dep(network_path),
        file_dep(boundary_path),
        param_dep("projection", projection),
    ],
    step_name="boundary_cut_links",
)
def _compute_boundary_cut_links_cached(
    network,
    boundary,
    network_path,
    boundary_path,
    projection: str,
) -> tuple[gpd.GeoDataFrame, pd.Series]:
    del network_path, boundary_path
    network_geo = network.as_geo(projection=projection).copy()
    cut_link_ids = compute_cut_link_ids_from_geo(network_geo, boundary)
    return network_geo, cut_link_ids


def run_boundary_cut_links_stage(state: PipelineState, projection: str = "EPSG:2056") -> None:
    state.boundary = gpd.read_file(state.boundary_path).union_all()
    state.network = matsim.read_network(str(state.network_path))
    state.network_geo, state.cut_link_ids = _compute_boundary_cut_links_cached(
        state.network,
        state.boundary,
        state.network_path,
        state.boundary_path,
        projection,
        no_cache=bool(state.config.get("no_cache", False)),
    )

    if output_needs_refresh(_compute_boundary_cut_links_cached, state.cut_links_csv_path):
        CsvWriter.write_series(state.cut_link_ids, state.cut_links_csv_path, index=False, header=False)
