# MATSim-SUMO Pipeline

This repository converts MATSim output into SUMO demand files.
It is designed as an installable Python package.
The main command is `matsim-sumo`.

Users provide MATSim output files and a SUMO network.
The pipeline generates a SUMO boundary.
It filters MATSim demand to this boundary.
It can scale demand.
It map-matches trip endpoints to SUMO edges.
It writes SUMO trip XML.

<p align="center">
  <img src="docs/pipeline_overview.png" alt="Pipeline overview" width="650">
</p>

## Main Workflow

The pipeline follows five logical steps.

| Step | Name | Purpose |
| --- | --- | --- |
| 0 | Initialization | Prepare the SUMO network and generate the boundary. |
| 1 | Demand Filtering | Select MATSim car demand inside the SUMO area. |
| 2 | Demand Scaling | Clone and perturb demand when scaling is required. |
| 3 | Map-Matching | Match trip endpoints to valid SUMO edges. |
| 4 | SUMO Trip Generation | Write SUMO trips and optionally run `duarouter`. |

## Repository Structure

```text
src/matsim_sumo/
  cli.py                 command line interface
  config/                YAML loading and validation
  pipeline/              pipeline runner, state, and numbered stages
  network/               SUMO network preprocessing and boundary generation
  demand/                demand helper logic
  io/                    file writing and QGIS export utilities
  visualization/         diagnostic plots
  tools/                 secondary analysis commands
  utils/                 shared helpers
```

## Quick Start

```bash
git clone <repo-url>
cd matsim-sumo-pipeline

python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp configs/templates/pipeline.yaml configs/my_run.yaml
matsim-sumo run --config configs/my_run.yaml
```

Edit `configs/my_run.yaml` before running.
Set the MATSim input folder.
Set the SUMO network path.
Set the output folder.

## Documentation

Use [`WIKI.md`](WIKI.md) for detailed usage instructions.
Use [`AGENT.md`](AGENT.md) for repository guidance for coding agents.
