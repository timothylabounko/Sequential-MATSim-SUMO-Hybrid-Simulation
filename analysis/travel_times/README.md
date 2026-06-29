# Travel-Time Analysis

This folder contains source code for comparing travel times on shared random origin-destination trips.
The reference source is TomTom.
The model sources are MATSim and SUMO.

Generated run folders and output tables are ignored by Git.
Only the analysis source code and this README should be committed from this folder.

## Required Inputs

Prepare a config under `configs/analysis/`.
Each config must point to local MATSim and SUMO data.

Important keys:

| Key | Meaning |
| --- | --- |
| `tomtom_api_key_env` | Environment variable used for the TomTom API key. |
| `travel_time_analysis_dir` | Root folder for generated run folders. |
| `travel_time_random_trips_file` | Stable random-trip CSV path. |
| `travel_time_boundary_file` | Boundary polygon for random trip generation. |
| `matsim_output_dir` | MATSim output folder. |
| `matsim_config_file` | Optional MATSim router config file. |
| `matsim_router_jar` | Optional router JAR path. Defaults to `lib/switzerland_run.jar`. |
| `sumo_output_dir` | SUMO output folder or SUMO scenario output snapshot. |
| `sumo_network_path` | Optional SUMO network path. Required if no SUMO config snapshot is available. |
| `duarouter_bin` | Optional `duarouter` path. Defaults to `duarouter` or a detected SUMO environment path. |

Do not commit personal absolute paths or API keys.
Use local copies of the example configs and edit them for your environment.

## Setup

Install the repository package first:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Set the TomTom key only when running TomTom queries:

```bash
export TOMTOM_API_KEY="your_key_here"
```

MATSim routing needs Java and the bundled router JAR:

```text
lib/switzerland_run.jar
```

SUMO routing needs SUMO command-line tools, including `sumo` and `duarouter`.

## Basic Variables

Choose a config and a run folder name:

```bash
CFG=configs/analysis/travel_time_bahnhof.yaml
RUN_DIR=1_output_scenario_bahnhof
```

`RUN_DIR` is resolved below `analysis/travel_times/` unless it is absolute.
Generated folders such as `*_output_*` and `travel_times_random/` are ignored by Git.

## Random Trips

Random trips are CSV files with origin, destination, departure time, SUMO edge IDs, and edge positions.
Generate or reuse random trips only:

```bash
.venv/bin/python analysis/travel_times/run_traveltime.py \
  --config "$CFG" \
  --run-dir "$RUN_DIR" \
  --generate-trips-only
```

If the stable random-trip file exists, it is reused.

## TomTom Only

TomTom is queried incrementally.
Completed trip IDs are stored in `tomtom_raw.json`.
The script skips completed trips on the next run.

```bash
.venv/bin/python analysis/travel_times/run_traveltime.py \
  --config "$CFG" \
  --run-dir "$RUN_DIR" \
  --tomtom-only
```

Main generated outputs:

```text
analysis/travel_times/<RUN_DIR>/tomtom_raw.json
analysis/travel_times/<RUN_DIR>/tomtom_errors.csv
analysis/travel_times/<RUN_DIR>/travel_times.csv
```

These files are ignored by Git.

## MATSim Only

MATSim travel times are computed with the MATSim router.
The router input is derived from the same random trips.

```bash
.venv/bin/python analysis/travel_times/run_traveltime.py \
  --config "$CFG" \
  --run-dir "$RUN_DIR" \
  --matsim-only
```

Main generated outputs:

```text
analysis/travel_times/<RUN_DIR>/matsim_router_input.csv
analysis/travel_times/<RUN_DIR>/matsim_routed.csv
analysis/travel_times/<RUN_DIR>/travel_times.csv
```

## SUMO Normal Simulation

This method inserts the random trips into SUMO as probe vehicles.
If a background state is configured, the probes interact with simulated traffic.

```bash
.venv/bin/python analysis/travel_times/run_traveltime.py \
  --config "$CFG" \
  --run-dir "$RUN_DIR" \
  --sumo-only
```

Main generated outputs:

```text
analysis/travel_times/<RUN_DIR>/sumo/sumo_travel_times.csv
analysis/travel_times/<RUN_DIR>/sumo/hourly/
analysis/travel_times/<RUN_DIR>/travel_times.csv
analysis/travel_times/<RUN_DIR>/plots/
```

The SUMO travel time is read from `tripinfo.xml`.
The used value is SUMO's `duration`.
Route distance is SUMO's `routeLength`.

## SUMO Edge-Based Estimate

This method does not insert probe vehicles into SUMO.
It routes the random trips with `duarouter` and sums hourly edge travel times from existing SUMO edge-data files.

```bash
.venv/bin/python analysis/travel_times/run_traveltime.py \
  --config "$CFG" \
  --run-dir "$RUN_DIR" \
  --sumo-edge-only
```

Main generated outputs:

```text
analysis/travel_times/<RUN_DIR>/sumo_edge_estimate/sumo_travel_times_edge_estimate.csv
analysis/travel_times/<RUN_DIR>/travel_times_edge_estimate.csv
analysis/travel_times/<RUN_DIR>/plots_edge_estimate/
```

This method is useful when probe trips should not add congestion.
It is an estimate because it uses aggregated edge travel times.
Missing edge travel times fall back to free-flow travel time.

## Full Run

Run all available steps:

```bash
.venv/bin/python analysis/travel_times/run_traveltime.py \
  --config "$CFG" \
  --run-dir "$RUN_DIR"
```

This runs TomTom, MATSim, normal SUMO simulation, and plots.
It writes the combined table:

```text
analysis/travel_times/<RUN_DIR>/travel_times.csv
```

## Plot-Only Modes

Regenerate plots for the normal SUMO table:

```bash
.venv/bin/python analysis/travel_times/run_traveltime.py \
  --config "$CFG" \
  --run-dir "$RUN_DIR" \
  --plots-only
```

Regenerate plots for the edge-based SUMO table:

```bash
.venv/bin/python analysis/travel_times/run_traveltime.py \
  --config "$CFG" \
  --run-dir "$RUN_DIR" \
  --plots-edge-only
```

## Long Sihlhoelzli Trip Helper

The helper for generating SUMO-routable Sihlhoelzli trips with a minimum route distance now lives here:

```bash
.venv/bin/python analysis/travel_times/generate_sihlhoezli_long_trips.py --help
```

It writes generated random-trip CSVs under ignored travel-time run folders.

## Output Tables

`travel_times.csv` contains the normal comparison table.
It may contain TomTom, MATSim, and normal SUMO columns.

`travel_times_edge_estimate.csv` contains the same comparison table, with SUMO columns from the edge-based estimate.

Important columns:

```text
tomtom_travel_time_min
matsim_travel_time_min
sumo_travel_time_min
tomtom_distance_km
matsim_distance_km
sumo_distance_km
```

## Interpretation

Use normal SUMO simulation when probe vehicles should interact with simulated traffic.
Use the edge-based estimate when probe vehicles should only measure existing SUMO conditions.
Normal simulation is more dynamic, but probe vehicles can add congestion.
The edge-based estimate avoids probe-traffic bias, but it depends on the quality of hourly edge travel times.
