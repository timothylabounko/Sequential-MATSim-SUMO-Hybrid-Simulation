import datetime as dt
import time

import requests
from pyproj import Transformer


def convert_crs(x, y, source="EPSG:2056", target="EPSG:4326"):
    return Transformer.from_crs(source, target, always_xy=True).transform(x, y)


def _tomtom_departure(seconds_after_midnight):
    seconds = int(round(float(seconds_after_midnight))) % 86400
    return dt.datetime.combine(dt.date(2025, 1, 6), dt.time(seconds // 3600, seconds % 3600 // 60, seconds % 60)).isoformat()


def route_one_trip(row, api_key):
    url = "https://api.tomtom.com/routing/1/calculateRoute/{},{}:{},{}/json".format(
        row.origin_y, row.origin_x, row.destination_y, row.destination_x
    )
    response = requests.get(url, params={
        "key": api_key,
        "departAt": _tomtom_departure(row.departure_time),
        "travelMode": "car",
        "computeTravelTimeFor": "all",
        "traffic": "true",
    }, timeout=30)
    response.raise_for_status()
    return response.json()["routes"][0]


def route_with_tomtom(trips, api_key, max_requests=100):
    routed = {}
    for row in trips.head(max_requests).itertuples(index=False):
        route_info = route_one_trip(row, api_key)
        routed[row.identifier] = {
            "origin_x": row.origin_x,
            "origin_y": row.origin_y,
            "destination_x": row.destination_x,
            "destination_y": row.destination_y,
            "departure_time": int(row.departure_time),
            "route_info": route_info,
        }
        time.sleep(0.1)
    return routed
