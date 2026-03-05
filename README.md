# matsim-sumo-pipeline
Reproducible pipeline to convert MATSim outputs into SUMO simulation inputs. The project filters and transforms demand data into SUMO-compatible network and route files to enable validation and comparison of MATSim-based demand within a microscopic SUMO framework.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Optional macOS speed dependencies

For faster extraction of large `output_events.xml.gz` files, install:

```bash
brew install pigz parallel
```

These are optional. `scripts/extract_entered_link_events_macos.sh` falls back to `gzip` + `awk` if they are not installed.
