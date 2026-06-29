# MATSim-SUMO Pipeline Wiki

This file explains how to install, configure, run, and verify the MATSim-SUMO conversion pipeline.
It is written for users who provide their own MATSim and SUMO data.

## 1. Installation

Create a virtual environment and install the package in editable mode.

```bash
git clone <repo-url>
cd matsim-sumo-pipeline

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

The package installs the command `matsim-sumo`.

```bash
matsim-sumo --help
matsim-sumo run --help
```

Python dependencies are listed in `pyproject.toml` and `requirements.txt`.
External tools are not installed by Python.

## 2. Required Inputs

The main pipeline needs one MATSim output folder and one SUMO network.
The recommended local layout is:

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

MATSim files are resolved relative to `matsim_output_dir`.
The SUMO network is resolved as `sumo_input_dir / sumo_network_path` unless `sumo_network_path` is absolute.

Amenities are optional.
If `amenity_kdtree_enabled: true`, `amenities_path` must point to a CSV with `x`, `y`, and `weight` columns.
Coordinates must use the same metric coordinate reference system as the pipeline data.
If `amenity_kdtree_enabled: false`, no amenities file is needed.

Microcensus data is not required for the numbered pipeline.
It is needed only for the optional mode-share diagnostics.
Keep `mode_share_diagnostics_enabled: false` unless `data/MZ` and the required MATSim `output_persons.csv.gz` file are available.

Large local data stays under `data/` and is ignored by Git.

## 3. Configuration

Start from the template:

```bash
cp configs/templates/pipeline.yaml configs/my_run.yaml
```

Edit the copied file.
Most runs need these fields:

| Key | Meaning |
| --- | --- |
| `matsim_output_dir` | Folder with MATSim output files. |
| `plans_file`, `trips_file`, `events_file`, `network_file` | MATSim output filenames below `matsim_output_dir`. |
| `sumo_input_dir` | Folder with the SUMO network. |
| `sumo_network_path` | SUMO `.net.xml` filename or absolute path. |
| `boundary_file` | Path where the generated boundary is written. |
| `output_dir` | Folder for generated pipeline outputs. |
| `scale_factor` | Demand multiplier. `1.0` keeps demand unchanged. |
| `amenity_kdtree_enabled` | Enables amenity-based endpoint relocation during scaling. |
| `sumo_match_*` | Endpoint map-matching thresholds and guardrails. |
| `run_duarouter` | Enables SUMO routing in Step 4.2. |
| `duarouter_bin` | `duarouter` command or absolute executable path. |
| `mode_share_diagnostics_enabled` | Runs optional Microcensus/MATSim mode-share diagnostics after Step 4. Keep `false` for data-light runs. |
| `write_review_csv` | Writes uncompressed review CSV copies. |

`boundary_file` is normally a generated file, not a hand-authored input.
Set `boundary_force_rebuild: true` when buffer settings or the SUMO network changed.

`configs/base_config.yaml` is portable and can run after local data paths are prepared.
Scenario-specific files under `configs/analysis/` are examples for travel-time analysis and must be edited for local SUMO output locations.

## 4. Run The Main Pipeline

Run:

```bash
matsim-sumo run --config configs/my_run.yaml
```

Bypass cached stage results:

```bash
matsim-sumo run --config configs/my_run.yaml --no-cache
```

The run writes a copy of the resolved config to `output_dir`.
This supports reproducibility.

## 5. Boundary Confirmation

Step 0.2 writes a boundary preview:

```text
<output_dir>/plots/sumo_network_boundary_confirmation.png
```

The command then asks:

```text
Use this boundary for the pipeline? [y/N]:
```

Answer `y` or `yes` to continue.
Any other answer stops the run before demand filtering.
If the image does not open automatically, open the PNG manually and then answer in the terminal.

## 6. Pipeline Steps

| Step | Name | Description |
| --- | --- | --- |
| 0.1 | Prepare SUMO network | Read the SUMO network and convert valid edges to LV95 geometry. |
| 0.2 | Generate boundary | Build the SUMO boundary, write a preview, and ask for confirmation. |
| 1.1 | Boundary cut links | Find MATSim network links that cross the generated boundary. |
| 1.2 | Extract car legs | Read MATSim plans and keep car legs. |
| 1.3 | Crossing segments | Split crossing legs into inside-boundary route segments. |
| 1.4 | Segment timing | Use MATSim events to recover missing segment departure times. |
| 1.5 | Inside trip filter | Keep trips that start and end inside the boundary. |
| 1.6 | Combine filtered trips | Combine crossing segments and inside trips. |
| 2 | Demand scaling | Clone trips and perturb times or coordinates if configured. |
| 3 | Map matching | Match trip endpoints to valid SUMO start and end edges. |
| 4.1 | SUMO trips | Write SUMO trip XML. |
| 4.2 | DUA routing | Optionally run `duarouter` and write routes. |

The numbered steps stop at Step 4, matching `docs/pipeline_overview.png`.
Mode-share comparison is an unnumbered diagnostic utility, not a pipeline stage.
It is skipped by default.

## 7. Main Outputs

Outputs are written under `output_dir`.
Default filenames include:

```text
0_sumo_boundary.geojson
0_sumo_network_lv95.geojson
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
scenario_stats.csv
```

Plots and diagnostics are written below:

```text
<output_dir>/plots/
```

`results/`, generated XML/CSV outputs, caches, and local data are ignored by Git.

## 8. Commands

Plot the generated boundary:

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

Run the optional mode-share diagnostic utility:

```bash
matsim-sumo analyze-mode-shares \
  --matsim-output-dir data/inputs/matsim \
  --boundary results/my_run/0_sumo_boundary.geojson \
  --scenario-name my_run \
  --output-dir results/my_run/plots
```

The legacy script wrapper is:

```bash
python scripts/run_pipeline.py --config configs/my_run.yaml
```

`scripts/` should contain only this wrapper.

## 9. SUMO And External Tools

Install SUMO separately if `run_duarouter: true`.
Set `duarouter_bin` to `duarouter` when it is on `PATH`, or to an absolute executable path.

The default MATSim event extraction helper is:

```text
src/matsim_sumo/resources/shell/extract_entered_link_events.sh
```

It requires:

```text
pigz
GNU parallel
awk
```

On macOS:

```bash
brew install pigz parallel
```

GNU Parallel may print a citation notice on first use.
Run `parallel --citation` once if you want to silence that notice.

## 10. MATSim Router And Java

Travel-time analysis can route MATSim trips through a Java router.
The repository includes the required JAR at:

```text
lib/switzerland_run.jar
```

This file is a required bundled router artifact, not a generated output.
Java must be installed and available as `java` on `PATH`.
Memory can be configured in travel-time configs with `matsim_router_java_xmx`.

## 11. Travel-Time Analysis

Travel-time analysis code is tracked under:

```text
analysis/travel_times/
```

Run help:

```bash
.venv/bin/python analysis/travel_times/run_traveltime.py --help
```

TomTom requests require an API key.
Set it through the environment variable named by `tomtom_api_key_env`, normally:

```bash
export TOMTOM_API_KEY="your_key_here"
```

Generated travel-time run folders and response artifacts are ignored by Git.
See `analysis/travel_times/README.md` for details.

## 12. Figure Helpers

Submission helper scripts that are not the main pipeline wrapper live under:

```text
analysis/figures/
```

These scripts generate figures from existing outputs or online map tiles.
Generated figure outputs should be committed only when intentionally used as documentation assets.

## 13. Troubleshooting

`duarouter` is not found.
Install SUMO or set `duarouter_bin` to the correct executable.

The SUMO network path is not found.
Check `sumo_input_dir` and `sumo_network_path`.
Relative SUMO paths are joined as `sumo_input_dir / sumo_network_path`.

The boundary preview does not open.
Open `<output_dir>/plots/sumo_network_boundary_confirmation.png` manually.
Then answer the terminal prompt.

The boundary looks wrong.
Adjust `sumo_boundary_*` buffer settings.
Set `boundary_force_rebuild: true`.
Run again.

Old cached outputs appear.
Run with `--no-cache` or remove local `.cache/`.

Event extraction fails because `pigz` or `parallel` is missing.
Install `pigz` and GNU `parallel`, or set `extract_events_script` to a compatible local script.
