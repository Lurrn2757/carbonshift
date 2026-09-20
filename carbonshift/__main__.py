"""Run a demo or a preview based on a live weather request."""

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.error import URLError

from .demo import demo_request, queue_demo_request
from .carbon import SimulatedCarbonProvider
from .forecast import fetch_open_meteo
from .models import Job, OptimizeRequest, Site, SubmitRequest
from .optimizer import optimize


def main(argv=None):
    parser = argparse.ArgumentParser(description="CarbonShift previews and persistent queue; run carbonshift.worker to execute jobs.")
    parser.add_argument("--db", help="Local queue database; defaults to CARBONSHIFT_DB or ~/.local/share/carbonshift/jobs.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    carbon_now = commands.add_parser("carbon-now", help="Fetch and journal a latest grid reading; never changes scheduling")
    carbon_now.add_argument("--zone", default="IN-WE")
    carbon_log = commands.add_parser("carbon-log", help="Read the separate local carbon observation journal")
    carbon_log.add_argument("--limit", type=int, default=50)
    app = commands.add_parser("app", help="Launch the local dashboard and Docker worker together")
    app.add_argument("--port", type=int, default=8088)
    app.add_argument("--no-worker", action="store_true", help="Serve only the dashboard; use a separately managed worker")
    demo = commands.add_parser("demo", help="Deterministic simulated solar scenario")
    demo.add_argument("--request", action="store_true", help="Print the input JSON for POST /api/optimize")
    demo.add_argument("--objective", choices=["solar", "carbon", "hybrid"], default="solar")
    live = commands.add_parser("live", help="Fetch live weather; site configuration remains a scenario unless --real-site is set")
    live.add_argument("--latitude", type=float, default=19.076)
    live.add_argument("--longitude", type=float, default=72.8777)
    live.add_argument("--pv-capacity-kwp", type=float, required=True)
    live.add_argument("--site-base-load-kw", type=float, required=True)
    live.add_argument("--estimated-power-w", type=float, required=True)
    live.add_argument("--duration-minutes", type=int, default=90)
    live.add_argument("--deadline-hours", type=float, default=24)
    live.add_argument("--performance-ratio", type=float, default=0.8)
    live.add_argument("--real-site", action="store_true", help="Assert that supplied site values describe your physical installation; output is still estimated")
    live.add_argument("--request", action="store_true", help="Print the full request JSON instead of the result")
    live.add_argument("--objective", choices=["solar", "carbon", "hybrid"], default="solar", help="Carbon/hybrid uses explicitly simulated carbon alongside live weather")
    queue = commands.add_parser("queue-demo", help="Queue a short real Docker job using a simulated energy scenario")
    queue.add_argument("--delay-seconds", type=int, default=10)
    queue.add_argument("--run-seconds", type=int, default=5)
    queue.add_argument("--request", action="store_true", help="Print a complete submit request without queuing")
    submit = commands.add_parser("submit", help="Persist a SubmitRequest JSON file")
    submit.add_argument("file")
    commands.add_parser("jobs", help="List up to 100 jobs")
    for command in ["job", "cancel"]:
        sub = commands.add_parser(command)
        sub.add_argument("job_id")
    commands.add_parser("doctor", help="Check Docker daemon and the locally built allowlisted image")
    args = parser.parse_args(argv)
    try:
        if args.command in ("carbon-now", "carbon-log"):
            from .evidence import append_observation, read_observations, reading_view
            if args.command == "carbon-now":
                from .carbon import fetch_electricitymaps_live
                reading = fetch_electricitymaps_live(args.zone)
                path = append_observation(reading)
                result = {"reading": reading_view(reading), "saved_to": str(path), "used_for_scheduling": False}
            else:
                result = read_observations(args.limit)
            print(json.dumps(result, indent=2, allow_nan=False))
            return 0
        if args.command == "app":
            from .launcher import launch
            launch(port=args.port, db=args.db, no_worker=args.no_worker)
            return 0
        if args.command in ("queue-demo", "submit", "jobs", "job", "cancel", "doctor"):
            from .store import Store
            if args.command == "doctor":
                from .docker_runner import DockerRunner
                result = DockerRunner().check()
            elif args.command == "queue-demo" and args.request:
                result = queue_demo_request(args.delay_seconds, args.run_seconds).model_dump(mode="json")
            else:
                store = Store(args.db)
                if args.command == "queue-demo":
                    result = store.submit(queue_demo_request(args.delay_seconds, args.run_seconds))
                elif args.command == "submit":
                    result = store.submit(SubmitRequest.model_validate_json(Path(args.file).read_text()))
                elif args.command == "jobs":
                    result = store.list()
                elif args.command == "job":
                    job_id = args.job_id
                    if job_id == "latest":
                        rows = store.list(limit=1)
                        if not rows:
                            raise ValueError("No jobs have been submitted.")
                        job_id = rows[0]["id"]
                    result = store.get(job_id)
                else:
                    result = store.cancel(args.job_id)
            print(json.dumps(result, indent=2, allow_nan=False))
            return 0
        if args.command == "demo":
            request = demo_request()
        else:
            if not 0 < args.deadline_hours <= 48:
                raise ValueError("For the live preview, deadline-hours must be in (0, 48].")
            now = datetime.now(timezone.utc)
            # Validate local configuration before making any network request.
            job = Job(
                name="live-weather-preview",
                earliest_start=now,
                deadline=now + timedelta(hours=args.deadline_hours),
                duration_minutes=args.duration_minutes,
                estimated_power_w=args.estimated_power_w,
            )
            site = Site(
                name="User-configured solar site",
                base_load_kw=args.site_base_load_kw,
                configuration_is_simulated=not args.real_site,
            )
            forecast = fetch_open_meteo(
                args.latitude, args.longitude, args.pv_capacity_kwp, args.performance_ratio,
            )
            request = OptimizeRequest(job=job, site=site, forecast=forecast)
        if args.objective != "solar":
            data = request.model_dump()
            data["objective"] = args.objective
            data["carbon_forecast"] = SimulatedCarbonProvider().forecast(request.job.earliest_start, request.job.deadline, request.site.grid_zone)
            request = OptimizeRequest.model_validate(data)
        result = request.model_dump(mode="json") if args.request else optimize(request)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        print(f"Cannot complete operation: {exc}", file=sys.stderr)
    except (URLError, TimeoutError) as exc:
        print(f"Weather unavailable: {exc}. Use 'python -m carbonshift demo' for a labeled simulation.", file=sys.stderr)
    except (RuntimeError, OSError) as exc:
        print(f"Operation failed: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
