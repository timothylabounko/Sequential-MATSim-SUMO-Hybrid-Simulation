# MATSim-SUMO Pipeline Wiki

This file explains how to install and run the MATSim-SUMO pipeline.
It is written for users who want to run the pipeline with their own MATSim and SUMO data.

## 1. Installation

Clone the repository.
Create a virtual environment.
Install the package in editable mode.

```bash
git clone <repo-url>
cd matsim-sumo-pipeline

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

The package installs the Python command `matsim-sumo`.
The command is the public interface of the pipeline.

```bash
matsim-sumo --help
matsim-sumo run --help
```

## 2. Required Inputs

The pipeline needs MATSim output files and one SUMO network.
The template config assumes this layout.

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

The MATSim files are resolved relative to `matsim_output_dir`.
The SUMO network is resolved as `sumo_input_dir / sumo_network_path`.
An absolute `sumo_network_path` is also valid.

Amenities are optional.
If `amenity_kdtree_enabled: true`, `amenities_path` must point to a CSV.
The CSV must contain `x`, `y`, and `weight` columns.
The coordinates must use the same metric coordinate system as the pipeline data.

## 3. Local Data Layout

The current local data aliases are documented in `data/README.md`.
The active layout is:

```text
data/inputs/matsim
data/inputs/sumo
data/inputs/amenities/locations.csv
```

The MATSim and SUMO entries can be symlinks.
This avoids copying large input folders.
The amenities folder should contain only `locations.csv`.

## 4. Configuration

Copy the template config.

```bash
cp configs/templates/pipeline.yaml configs/my_run.yaml
```

Edit `configs/my_run.yaml`.
Most users need these fields.

| Key | Meaning |
| --- | --- |
| `matsim_output_dir` | Folder with MATSim output files. |
| `sumo_input_dir` | Folder with the SUMO network. |
| `sumo_network_path` | SUMO `.net.xml` file name or absolute path. |
| `boundary_file` | Output path for the generated boundary. |
| `output_dir` | Folder for pipeline outputs. |
| `scale_factor` | Demand multiplier. `1.0` keeps demand unchanged. |
| `amenity_kdtree_enabled` | Enables amenity-based endpoint relocation. |
| `run_duarouter` | Enables SUMO routing in Step 4.2. |
| `duarouter_bin` | Path or command name for `duarouter`. |
| `write_review_csv` | Writes uncompressed review CSV files. |

`boundary_file` is normally not an input file.
The pipeline writes the generated boundary to this path.
Set `boundary_force_rebuild: true` if the boundary should be rebuilt.

## 5. Run The Pipeline

Run the pipeline with a config file.

```bash
matsim-sumo run --config configs/my_run.yaml
```

Use `--no-cache` when cached intermediate results should be ignored.

```bash
matsim-sumo run --config configs/my_run.yaml --no-cache
```

The run writes a copy of the resolved config to the output folder.
This improves reproducibility.

## 6. Boundary Confirmation

During Step 0.2 the pipeline writes a boundary preview after generating the boundary.

```text
<output_dir>/plots/sumo_network_boundary_confirmation.png
```

It then asks:

```text
Use this boundary for the pipeline? [y/N]:
```

Answer `y` or `yes` to continue.
Any other answer stops the run before demand filtering.
If the image does not open automatically, open it manually.

## 7. Pipeline Steps

| Step | Name | Description |
| --- | --- | --- |
| 0.1 | Prepare SUMO network | Read the SUMO network and convert valid edges to LV95 geometry. |
| 0.2 | Generate boundary | Build the boundary, write a preview, and ask the user to confirm it. |
| 1.1 | Boundary cut links | Find MATSim network links that cross the generated boundary. |
| 1.2 | Extract car legs | Read MATSim plans and keep car legs. |
| 1.3 | Crossing segments | Split crossing legs into inside-boundary route segments. |
| 1.4 | Segment timing | Use MATSim events to recover missing segment departure times. |
| 1.5 | Inside trip filter | Keep trips that start and end inside the boundary. |
| 1.6 | Combine filtered trips | Combine crossing segments and inside trips. |
| 2 | Demand scaling | Clone trips and perturb times or coordinates if configured. |
| 3 | Map-matching | Match trip endpoints to valid SUMO start and end edges. |
| 4.1 | SUMO trips | Write SUMO trip XML. |
| 4.2 | DUA routing | Optionally run `duarouter` and write routes. |

## 8. Output Files

Outputs are written under `output_dir`.
Default output names are:

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

Plots and diagnostics are written under:

```text
<output_dir>/plots
```

## 9. Optional Commands

Plot the generated boundary.

```bash
matsim-sumo plot-boundary --config configs/my_run.yaml
```

Export routed SUMO routes to a QGIS-ready GeoPackage.

```bash
matsim-sumo export-routes \
  --routes results/my_run/4_2_sumo_routes.rou.xml \
  --network data/inputs/sumo/scenario.net.xml \
  --output results/my_run/routes.gpkg \
  --percent 1
```

Run the mode-share analysis utility.

```bash
matsim-sumo analyze-mode-shares --matsim-output-dir data/inputs/matsim
```

## 10. SUMO And DUA Router

The Python package installs Python dependencies.
It does not install the SUMO command-line tools.

If `run_duarouter: true`, install SUMO separately.
Set `duarouter_bin` to a valid executable.
The value can be `duarouter` if the command is on `PATH`.
It can also be an absolute path.

## 11. Optional Speed Dependencies

Large MATSim event files can be slow to process.
On macOS, install:

```bash
brew install pigz parallel
```

The packaged event extraction helper uses `pigz` and GNU `parallel`.
If these tools are unavailable, set `extract_events_script` to another compatible script.

## 12. Troubleshooting

`duarouter` is not found.
Set `duarouter_bin` to the correct executable.
Install SUMO if needed.

The SUMO network path is not found.
Check `sumo_input_dir`.
Check `sumo_network_path`.
Remember that relative paths are joined together.

The boundary preview does not open.
Open `<output_dir>/plots/sumo_network_boundary_confirmation.png` manually.
Then answer the terminal prompt.

The boundary looks wrong.
Adjust the `sumo_boundary_*` buffer settings.
Set `boundary_force_rebuild: true`.
Run the pipeline again.

Old cached outputs appear.
Run with `--no-cache`.

Event extraction fails.
Install `pigz` and GNU `parallel`.
Or set `extract_events_script` to another compatible helper.
