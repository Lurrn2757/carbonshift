# CarbonShift v0.4 — schedule a useful image batch

Local energy-aware scheduling with an image-processing workload, a persistent
single-worker queue, downloadable results and optional Electricity Maps observations.
Existing checksum execution, forecast exploration, Open-Meteo CLI and carbon journal
are preserved. Planning forecasts in the dashboard remain explicitly simulated.

## Start

Stop the old API and worker with Ctrl+C. Do not run two versions against the same queue.

```bash
unzip "$HOME/Downloads/CarbonShift_v0.4.zip" -d "$HOME/Projects"
cd "$HOME/Projects/carbonshift-v0.4"
bash start.sh
```

Open http://127.0.0.1:8088. The launcher creates a virtual environment, installs
requirements, builds the new `carbonshift-workload:0.4` image when missing, and starts
the local API and worker. Keep the terminal running. First setup requires internet;
forecast exploration and image execution then need no external data API.

Requirements: Linux, Python 3.10+, local Docker daemon for execution, and free disk
space. Docker Desktop/remote Docker contexts are not supported by this release's
host bind mounts. The planner remains usable when Docker is unavailable.
If necessary, inspect `docker info` or rebuild explicitly:

```bash
docker build -t carbonshift-workload:0.4 ./workloads
```

## First useful run

1. In Schedule planner, select **Resize & compress images**.
2. Choose up to 20 JPEG/PNG files. For a quick check, start with ten small photos.
3. Leave longest edge at 1600 pixels and JPEG quality at 80, or change them.
4. Click **Set a quick test**: earliest start in two minutes, deadline in ten.
5. Set **Start choice → Earliest permitted start** and **Duration → 2 minutes**.
6. Click **Find best window**. Inspect the energy comparison and selected batch time.
7. Click **Queue this exact batch** once Docker is ready.
8. Open the record in **Job activity**. It shows waiting/starting/processing state,
   processed/total count when observed, logs and actual container runtime.
9. After `SUCCEEDED`, click **Download processed images**. The ZIP includes numbered
   JPEGs and `report.json`, which maps originals to outputs, sizes and dimensions.

Images may finish before the next two-second dashboard poll, so a small batch may
jump directly to completion. Progress is persisted from bounded container logs.

Select **Recommended energy window** for subsequent runs to use the optimizer's
selected time. Choose a longer deadline if you want to explore delaying into an
invented daytime solar window. No solar or an already-optimal start can legitimately
produce zero reduction. Custom windows are limited to 48 hours. All displayed
schedule timestamps are IST; datetime input controls use the browser's local timezone.

The image reservation is 1–60 minutes and is a hard execution budget, not a measured
processing estimate. Energy predictions cover that entire reservation; the results
record separately estimates total energy from configured watts × actual container
runtime. Neither number is a power measurement or proof of avoided emissions.

## Files and execution

- 1–20 single-frame JPEG/PNG images, up to 5 MiB and 8 megapixels each, 20 MiB total.
- Output JPEG longest edge 64–4096 px, quality 40–95, aspect ratio preserved, no upscaling.
- Transparency is flattened on white; EXIF metadata is stripped. Output bytes can
  increase for some inputs. Originals are not overwritten.
- Input bytes and manifest are hashed. Missing/changed inputs fail validation before
  queue submission and container creation. Preview contents are saved server-side;
  queue submission identifies that saved preview and reuses its idempotency key.
- One compute slot; conflicting reservations are rejected rather than silently moved.
- Image container: one CPU, 512 MiB RAM, no network, unprivileged UID, dropped
  capabilities, read-only root, read-only input mount, only its dedicated output
  directory writable. The checksum workload retains its 128 MiB limit.
- The fixed workload bounds output to 64 MiB and writes a ZIP atomically. The worker
  validates the archive, settings and input identity before reporting success.
  Exit code 0 without valid results becomes FAILED. Downloads verify the completed
  archive hash; partial/failed results are not offered as successful downloads.
- Data stays outside version folders: jobs at `~/.local/share/carbonshift/jobs.sqlite3`,
  assets at `~/.local/share/carbonshift/assets`, carbon journal at
  `~/.local/share/carbonshift/carbon_observations.jsonl`.
- `CARBONSHIFT_DB`, `CARBONSHIFT_ASSETS` and `CARBONSHIFT_OBSERVATIONS` override those
  paths. API and worker must use the same local paths. Asset paths must not contain
  commas. Keep the asset root private and on the execution host.
- Uploads, previews, result ZIPs and stopped containers are retained; automatic
  retention cleanup is not implemented. Monitor disk use. Do not delete referenced
  files while jobs are queued or running. No arbitrary user scripts are accepted.

Existing job history and checksum idempotency hashes remain compatible. A previously
created Docker container keeps its existing image. The new runner uses image 0.4 for
new containers. Stop the old version before upgrading; source folders are separate.

## API keys — configure once

Only Electricity Maps needs a key in this application. Obtain yours through the
[official API account page](https://app.electricitymaps.com/docs/quickstart/authorization).
Current public docs advertise a 14-day trial; verify your account's actual IN-WE and
signal access. An account key does not guarantee access to every forecast endpoint.

From the new project folder:

```bash
bash configure-api.sh
```

Paste your key into the hidden prompt and press Enter. It is saved with mode 0600 to
`~/.config/carbonshift/electricitymaps.key`, outside the project. This is a private
local plaintext file, not an encrypted vault. Never send the key in chat, commit it
or include it in a screenshot. Configuration itself makes no external API request.

Restart the app with `bash start.sh`. Open **Grid observations**, leave the zone as
`IN-WE`, and click **Fetch & save reading**. The server sends the key as the documented
`auth-token` header. It never sends the key to dashboard JavaScript, jobs, observation
records or Docker workloads. A configured-key label confirms local configuration,
not provider authorization; the first successful fetch proves your request is allowed.

The environment variable `ELECTRICITYMAPS_API_KEY` overrides the saved file, even
when explicitly empty. If you previously exported another value, run
`unset ELECTRICITYMAPS_API_KEY` before launching to use the saved key.
Rotate by running the configuration script again, then restart. Remove the saved key
file and unset the environment variable to disable access. Saved observations remain.

CLI after first setup:

```bash
source .venv/bin/activate
python -m carbonshift carbon-now --zone IN-WE
python -m carbonshift carbon-log --limit 20
```

401 means key rejected; 403 means check account entitlement; 429 means rate-limited.
Provider errors do not generate fake readings. Latest observations can be estimated
or stale and are never reused as scheduling forecasts. Carbon records use a separate
JSONL journal; repeated retrievals may contain the same provider timestamp.

## Weather API

The retained Open-Meteo integration uses its public endpoint and requires no API key
in this implementation. It is currently a CLI preview, separate from the dashboard's
simulated planner. Respect provider usage terms for your deployment. Example:

```bash
source .venv/bin/activate
python -m carbonshift live --latitude 19.076 --longitude 72.8777 \
  --pv-capacity-kwp 0.5 --site-base-load-kw 0.08 \
  --estimated-power-w 180 --duration-minutes 90 --deadline-hours 24
```

This estimates solar power from weather; it is not measured PV output or a grid-carbon
forecast. Electricity Maps latest-reading access does not enable live forecast scheduling.

## Development and verification

```bash
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

See VERIFICATION.md for actual test results and remaining Docker/API checks.
`tests/browser_smoke.mjs` is optional Playwright tooling; Node is not needed to run
the application. It uses temporary data and explicit fixtures. The older CLI/queue
reference is BACKEND_REFERENCE.md; V031_REFERENCE.md preserves the previous release
documentation. Use this README for current startup and configuration instructions.

New routes: POST /api/batches, POST /api/batches/preview,
POST /api/batches/queue/{preview_id}, GET /api/jobs/{job_id}/results.
The existing optimize, forecast, queue, cancel and carbon-observation routes remain.
API docs: http://127.0.0.1:8088/docs. Local only; no multi-user authentication or public hosting.
