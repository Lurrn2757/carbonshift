# CarbonShift v0.3.1 — local scheduling workspace

A working dashboard for the Python scheduler: editable forecast scenarios,
energy comparisons, a real Docker execution check, and persistent job records.
No external data API is needed for planning or Docker execution. Optional
Open-Meteo CLI support is retained. v0.3.1 adds Electricity Maps latest observations
and a separate local journal; scheduling carbon forecasts remain simulated.

## Start here

Stop the old CarbonShift worker and API with Ctrl+C in their terminals. The queue
history is shared between versions; do not run both versions against it at once.

```bash
unzip "$HOME/Downloads/CarbonShift_v0.3.1.zip" -d "$HOME/Projects"
cd "$HOME/Projects/carbonshift-v0.3.1"
bash start.sh
```

Open **http://127.0.0.1:8088**. Keep that terminal running.

The script creates a Python virtual environment, installs required packages if
missing, builds `carbonshift-workload:0.2` if Docker is available and the image is
missing, then launches the dashboard and worker together. The image tag remains
0.2 because the workload itself has not changed. The Docker logging fix is in
the Python runner and applies to newly created containers.

First-time package installation and image build need internet access. After
setup, the dashboard's simulated planner and local job execution need no data API.
Requirements: Linux, Python 3.10+, and Docker for execution. If Docker is missing,
the planner still opens and the runner explains why execution is unavailable.

On later launches:

```bash
cd "$HOME/Projects/carbonshift-v0.3.1"
bash start.sh
```

If port 8088 is occupied, stop the old CarbonShift API or use `bash start.sh --port 8089`.
No Node, npm, frontend build step, or separate worker terminal is required.

## What to try

1. **Schedule planner:** the initial sunny 90-minute / 180 W scenario should show
   76% lower modeled grid energy by shifting from 09:00 to 12:00 IST tomorrow.
   Change duration, power, solar capacity, base load, scenario, or objective and
   click **Find best window**. Edited inputs dim the previous result until recalculated.
2. Choose **No solar available** with the solar objective. The result should be
   0% reduction and no delay. Zero is a legitimate result.
3. Choose **Lowest emissions · solar + grid** to explore invented carbon intensity
   alongside solar. The chart adds a separate carbon axis. These are not Indian
   grid observations or measured savings.
4. **Run a real workload:** set runtime to 5 seconds and earliest-start delay to
   15 seconds. Choose **Wait for solar** and click **Preview execution plan**.
   The invented short solar scenario recommends waiting one additional minute.
   Review that time, then click **Queue this plan** once the worker is ready.
   The queue preserves exactly the displayed execution plan. Alternatively choose
   **Earliest start** for a zero-solar execution check without the additional delay.
   The one-minute reservation includes startup/timeout margin; actual CPU runtime
   is five seconds. Predictions over the reservation are not measured run savings.
5. **Job activity:** watch the job, open its record, inspect timestamps, logs and
   exit code. Success means `SUCCEEDED`, exit code 0, and a `completed` log entry.
   Cancel is available before a pending job is claimed. Export plans and job records
   as JSON for further development.

The only executable workload currently allowed is the bundled CPU checksum batch.
The dashboard is not yet an arbitrary user-code scheduler. Section 01 is a
long-horizon planning workspace; section 02 previews and submits its own short
executable scenario. Both use the same optimizer and queue models. Editing execution
inputs requires a fresh preview. Expired plans must be refreshed before submission.
Advanced callers can also submit validated requests through the existing API.

## Data and worker behavior

SQLite history stays at `~/.local/share/carbonshift/jobs.sqlite3`, outside the
versioned project folder. v0.2 records are readable in v0.3. New worker metadata
uses the existing metadata table without rewriting jobs. Back up a database only
while the worker and API are stopped, using SQLite's backup tooling if needed.

One local worker owns the queue lock. It checks Docker and the image every five
seconds and writes a heartbeat. Heartbeats older than 45 seconds are treated as
stale. Readiness checks daemon/image availability; the first real workload verifies
container execution. A missing image does not block reconciliation of a container
that is already running. The dashboard polls records every two seconds.

Existing containers with the old `max-file=1` compression error cannot have their
logging options changed in place. The v0.3 worker leaves expired containers stopped
and records MISSED_WINDOW; submit a fresh job once ready. It does not delete history.

Ctrl+C stops the dashboard and its child worker. An already-running container
keeps its own bounded runtime and is reconciled on the next launch. Keep the
worker running for timeout enforcement. Pending jobs can miss their windows if
the app is closed. Foreign/missing containers are never blindly restarted.
RECOVERY_REQUIRED still requires operator investigation; there is no automatic
resolution or running-job cancellation workflow in this release.

## Development commands

```bash
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m carbonshift app
```

For a separate managed worker, use `python -m carbonshift app --no-worker` and
`python -m carbonshift.worker` in separate terminals with the same database.
For an isolated database:

```bash
export CARBONSHIFT_DB="$HOME/.local/share/carbonshift/dev.sqlite3"
python -m carbonshift app
```

The API is local only and has no multi-user authentication. Host/origin checks
prevent unrelated websites from submitting browser mutations. Do not expose it
as a public service. API docs remain at `/docs`.

New routes:

| Route | Purpose |
| --- | --- |
| GET / | Dashboard |
| POST /api/planner | Editable simulated scenario + optimizer result; no queue writes |
| GET /api/system | Worker heartbeat, readiness, and counts across all jobs |
| POST /api/execution/preview | Exact submission payload and recommendation for a short executable scenario |

Existing optimize, forecast, queue, cancellation and detail routes remain.
The CLI and underlying contracts are described in BACKEND_REFERENCE.md; its
v0.2 setup instructions are historical. Use the startup instructions above.

## Scheduling forecast integration later

`carbonshift/carbon.py` retains the `CarbonProvider` protocol. A real provider
must return validated timestamps, regional identity, gCO2e/kWh units, freshness
metadata, and explicit provenance. The optimizer and queue remain independent of
provider credentials. No silent simulation fallback is allowed.

Runtime-based energy is configured watts × Docker runtime, not a power-meter
reading. Physical power measurement, CPU/memory telemetry, additional workloads,
multi-host scheduling and measured carbon savings remain future work.

See VERIFICATION.md for what was actually tested and BUILD_PLAN.md for next work.

## Optional browser regression check

`tests/browser_smoke.mjs` starts the app against a temporary database and checks
the UI with Playwright. Install Playwright and its Chromium browser in a separate
development environment, then run `node tests/browser_smoke.mjs` with that package
available to Node. A custom binary can be supplied through CHROMIUM_EXECUTABLE.
This is optional developer tooling, not a runtime dependency. The test uses explicit provider fixtures for the observations panel and mocks
worker readiness for queue submission; it does not run a Docker workload or
contact Electricity Maps. Its database and observation journal are temporary.

## Optional Electricity Maps observations (new in v0.3.1)

This adds current grid context, not a forecast or a job-emissions measurement.
The app calls only the documented v4 carbon-intensity/latest endpoint. It never
feeds that single reading into the optimizer, changes schedules, or rewrites jobs.

The earlier claim that a permanent free personal tier is available with precisely
latest/history entitlement was not verified. As checked September 18, 2026, the
public documentation and pricing page advertise a 14-day API trial. Your actual
zone/signal permissions depend on your account. Verify access in the provider
portal; do not assume forecast access or an ongoing free entitlement.

Sources:
- [Current API docs](https://app.electricitymaps.com/docs)
- [Pricing and trial](https://www.electricitymaps.com/pricing)
- [Latest endpoint and response metadata](https://app.electricitymaps.com/docs/reference/carbon-intensity/latest)
- [Authentication](https://app.electricitymaps.com/docs/quickstart/authorization)
- [Zone coverage](https://app.electricitymaps.com/coverage)

IN-WE is listed as Western India. Coverage alone does not establish your account's
entitlement. The client requests hourly, lifecycle, consumption-based intensity
in gCO2e/kWh for that exact zone, with IP-based fallback disabled. It validates
response metadata rather than silently accepting a different signal or region.

### Set the key locally

Stop the app first. In the terminal from which you launch it:

```bash
cd "$HOME/Projects/carbonshift-v0.3.1"
read -rsp "Electricity Maps API key: " ELECTRICITYMAPS_API_KEY
printf '\n'
export ELECTRICITYMAPS_API_KEY
bash start.sh
```

The input is hidden and the value is not embedded in your shell command history.
The server reads the environment key. It is not sent to browser JavaScript, stored
in an observation, passed on the command line, or printed in provider errors.
The subprocess worker inherits the server environment; this is a local trusted
application, not a secret-management system. Docker workload environments are not
populated from the host environment.

Open Grid observations, use IN-WE, and click Fetch & save reading. Only that
explicit action calls Electricity Maps. The rest of the dashboard still works
without a key. Missing key, denied access, rate limits, malformed responses and
outages show errors; they never become fake live data. Old saved records remain
visibly saved observations when a new fetch fails.

### CLI

In a terminal with the key exported:

```bash
source .venv/bin/activate
python -m carbonshift carbon-now --zone IN-WE
python -m carbonshift carbon-log --limit 20
```

`carbon-now` validates and appends one observation. `carbon-log` reads the journal
without making a provider call or needing a key. Reading time (`data_at`) and our
retrieval time (`retrieved_at`) remain distinct. `is_estimated` and the provider's
estimation method are retained. A latest reading can still be estimated or stale.

### Separate journal

Default: `~/.local/share/carbonshift/carbon_observations.jsonl`.
Override with `CARBONSHIFT_OBSERVATIONS=/absolute/path/observations.jsonl`.
This path persists across application source-version upgrades. It must be distinct
from the queue database. SQLite files and a matching queue path are rejected.
Writes are append-only in normal application operation, file-locked, flushed and
fsynced. A partial final record blocks further appends so it can be preserved and
investigated. Readback reports invalid records rather than silently deleting them.

Every successful pull is a retrieval record. Multiple pulls can share one provider
timestamp; they are not new independent samples. The last 20 records are displayed
and exportable in the dashboard. Reload the page to see CLI-appended records.
Staleness uses a 3-hour local display threshold, not a provider freshness guarantee.
No stale observation is used for scheduling.

This is an editable local journal, not a cryptographically tamper-proof audit log.
It records what the application retrieved; it does not prove measured carbon
savings. All shipped tests use clearly named fixtures and temporary journals.
The release contains no fake readings preloaded as real evidence.

Additional local routes:
- GET /api/carbon/observations?limit=20 — saved readings and key-configured boolean.
- POST /api/carbon/observe?zone=IN-WE — explicit provider fetch and journal append.

The server returns an error if persistence fails, even if retrieval succeeded.
No provider credentials or raw error bodies are included in public errors.
