# CarbonShift backend reference (v0.2 architecture, retained in v0.3)

This release adds a Docker execution path, a SQLite queue, and a replaceable
simulated carbon provider. Electricity Maps is deferred. The working Open-Meteo
weather integration remains optional; offline demos need no data API.

See README.md for the v0.3 dashboard and launcher. See VERIFICATION.md for current test results. This document describes the underlying queue and CLI.

## Install

Save CarbonShift_v0.3.zip in Downloads. Extract into its own directory:

```bash
mkdir -p "$HOME/Projects"
unzip "$HOME/Downloads/CarbonShift_v0.3.zip" -d "$HOME/Projects"
cd "$HOME/Projects/carbonshift-v0.3"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Python 3.10+ and Linux are required. CLI, queue, and worker need only
requirements-core.txt. Use requirements-dev.txt to include FastAPI and HTTP tests.
Use one machine, one Docker daemon, and one queue DB. No Proxmox changes are needed.

## Preview with simulated carbon

```bash
python -m carbonshift demo --objective hybrid
```

The output includes carbon_source: simulated-carbon-v1 and carbon_is_simulated:
true. The carbon values are invented, not Indian grid observations. Solar/site
values are also simulated in this demo.

| Objective | Minimize | Local solar |
| --- | --- | --- |
| solar | Estimated grid energy, kWh | Included |
| carbon | Estimated grid emissions, gCO2e | Ignored; grid-only mode |
| hybrid | Estimated grid emissions, gCO2e | Included |

Each candidate covers the whole duration and meets the deadline. Solar and carbon
interval boundaries are aligned before integration. Earliest start wins ties.
Candidate spacing defaults to 15 minutes; hourly input remains hourly.
Hybrid can increase grid kWh while reducing estimated emissions, so its signed
energy reduction may be negative.

## Run a short Docker workload

Build the allowlisted image. The initial build needs the Python base image;
subsequent jobs do not pull images or access the network.

```bash
docker build -t carbonshift-workload:0.2 ./workloads
python -m carbonshift doctor
```

Doctor must report the Docker server and workload image. Resolve any daemon,
permission, or missing-image error before submitting. Use the same Linux user and
Docker context for the build and worker. Keep the dev API and Docker daemon local.

Start the worker and leave it running:

```bash
python -m carbonshift.worker
```

In a second terminal:

```bash
cd "$HOME/Projects/carbonshift-v0.3"
source .venv/bin/activate
python -m carbonshift queue-demo --delay-seconds 10 --run-seconds 5
```

This persists a job about ten seconds ahead. The checksum calculation runs for
about five seconds within a one-minute reservation that allows startup/timeout
margin. The energy scenario has zero solar so it chooses the earliest start.
Its 30 W power value is illustrative, not a measurement of your machine.

After the run, inspect:

```bash
python -m carbonshift jobs
python -m carbonshift job latest
```

Expected states: SCHEDULED, STARTING, RUNNING, SUCCEEDED.
Look for exit_code 0, a Docker container ID, actual start/finish timestamps, and
logs with an event of completed. Short tasks can finish between polls; actual
Docker timestamps are retained even if a state is not observed.

Worker --once performs one poll, not a complete run-and-wait cycle.

## Storage, reservations, and recovery

Default DB: ~/.local/share/carbonshift/jobs.sqlite3. API and worker share it, so
history lives outside versioned source directories. The DB is created when a queue
operation is used. To override it, set the same value in every terminal:

```bash
export CARBONSHIFT_DB="$HOME/.local/share/carbonshift/lab.sqlite3"
```

Explicit options are also supported:

```bash
python -m carbonshift --db /absolute/path/lab.sqlite3 jobs
python -m carbonshift.worker --db /absolute/path/lab.sqlite3
```

DB timestamps are Unix seconds in UTC; prediction/forecast timestamps are
ISO 8601. Use a local filesystem. Do not copy a live DB to another host or change
its Docker context.

- Complete inputs and the recommendation are frozen at submission.
- Identical requests with the same idempotency key return the existing job.
  The same key with different input is rejected.
- Overlapping reservations are rejected; v0.3 does not automatically replan.
  New submissions also wait while a job is active or unresolved.
- A local filesystem lock prevents multiple workers on the same queue.
- Stable names and queue/job labels support recovery after interrupted create/start.
- Daemon errors hold the reservation; they never mean the container is absent.
- A confirmed missing tracked container becomes LOST. Its outcome is unknown;
  it is never automatically rerun.
- Identity mismatches and unexpected states become RECOVERY_REQUIRED. The queue
  holds for investigation; v0.3 has no automated operator resolution workflow.
- Terminal jobs are immutable. New attempts require new submissions.
- Containers are retained after exit for inspection; no bulk cleanup is performed.

Cancel a pending job using its returned ID:

```bash
python -m carbonshift cancel JOB_ID
```

Only SCHEDULED jobs can be cancelled in v0.3. Ctrl+C stops the worker loop but
leaves an already-running container for reconciliation after restart. The demo
workload has its own bounded runtime.

Timeout enforcement needs the worker and Docker daemon to be reachable.
Polling and stop grace add latency; this is not hard real-time scheduling.
Overruns and missed windows observed after downtime are recorded explicitly.

## Local HTTP API

For development with a separate worker, stop the combined launcher first:

```bash
source .venv/bin/activate
python -m uvicorn carbonshift.api:app --host 127.0.0.1 --port 8088 --reload
```

Open [API docs](http://127.0.0.1:8088/docs). Health must report version 0.3.0;
API health does not imply the separate worker is running.

| Endpoint | Purpose |
| --- | --- |
| GET /health | API version/capability |
| GET /api/demo?objective=hybrid | Simulated hybrid preview |
| GET /api/demo/request?objective=hybrid | Optimization inputs |
| POST /api/optimize | Preview; no persistence |
| GET /api/forecast | Optional live Open-Meteo solar estimate |
| GET /api/jobs/demo/request?delay_seconds=60 | Example submission |
| POST /api/jobs | Persist and reserve |
| GET /api/jobs | Recent summaries |
| GET /api/jobs/{job_id} | Inputs, prediction, evidence, logs, events |
| POST /api/jobs/{job_id}/cancel | Cancel pending job |

422 means invalid input. 409 indicates overlap, past start, or idempotency
conflict. Generate a fresh future-start request if you waited too long.
The separate worker must be started explicitly when using this direct Uvicorn command; reload never starts it. The v0.3 app launcher manages its own child worker.
The API has no multi-tenant authentication and is intended for loopback development.

## Live weather with simulated carbon

```bash
python -m carbonshift live \
  --pv-capacity-kwp 0.5 \
  --site-base-load-kw 0.08 \
  --estimated-power-w 180 \
  --duration-minutes 90 \
  --deadline-hours 24 \
  --objective hybrid
```

Only weather is fetched live here. Carbon and site configuration remain simulated;
power is user-estimated. This command remains a preview and never queues a job.

## Swap the provider later

carbonshift/carbon.py defines:

```python
forecast(start, end, grid_zone) -> CarbonForecast
```

A future Electricity Maps adapter returns timezone-aware intervals in gCO2e/kWh,
provider/retrieval metadata, the verified grid zone, and is_simulated=False.
Select it in the CLI/API service layer. The optimizer, queue, and worker keep the
same validated model.

Before enabling it, verify authentication, coverage, units, freshness, and service
limits. Missing/stale data must be explicit. Never substitute simulation silently
or treat a current carbon reading as a future forecast. No new API key is needed now.

## Evidence and limits

Solar uses a basic horizontal-irradiance PV approximation. Base load consumes
solar first. Batteries, export credits, changing base load, and site-wide
counterfactual emissions are not modeled.

Execution evidence includes actual Docker runtime. Energy is still configured
power multiplied by runtime. CPU/memory telemetry and physical power measurements
are not included yet. Predictions are never relabeled as realized or measured
savings. Carbon intensity estimates do not prove marginal emissions avoided.

Dependencies have compatibility ranges rather than a fully verified environment
lock. v0.3 adds a local dashboard. Billing, multi-host control, and deployment remain out of scope.

## References

- [Open-Meteo docs](https://open-meteo.com/en/docs): solar variables and preceding-hour semantics. Retain Open-Meteo attribution.
- [Docker create](https://docs.docker.com/reference/cli/docker/container/create/): create/start separation and options.
- [Docker resource limits](https://docs.docker.com/engine/containers/resource_constraints/): CPU and memory limits.

See BUILD_PLAN.md and VERIFICATION.md for the remaining work and local gates.
