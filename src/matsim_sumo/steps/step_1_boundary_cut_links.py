import geopandas as gpd
import matsim
import pandas as pd
from shapely.geometry import Point

from matsim_sumo.csv_writer import CsvWriter
from matsim_sumo.pipeline_state import PipelineState


# Compute cut link IDs from network geometry and boundary
def compute_cut_link_ids_from_geo(geo: gpd.GeoDataFrame, boundary) -> pd.Series:
    # Keep only true boundary-crossing links (endpoint side changes).
    start_inside = geo["geometry"].apply(lambda g: boundary.covers(Point(g.coords[0])))
    end_inside = geo["geometry"].apply(lambda g: boundary.covers(Point(g.coords[-1])))
    crosses = start_inside ^ end_inside
    return (
        geo.loc[crosses, "link_id"]
        .astype(str)
        .str.extract(r"^(\d+)$", expand=False)
        .dropna()
        .drop_duplicates()
        .reset_index(drop=True)
    )
def run_boundary_cut_links_stage(state: PipelineState, projection: str = "EPSG:2056") -> None:
    state.boundary = gpd.read_file(state.boundary_path).union_all()
    state.network = matsim.read_network(str(state.network_path))
    state.network_geo = state.network.as_geo(projection=projection).copy()
    state.cut_link_ids = compute_cut_link_ids_from_geo(state.network_geo, state.boundary)

    CsvWriter.write_series(state.cut_link_ids, state.cut_links_csv_path, index=False, header=False)
