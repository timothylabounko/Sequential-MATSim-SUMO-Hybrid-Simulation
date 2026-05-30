REQUIRED_CONFIG_KEYS = {
    "matsim_output_dir",
    "boundary_file",
    "network_file",
    "plans_file",
    "trips_file",
    "events_file",
    "output_dir",
    "modes_keep",
    "write_intermediates",
    "write_review_csv",
    "scale_factor",
    "dep_time_jitter_seconds",
    "coord_jitter_radius_m",
    "demand_scaling_seed",
    "amenity_kdtree_enabled",
    "amenities_path",
    "sumo_input_dir",
    "sumo_network_path",
    "sumo_network_lv95_tmp_file",
    "sumo_vclass_map",
    "run_duarouter",
    "duarouter_bin",
    "sumo_routes_file",
    "sumo_unrouted_trips_file",
    "duarouter_log_file",
}


def validate_config(config: dict) -> None:
    missing = sorted(REQUIRED_CONFIG_KEYS - set(config))
    if missing:
        raise KeyError(f"Missing required config keys: {', '.join(missing)}")
