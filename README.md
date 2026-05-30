# MATSim-SUMO Pipeline

This repository converts MATSim outputs into SUMO demand files. The main workflow filters MATSim car trips to a boundary, scales demand if requested, matches trip endpoints to SUMO edges, writes SUMO trip XML, and can optionally run `duarouter`.

## Quick Start

```bash
git clone <repo-url>
cd matsim-sumo-pipeline

python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp configs/templates/pipeline.yaml configs/my_run.yaml
```

Place or point your input files in `configs/my_run.yaml`, then run:

```bash
matsim-sumo run --config configs/my_run.yaml
```

The old `python scripts/run_pipeline.py` workflow is no longer the primary entrypoint. The package command is the documented interface.

## Expected Inputs

The template assumes this simple layout, but every path can be changed in YAML:

```text
data/
  inputs/
    matsim/
      output_network.xml.gz
      output_plans.xml.gz
      output_trips.csv.gz
      output_events.xml.gz
    sumo/
      scenario.net.xml
    boundaries/
      boundary.geojson
```

Optional amenities for spatial demand jitter can be provided as a CSV and enabled with `amenity_kdtree_enabled: true`.

## Main Commands

```bash
matsim-sumo --help
matsim-sumo run --config configs/my_run.yaml
matsim-sumo plot-boundary --config configs/my_run.yaml
matsim-sumo export-routes --help
matsim-sumo analyze-mode-shares --help
```

## Pipeline Steps

1. Prepare the SUMO network in LV95 coordinates and derive a SUMO boundary.
2. Find MATSim network links cut by the boundary.
3. Extract MATSim car legs from plans.
4. Split legs that cross the boundary into inside-boundary segments.
5. Recover missing segment departure times from MATSim events.
6. Keep trips that are fully inside the boundary.
7. Combine crossing and inside trips into one demand table.
8. Scale demand and jitter cloned trip coordinates if configured.
9. Match trip start/end points to valid SUMO edges.
10. Write SUMO trip XML and optionally run `duarouter`.

## Outputs

Outputs are written under `output_dir` from the config. The default filenames are:

```text
1_cut_links.csv
2_car_legs.csv.gz
3_car_legs_cut.csv.gz
3_crossing_segments.csv.gz
4_crossing_segments_timed.csv.gz
5_trips_inside.csv.gz
6_combined_trips.csv.gz
7_scaled_trips.csv.gz
8_scaled_trips_matched.csv.gz
9_sumo_trips.xml
10_sumo_routes.rou.xml
10_unrouted_trips.xml
10_duarouter.log
```

## Optional macOS Speed Dependencies

For faster extraction of large `output_events.xml.gz` files, install:

```bash
brew install pigz parallel
```

The packaged event extraction helper uses `pigz` and GNU `parallel`. If those are unavailable, set `extract_events_script` in the config to your own compatible extraction script.
