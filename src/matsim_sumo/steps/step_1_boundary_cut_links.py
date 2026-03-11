from pathlib import Path

import geopandas as gpd
import matsim
import pandas as pd

from matsim_sumo.pipeline_state import PipelineState


def compute_cut_link_ids_from_geo(geo: gpd.GeoDataFrame, boundary) -> pd.Series:
    inside = geo.geometry.within(boundary)
    outside = geo.geometry.disjoint(boundary)
    return (
        geo.loc[~inside & ~outside, "link_id"]
        .astype(str)
        .str.extract(r"^(\d+)$", expand=False)
        .dropna()
        .drop_duplicates()
        .reset_index(drop=True)
    )


def write_cut_links_csv(cut_link_ids: pd.Series, out_path: Path | str) -> None:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cut_link_ids.to_csv(out, index=False, header=False)


def run_boundary_cut_links_stage(state: PipelineState, projection: str = "EPSG:2056") -> None:
    state.boundary = gpd.read_file(state.boundary_path).union_all()
    state.network = matsim.read_network(str(state.network_path))
    state.network_geo = state.network.as_geo(projection=projection).copy()
    state.cut_link_ids = compute_cut_link_ids_from_geo(state.network_geo, state.boundary)

    write_cut_links_csv(state.cut_link_ids, state.cut_links_csv_path)
