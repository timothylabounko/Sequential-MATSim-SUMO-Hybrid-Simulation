from pathlib import Path

import matsim
import pandas as pd

from matsim_sumo.csv_writer import CsvWriter
from matsim_sumo.pipeline_state import PipelineState


LEG_COLUMNS = ["person_id", "mode", "dep_time", "vehicleRefId", "route_links"]


# Extract car legs from MATSim plans
def extract_legs_from_plans(plans_path: Path | str, modes_keep: object = None) -> pd.DataFrame:
    modes = set(modes_keep)
    rows = []
    append = rows.append

    for person, plan in matsim.plan_reader(str(plans_path), selected_plans_only=True):
        if plan is None:
            continue

        person_id = person.attrib.get("id")

        for leg in plan.iterfind("leg"):
            leg_attr = leg.attrib
            mode = leg_attr.get("mode")
            if mode not in modes:
                continue

            # Filter out legs with invalid departure times
            dep_time = leg_attr.get("dep_time", "24:00:00")
            if int(dep_time.split(":", 1)[0]) >= 24:
                continue

            route = leg.find("route")
            route_links = (route.text or "").strip() if route is not None else ""
            if not route_links:
                continue

            route_attr = route.attrib if route is not None else {}
            append(
                {
                    "person_id": person_id,
                    "mode": mode,
                    "dep_time": dep_time,
                    "vehicleRefId": route_attr.get("vehicleRefId"),
                    "route_links": route_links,
                }
            )

    return pd.DataFrame.from_records(rows, columns=LEG_COLUMNS)


def run_plans_car_legs_stage(state: PipelineState) -> None:
    state.car_legs = extract_legs_from_plans(
        plans_path=state.plans_path,
        modes_keep=state.config["modes_keep"],
    )
    if state.config["write_intermediates"]:
        CsvWriter.write_df(state.car_legs, state.car_legs_path)
