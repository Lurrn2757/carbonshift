# CarbonShift next steps after v0.4

Delivered: useful bounded image workload, main planner to execution connection,
custom deadlines, exact saved previews, progress and downloadable verified results.
Local latest-carbon observations remain separate from scheduling.

Next gates:
1. Run a real image container on the user's local Docker daemon, download results,
   then verify restart reconciliation with a disposable job. No daemon is available
   in the build sandbox.
2. Verify an authorized Electricity Maps IN-WE reading with the user's own account.
3. Add retention controls for unreferenced uploads/previews and completed artifacts.
   Never prune data belonging to pending/running jobs or unresolved containers.
4. Use observed processing times to improve reservation suggestions; handle deadline
   risk explicitly. Add CPU/memory telemetry without relabeling it as measured power.
5. Add actual scheduling forecast adapters only with suitable access and coverage,
   keeping provider provenance/freshness explicit.
6. Calibrate energy estimates using a meter before claiming measured energy savings.

Continue with one worker/compute slot. Concurrent scheduling requires shared solar
and compute reservations. User code execution, public hosting, multiple accounts,
remote Docker daemons and automatic replanning remain outside this release.
