from pathlib import Path
from xml.etree.ElementTree import ParseError, iterparse

import pandas as pd


def read_sumo_travel_times(output_dir, identifiers, tripinfo_file="tripinfo.xml"):
    wanted = set(map(str, identifiers))
    rows = {}
    for path in sorted((Path(output_dir) / "state" / "hourly").glob(f"*/{tripinfo_file}")):
        try:
            for _, elem in iterparse(path, events=("end",)):
                if elem.tag == "tripinfo" and elem.get("id") in wanted:
                    duration = elem.get("duration")
                    route_length = elem.get("routeLength")
                    if duration is not None:
                        rows[elem.get("id")] = {
                            "identifier": elem.get("id"),
                            "sumo_distance_km": float(route_length or 0.0) / 1000,
                            "sumo_travel_time_min": float(duration) / 60,
                        }
                elem.clear()
        except ParseError as exc:
            print(f"Skipping malformed SUMO tripinfo file {path}: {exc}")
    return pd.DataFrame(rows.values())
