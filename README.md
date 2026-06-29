# MATSim-SUMO Pipeline

This repository contains an installable Python package for converting MATSim output into SUMO demand files.
The public command is `matsim-sumo`.

The pipeline reads MATSim plans, trips, events, and network files together with a SUMO network.
It generates a SUMO boundary, filters MATSim car demand to that boundary, optionally scales demand, map-matches trip endpoints to SUMO edges, writes SUMO trip XML, and can run `duarouter`.
The numbered pipeline steps are the steps shown in the overview figure and stop at Step 4.

<p align="center">
  <img src="docs/pipeline_overview.png" alt="Pipeline overview" width="650">
</p>

## Install

```bash
git clone <repo-url>
cd matsim-sumo-pipeline

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

SUMO command-line tools are not installed by Python.
Install SUMO separately if you want to run `duarouter` or SUMO travel-time analysis.

## Input Layout

Use this local data layout for pipeline runs:

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

Large input data under `data/` is intentionally ignored by Git.
Amenities and Microcensus data are not required for a default run.
Amenities are used only when `amenity_kdtree_enabled: true`.
Mode-share diagnostics are used only when `mode_share_diagnostics_enabled: true` or when running `matsim-sumo analyze-mode-shares` directly.

## Run

Copy the template and edit paths for your data:

```bash
cp configs/templates/pipeline.yaml configs/my_run.yaml
matsim-sumo run --config configs/my_run.yaml
```

The compatibility wrapper is also available:

```bash
python scripts/run_pipeline.py --config configs/my_run.yaml
```

`scripts/` intentionally contains only this wrapper.
All reusable code lives in `src/matsim_sumo/` or `analysis/`.

## Outputs

Pipeline outputs are written below the configured `output_dir`, normally under `results/`.
Generated outputs, caches, local data, and notebooks are ignored by Git.
Only source code, configuration templates/examples, documentation, tests, and lightweight reference files should be committed.

## Documentation

See [WIKI.md](WIKI.md) for full installation, configuration, run, output, and troubleshooting notes.
See [AGENT.md](AGENT.md) for repository guidance for coding agents.
