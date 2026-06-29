import pandas as pd


def read_routed_trips(path):
    df = pd.read_csv(path)
    return pd.DataFrame({
        "identifier": df["identifier"].astype(str),
        "matsim_distance_km": df["travel_distance"] / 1000,
        "matsim_travel_time_min": df["travel_time"] / 60,
        "matsim_access_distance_km": df["access_distance"] / 1000,
        "matsim_egress_distance_km": df["egress_distance"] / 1000,
    })
