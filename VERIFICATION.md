# Verification — CarbonShift v0.4.0

September 19, 2026. Python 3.12, Pillow 12.3.0, Pydantic 2.13.5.

## Automated results

- 108 Python/HTTP tests passed, none skipped. All 91 prior tests retained.
- 17 image/configuration tests cover real PNG decoding, resizing, JPEG encoding,
  white transparency flattening, ZIP/report creation, input hash validation,
  upload and pixel/body limits, path restrictions, local-origin enforcement,
  frozen preview submission, retries, expiry, changed planner results, retained
  checksum hashes, progress, artifact validation/download, and key permissions.
- The image processor ran as a real local Python subprocess in these tests.
  HTTP upload, preview, SQLite queue, reopen and ZIP download used actual code.
  Docker inspection/timestamps and worker completion were simulated by test adapters.
- Chromium/Playwright checks passed for the original forecast scenarios, exports,
  checksum preview/queue/cancel, carbon observation fixtures, ten-image upload,
  batch preview, invalidation after editing, queue/cancel, mobile width and no JS errors.
- Browser queue tests stub only worker readiness while exercising the real API
  and temporary SQLite database. A separate explicitly marked completed-job fixture
  checks progress rendering, filename escaping and the results download link.
  That UI fixture does not claim real container execution or a real ZIP transformation.
  Actual ZIP transformation and download integrity are checked by the Python tests.
- Desktop and mobile screenshots were inspected. They show the actual unavailable
  Docker/API-key state and cancelled test jobs, not staged successful live runs.
- Python compilation, JavaScript syntax and shell syntax checks passed.

## Remaining host checks

Docker is not installed in this build runtime. The new image has not been built
or run against a real Docker daemon here. Verify that image build, bind mounts,
UID permissions, resource limits, real-job completion and restart reconciliation
work on your Linux execution host. Keep the first batch small and inspect results.

No API key was supplied. Electricity Maps responses were mocked; authenticated
IN-WE access and a real reading remain account-specific checks. Key configuration
saves locally and does not itself verify provider access. Dashboard forecasts
remain simulated; live observations are not scheduling forecasts.

No physical power measurement, measured avoided emissions, multi-host execution,
load testing or production/public hosting validation is claimed.
