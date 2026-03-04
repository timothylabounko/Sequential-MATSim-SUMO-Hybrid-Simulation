import argparse
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely import points


def filter_inside_car_trips(trips_path, boundary_path, out_path=None):
    trips = pd.read_csv(trips_path, sep=";")
    boundary = gpd.read_file(boundary_path).union_all()
    start_in = gpd.GeoSeries(points(trips["start_x"], trips["start_y"])).covered_by(boundary)
    end_in = gpd.GeoSeries(points(trips["end_x"], trips["end_y"])).covered_by(boundary)
    out = trips.loc[trips["main_mode"].eq("car") & start_in & end_in].copy()
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(out_path, sep=";", index=False, compression="infer")
    return out


def main():
    p = argparse.ArgumentParser(description="Filter MATSim trips to car trips fully inside a boundary.")
    p.add_argument("trips_path")
    p.add_argument("boundary_path")
    p.add_argument("out_path")
    args = p.parse_args()
    out = filter_inside_car_trips(args.trips_path, args.boundary_path, args.out_path)
    print(f"Wrote {len(out)} trips to {args.out_path}")


if __name__ == "__main__":
    main()
