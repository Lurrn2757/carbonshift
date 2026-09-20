import json
import math
import uuid
from datetime import datetime, timedelta, timezone

from .models import Forecast, Interval, Job, OptimizeRequest, Site
from .optimizer import optimize


def demo_request() -> OptimizeRequest:
    # Tomorrow, 09:00 IST. These are explicitly invented scenario values.
    ist = timezone(timedelta(hours=5, minutes=30))
    tomorrow = datetime.now(ist).date() + timedelta(days=1)
    start = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 9, tzinfo=ist)
    solar = [0.08, 0.12, 0.18, 0.23, 0.20, 0.10, 0.08, 0.08, 0.08]
    return OptimizeRequest(
        job=Job(name="90-minute-batch", earliest_start=start, deadline=start + timedelta(hours=9), duration_minutes=90, estimated_power_w=180),
        site=Site(name="Mumbai simulated solar site", base_load_kw=0.08, configuration_is_simulated=True),
        forecast=Forecast(
            source="simulated",
            generated_at=datetime.now(timezone.utc),
            note="Synthetic test scenario, not observed weather or measured solar output.",
            intervals=[Interval(start=start + timedelta(hours=i), end=start + timedelta(hours=i+1), estimated_solar_kw=power) for i, power in enumerate(solar)],
        ),
    )


def queue_demo_request(delay_seconds=10, run_seconds=5, idempotency_key=None):
    from .models import ExecutionSpec, SubmitRequest
    if not 5 <= delay_seconds <= 86400 or not 1 <= run_seconds <= 3600:
        raise ValueError("Delay must be 5–86400 seconds; run time must be 1–3600 seconds.")
    timeout = run_seconds + 10
    duration = math.ceil(timeout / 60)
    now = datetime.now(timezone.utc)
    start = now + timedelta(seconds=delay_seconds)
    end = start + timedelta(minutes=duration + 2)
    request = OptimizeRequest(
        job=Job(name="short-docker-demo", earliest_start=start, deadline=end,
                duration_minutes=duration, estimated_power_w=30),
        site=Site(name="Simulated site for execution demo", base_load_kw=0, configuration_is_simulated=True),
        forecast=Forecast(source="simulated", generated_at=now,
                          note="Flat zero-solar scenario so the short execution demo starts at earliest_start; 30 W is an illustrative estimate.",
                          intervals=[Interval(start=start, end=end, estimated_solar_kw=0)]),
    )
    return SubmitRequest(optimization=request, execution=ExecutionSpec(run_seconds=run_seconds, timeout_seconds=timeout),
                         idempotency_key=idempotency_key or uuid.uuid4().hex)


if __name__ == "__main__":
    print(json.dumps(optimize(demo_request()), indent=2))
