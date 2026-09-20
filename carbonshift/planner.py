"""Editable, explicitly synthetic scenarios for the local planning workspace."""

from typing import Literal
from pydantic import Field, AwareDatetime, model_validator
from .models import Model, OptimizeRequest, ImageBatchSpec
from .demo import demo_request
from .carbon import SimulatedCarbonProvider
from .demo import queue_demo_request
from .models import SubmitRequest
from datetime import timedelta


class PlanSettings(Model):
    earliest_start: AwareDatetime | None = None
    deadline: AwareDatetime | None = None
    name: str = Field(default="afternoon-batch", min_length=1, max_length=100)
    duration_minutes: int = Field(default=90, ge=1, le=540, strict=True)
    estimated_power_w: float = Field(default=180, gt=0, le=100000)
    base_load_kw: float = Field(default=0.08, ge=0, le=100000)
    pv_capacity_kwp: float = Field(default=0.5, ge=0, le=100000)
    scenario: Literal["sunny", "cloudy", "no-solar"] = "sunny"
    objective: Literal["solar", "carbon", "hybrid"] = "solar"

    @model_validator(mode="after")
    def check_dates(self):
        if (self.earliest_start is None) != (self.deadline is None):
            raise ValueError("Set both earliest start and deadline, or leave both empty.")
        return self


def build_plan(settings: PlanSettings) -> OptimizeRequest:
    data = demo_request().model_dump()
    data["job"].update(name=settings.name, duration_minutes=settings.duration_minutes,
                       estimated_power_w=settings.estimated_power_w)
    data["site"]["base_load_kw"] = settings.base_load_kw
    factor = {"sunny": 1, "cloudy": 0.3, "no-solar": 0}[settings.scenario]
    for interval in data["forecast"]["intervals"]:
        interval["estimated_solar_kw"] *= settings.pv_capacity_kwp / 0.5 * factor
    data["forecast"]["note"] = f"Synthetic {settings.scenario} scenario, scaled to the configured PV capacity. No weather API or measured solar data used."
    if settings.earliest_start is not None:
        from datetime import timezone
        import math
        start=settings.earliest_start.astimezone(timezone.utc)
        end=settings.deadline.astimezone(timezone.utc)
        if not 0 < (end-start).total_seconds() <= 48*3600:
            raise ValueError("Custom windows must be positive and at most 48 hours.")
        data["job"].update(earliest_start=start, deadline=end)
        intervals=[]; cursor=start
        while cursor<end:
            finish=min(end,cursor+timedelta(minutes=15))
            midpoint=cursor+(finish-cursor)/2
            local=midpoint+timedelta(hours=5,minutes=30)
            hour=local.hour+local.minute/60
            sun=max(0,math.sin(math.pi*(hour-6)/12)) if 6<hour<18 else 0
            intervals.append({"start":cursor,"end":finish,"estimated_solar_kw":settings.pv_capacity_kwp*factor*sun*0.8})
            cursor=finish
        data["forecast"]["intervals"]=intervals
        data["forecast"]["note"]="Invented daily solar curve for the chosen window (06:00–18:00 IST). No live weather or measured solar output; power and reservation length are estimates."
        data["step_minutes"]=1 if (end-start).total_seconds()<=3600 else 15
    data["objective"] = settings.objective
    if settings.objective != "solar":
        data["carbon_forecast"] = SimulatedCarbonProvider().forecast(
            data["job"]["earliest_start"], data["job"]["deadline"], data["site"]["grid_zone"])
    return OptimizeRequest.model_validate(data)


class RunSettings(Model):
    name: str = Field(default="checksum-batch", min_length=1, max_length=100)
    run_seconds: int = Field(default=5, ge=1, le=3600, strict=True)
    delay_seconds: int = Field(default=15, ge=5, le=86400, strict=True)
    estimated_power_w: float = Field(default=30, gt=0, le=100000)
    mode: Literal["immediate", "solar-shift"] = "immediate"


def build_execution_plan(settings: RunSettings) -> SubmitRequest:
    """A short executable scenario: same frozen plan is previewed and queued."""
    data = queue_demo_request(settings.delay_seconds, settings.run_seconds).model_dump()
    request = data["optimization"]
    request["job"].update(name=settings.name, estimated_power_w=settings.estimated_power_w)
    request["step_minutes"] = 1
    start, end = request["job"]["earliest_start"], request["job"]["deadline"]
    if settings.mode == "solar-shift":
        change = start + timedelta(minutes=1)
        request["forecast"]["intervals"] = [
            {"start": start, "end": change, "estimated_solar_kw": 0},
            {"start": change, "end": end, "estimated_solar_kw": settings.estimated_power_w / 1000},
        ]
        request["forecast"]["note"] = "Invented short scenario: solar becomes available one minute after earliest start. This demonstrates a real scheduling delay, not real solar availability. Prediction covers the full reservation including startup/timeout margin."
    else:
        request["forecast"]["note"] = "Zero-solar execution check; chooses earliest start. Power is a user estimate. Prediction covers the full reservation including startup/timeout margin."
    return SubmitRequest.model_validate(data)


class BatchPreviewSettings(Model):
    plan: PlanSettings
    batch: 'ImageBatchSpec'
    timing: Literal['recommended', 'earliest'] = 'recommended'


def build_batch_plan(settings):
    import uuid
    from .models import ExecutionSpec
    request = build_plan(settings.plan)
    seconds=request.job.duration_minutes*60
    if seconds>3600:
        raise ValueError('Image batches are limited to a 60-minute reservation.')
    if settings.timing=='earliest':
        data=request.model_dump()
        data['job']['deadline']=request.job.earliest_start+timedelta(seconds=seconds)
        request=OptimizeRequest.model_validate(data)
    return SubmitRequest(optimization=request,
        execution=ExecutionSpec(workload='image-batch',run_seconds=max(1,seconds-10),timeout_seconds=seconds,
                                image_batch=settings.batch),idempotency_key=uuid.uuid4().hex)
