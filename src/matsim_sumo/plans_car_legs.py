from pathlib import Path

import matsim
import pandas as pd

from matsim_sumo.mode_utils import normalize_modes
from matsim_sumo.pipeline_state import PipelineState


def _to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_legs_from_plans(plans_path: Path | str, modes_keep: object = None) -> pd.DataFrame:
    """
    Parse selected MATSim plans and extract leg-level route data for requested modes.
    """
    modes = set(normalize_modes(modes_keep))

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

            route = None
            routing_mode = None

            for child in leg:
                if child.tag == "route":
                    route = child
                elif child.tag == "attributes":
                    for attr in child:
                        if attr.tag == "attribute" and attr.attrib.get("name") == "routingMode":
                            routing_mode = (attr.text or "").strip() or None
                            break

            route_attr = route.attrib if route is not None else {}
            append(
                {
                    "person_id": person_id,
                    "mode": mode,
                    "dep_time": leg_attr.get("dep_time"),
                    "leg_trav_time": leg_attr.get("trav_time"),
                    "start_link": route_attr.get("start_link"),
                    "end_link": route_attr.get("end_link"),
                    "route_trav_time": route_attr.get("trav_time"),
                    "distance": _to_float(route_attr.get("distance")),
                    "vehicleRefId": route_attr.get("vehicleRefId"),
                    "routing_mode": routing_mode,
                    "route_links": (route.text or "").strip() if route is not None else None,
                }
            )

    return pd.DataFrame.from_records(rows)


def write_legs_csv(df: pd.DataFrame, out_path: Path | str) -> None:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, sep=";", index=False, compression="infer")


def run_plans_car_legs_stage(state: PipelineState) -> None:
    """
    Extract and persist mode-filtered route legs from MATSim plans.
    """
    state.car_legs = extract_legs_from_plans(
        plans_path=state.plans_path,
        modes_keep=state.config.get("modes_keep"),
    )
    write_legs_csv(state.car_legs, state.car_legs_path)
