# Euler

These scripts prepare ETH Euler for the Zürich MATSim–SUMO runs. Euler creates your account the first time you log in. This repository cannot log in for you: the first connection asks for your ETH password and a code sent to your ETH email.

Connect to the ETH network or VPN before using SSH.

## 1. Log in from your Mac

```bash
ssh -o PreferredAuthentications=keyboard-interactive -o PubkeyAuthentication=no YOUR_ETH_USERNAME@euler.ethz.ch
```

Accept the cluster rules and enter the access code from your ETH email. Later logins can use a normal SSH key.

## 2. Copy the project

On your Mac, from the repository root:

```bash
ETH_USER=YOUR_ETH_USERNAME bash cluster/euler/sync_to_euler.sh
```

The public GitHub `main` branch may be behind this project. Use the copy you have locally, or `rsync` as above, so Euler receives `networks/zurich/zurich.net.xml`.

## 3. Install the Python package

On Euler, on a login node:

```bash
cd ~/matsim-sumo
bash cluster/euler/setup_euler.sh
```

This loads `stack/2025-06`, `python/3.12.8`, and `sumo/1.22.0`, then installs the package into `~/venvs/matsim-sumo`. If `sumo/1.22.0` is missing from that stack, the script tries `stack/2024-06`. Check `module spider sumo` when both fail.

## 4. Run a short simulation

Still in `~/matsim-sumo`:

```bash
sbatch cluster/euler/smoke_sumo.sbatch
squeue --me
```

The job loads the Zürich network, builds 30 passenger trips, and simulates 300 seconds. Results go to `$SCRATCH/matsim-sumo/smoke/<jobid>/`. Scratch files are removed by Euler after two weeks. Read the log with:

```bash
cat slurm-smoke-<jobid>.out
```

Do not start `sumo` directly on a login node. Login nodes are shared.

## 5. Run the pipeline

Put the MATSim and SUMO inputs under `data/inputs/` as described in the repository README. Copy `configs/templates/pipeline.yaml` to `configs/my_run.yaml`, then:

```bash
sbatch cluster/euler/run_pipeline.sbatch
```

The pipeline job asks for 2 CPUs, 8 GB per CPU, and 4 hours. Raise `#SBATCH --time` and `--mem-per-cpu` in the script if a full-day scenario needs more. Leave `#SBATCH --account` unset unless your group has given you a shareholder account name.
