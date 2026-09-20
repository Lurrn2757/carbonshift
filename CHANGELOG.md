# v0.4.0 — September 19, 2026

- Image uploads, input hashes, bounded JPEG processing and result ZIP/report downloads.
- Main planner now supports custom deadlines and direct image batch preview/queue.
- Saved exact previews and idempotent submission; old checksum hashes remain compatible.
- Observed per-image progress, preserved execution logs and output validation.
- Private local API-key setup with environment override; existing observations retained.
- New workload image 0.4; source, queue schema and older data preserved.

# v0.3.1 — September 18, 2026

Preserves the v0.3 dashboard, execution planner, queue, Docker fixes and launcher.
Adds validated Electricity Maps v4 latest readings, separate JSONL observation
journal, carbon-now/carbon-log CLI commands and a Grid observations dashboard panel.
No latest reading is converted to a forecast or applied to scheduling. Key access
and a real provider response have not been authenticated in the build environment.

# v0.3.0 — September 17, 2026

Built on the v0.2 queue and worker; previous code and queue-history compatibility
are retained. No Electricity Maps dependency added.

- Added a local responsive dashboard with energy forecast charts, comparisons,
  editable synthetic scenarios, exportable plans, job activity and detail records.
- Connected short executable planning to submission: earliest-start check or
  one-minute simulated solar shift; preview and queue the exact same request.
- Added one-command startup, persistent worker heartbeat/readiness, automatic
  Docker readiness recovery and local host/origin checks.
- Preserved the fix for Docker local logging with max-file=1: compress=false.
- Added queue names, error guidance, pending cancellation, record export and
  explicit separation of forecast estimates from recorded runtime evidence.

## Manual acceptance on the execution host

1. Stop old CarbonShift processes and run bash start.sh from v0.3.
2. Check the default planner's 76% modeled grid reduction and no-solar's 0%.
3. In Run a real workload, choose Wait for solar, run 5 seconds, delay 15 seconds.
4. Preview: the recommended start should be one minute after earliest start.
5. Queue the plan while it is fresh. Keep the launcher running.
6. After the recommended start plus the run duration, open the record. Verify
   SUCCEEDED, exit 0, actual timestamps, and completed in logs.
7. Verify that the frozen forecast stays a prediction, and the runtime evidence
   does not claim measured energy/carbon savings.

Remaining restrictions and actual test coverage are in README.md and VERIFICATION.md.
