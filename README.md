# MATSim-SUMO Pipeline

This repository converts MATSim output into SUMO demand files. Users provide MATSim outputs and a SUMO network; the pipeline generates the SUMO boundary, filters MATSim demand to that boundary, scales demand if configured, map-matches trip endpoints to SUMO edges, and writes SUMO trip XML.

![Pipeline overview](docs/pipeline_overview.png)

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

The package command is the documented interface. The old `python scripts/run_pipeline.py` workflow is no longer the primary entry point.

## Required Inputs

The template assumes this layout, but every path can be changed in YAML:

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
    amenities/
      locations.csv
```

Required MATSim files are resolved as `matsim_output_dir / <file key>`. The SUMO network is resolved as `sumo_input_dir / sumo_network_path` unless `sumo_network_path` is absolute.

`boundary_file` is normally not an input. It is the path where the pipeline writes the generated boundary GeoJSON. Use `boundary_force_rebuild: true` or `matsim-sumo run --no-cache --config ...` when you want to regenerate cached outputs.

Amenities are optional. If `amenity_kdtree_enabled: true`, `amenities_path` must point to a CSV with at least `x`, `y`, and `weight` columns in the same coordinate system as the MATSim/SUMO LV95 data.

For the current local setup, see `data/README.md`. It documents the active aliases:

```text
data/inputs/matsim
data/inputs/sumo
data/inputs/amenities/locations.csv
```

## Config Guide

Most users only need to edit these keys:

- `matsim_output_dir`: folder containing MATSim output files.
- `sumo_input_dir` and `sumo_network_path`: folder and filename for the SUMO `.net.xml`.
- `boundary_file`: generated boundary output path.
- `output_dir`: folder for all pipeline outputs.
- `scale_factor`: `1.0` keeps demand unchanged; values above `1.0` clone trips.
- `dep_time_jitter_seconds` and `coord_jitter_radius_m`: randomization used for cloned trips.
- `amenity_kdtree_enabled`: if true, cloned endpoints can be moved toward amenities from `locations.csv`.
- `run_duarouter`: if true, Step 4.2 runs SUMO `duarouter`.
- `duarouter_bin`: executable path or command name for `duarouter`.
- `write_review_csv`: writes uncompressed review copies for selected CSV outputs.

`pip install -e .` installs Python dependencies, including `sumolib`, but it does not install the SUMO command-line tools. If `run_duarouter: true`, install SUMO separately and make sure `duarouter_bin` works.

## Running The Pipeline

```bash
matsim-sumo --help
matsim-sumo run --config configs/my_run.yaml
matsim-sumo run --config configs/my_run.yaml --no-cache
```

During Step 0.3 the pipeline writes:

```text
<output_dir>/plots/sumo_network_boundary_confirmation.png
```

It then tries to open the image and asks:

```text
Use this boundary for the pipeline? [y/N]:
```

Answer `y` or `yes` to continue. Any other answer stops the run before demand filtering.

## Pipeline Steps

| Step | Name | What happens |
| --- | --- | --- |
| 0 | Initialization | Prepare the SUMO network in LV95, generate the SUMO boundary, and ask the user to confirm it. |
| 1.1 | Demand Filtering | Find MATSim links cut by the generated boundary. |
| 1.2 | Demand Filtering | Extract MATSim car legs from plans. |
| 1.3 | Demand Filtering | Split crossing car legs into inside-boundary segments. |
| 1.4 | Demand Filtering | Recover missing segment departure times from MATSim events. |
| 1.5 | Demand Filtering | Keep trips that start and end inside the boundary. |
| 1.6 | Demand Filtering | Combine crossing segments and inside trips into one filtered demand table. |
| 2 | Demand Scaling | Clone trips according to `scale_factor` and jitter times/coordinates if configured. |
| 3 | Map-Matching | Match trip start/end coordinates to valid SUMO edges. |
| 4.1 | SUMO Trip Generation | Write SUMO trip XML. |
| 4.2 | SUMO Trip Generation | Optionally run `duarouter` and write routes/unrouted trips. |

## Outputs

Outputs are written under `output_dir`. Default filenames are:

```text
1_1_cut_links.csv
1_2_car_legs.csv.gz
1_3_car_legs_cut.csv.gz
1_3_crossing_segments.csv.gz
1_4_output_events_link_events_car_truck_cut.csv
1_4_crossing_segments_timed.csv.gz
1_5_trips_inside.csv.gz
1_6_combined_trips.csv.gz
2_scaled_trips.csv.gz
3_matched_trips.csv.gz
4_1_sumo_trips.xml
4_2_sumo_routes.rou.xml
4_2_unrouted_trips.xml
4_2_duarouter.log
```

Plots and diagnostics are written under `<output_dir>/plots`.

## Optional Commands

Re-plot the generated boundary:

```bash
matsim-sumo plot-boundary --config configs/my_run.yaml
```

Export routed SUMO routes to a QGIS-ready GeoPackage:

```bash
matsim-sumo export-routes \
  --routes results/my_run/4_2_sumo_routes.rou.xml \
  --network data/inputs/sumo/scenario.net.xml \
  --output results/my_run/routes.gpkg \
  --percent 1
```

Run the mode-share analysis utility:

```bash
matsim-sumo analyze-mode-shares --matsim-output-dir data/inputs/matsim
```

## Optional Speed Dependencies

For faster extraction of large `output_events.xml.gz` files on macOS, install:

```bash
brew install pigz parallel
```

The packaged event extraction helper uses `pigz` and GNU `parallel`. If those are unavailable, set `extract_events_script` in the config to your own compatible extraction script.

## Troubleshooting

- `duarouter` not found: install SUMO and set `duarouter_bin` to the correct executable.
- SUMO network path not found: check `sumo_input_dir` and `sumo_network_path`; relative network paths are joined together.
- Boundary preview does not open: open `<output_dir>/plots/sumo_network_boundary_confirmation.png` manually and answer the terminal prompt.
- Boundary looks wrong: tune the `sumo_boundary_*` buffer settings and rerun with `boundary_force_rebuild: true`.
- Old cached outputs appear: rerun with `matsim-sumo run --config ... --no-cache`.
- Event extraction fails: install `pigz` and GNU `parallel`, or set `extract_events_script` to another compatible helper.
