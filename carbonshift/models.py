from datetime import timezone
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Job(Model):
    name: str = Field(default="demo-job", min_length=1, max_length=100)
    earliest_start: AwareDatetime
    deadline: AwareDatetime
    duration_minutes: int = Field(gt=0, le=1440, strict=True)
    estimated_power_w: float = Field(gt=0, le=100000)

    @field_validator("earliest_start", "deadline")
    @classmethod
    def normalize_utc(cls, value):
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def check_window(self):
        seconds = (self.deadline - self.earliest_start).total_seconds()
        if seconds < self.duration_minutes * 60:
            raise ValueError("The job cannot finish before its deadline.")
        if seconds > 7 * 86400:
            raise ValueError("Preview horizon is limited to seven days.")
        return self


class Site(Model):
    name: str = Field(default="Configured site", min_length=1, max_length=100)
    # Non-job demand is served by solar first. No battery or export model in v0.1.
    base_load_kw: float = Field(default=0, ge=0, le=100000)
    configuration_is_simulated: bool = True
    grid_zone: str = Field(default="IN-WE", min_length=1, max_length=100)


class Interval(Model):
    start: AwareDatetime
    end: AwareDatetime
    estimated_solar_kw: float = Field(ge=0, le=100000)

    @field_validator("start", "end")
    @classmethod
    def normalize_utc(cls, value):
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def check_order(self):
        if self.end <= self.start:
            raise ValueError("Forecast intervals must have positive duration.")
        return self


class Forecast(Model):
    source: Literal["simulated", "open-meteo"]
    generated_at: AwareDatetime
    intervals: list[Interval] = Field(min_length=1, max_length=192)
    note: str = Field(max_length=2000)

    @model_validator(mode="after")
    def check_contiguous(self):
        for previous, current in zip(self.intervals, self.intervals[1:]):
            if previous.end != current.start:
                raise ValueError("Forecast must be ordered and contiguous, with no gaps or overlaps.")
        return self


class CarbonInterval(Model):
    start: AwareDatetime
    end: AwareDatetime
    intensity_gco2e_per_kwh: float = Field(ge=0, le=10000)

    @field_validator("start", "end")
    @classmethod
    def normalize_utc(cls, value):
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def check_order(self):
        if self.end <= self.start:
            raise ValueError("Carbon intervals must have positive duration.")
        return self


class CarbonForecast(Model):
    source: str = Field(min_length=1, max_length=100)
    is_simulated: bool
    grid_zone: str = Field(min_length=1, max_length=100)
    generated_at: AwareDatetime
    note: str = Field(min_length=1, max_length=2000)
    intervals: list[CarbonInterval] = Field(min_length=1, max_length=2016)

    @model_validator(mode="after")
    def check_contiguous(self):
        for previous, current in zip(self.intervals, self.intervals[1:]):
            if previous.end != current.start:
                raise ValueError("Carbon intervals must be contiguous and ordered.")
        return self


class OptimizeRequest(Model):
    job: Job
    site: Site
    forecast: Forecast
    step_minutes: int = Field(default=15, ge=1, le=60, strict=True)
    objective: Literal["solar", "carbon", "hybrid"] = "solar"
    carbon_forecast: CarbonForecast | None = None

    @model_validator(mode="after")
    def check_carbon(self):
        if self.objective != "solar" and self.carbon_forecast is None:
            raise ValueError("Carbon/hybrid objectives require a carbon forecast.")
        if self.carbon_forecast and self.carbon_forecast.grid_zone != self.site.grid_zone:
            raise ValueError("The carbon forecast zone must match the site grid zone.")
        return self


class ImageBatchSpec(Model):
    batch_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_edge: int = Field(default=1600, ge=64, le=4096, strict=True)
    quality: int = Field(default=80, ge=40, le=95, strict=True)


class ExecutionSpec(Model):
    workload: Literal["checksum-demo", "image-batch"] = "checksum-demo"
    run_seconds: int = Field(default=5, ge=1, le=3600, strict=True)
    timeout_seconds: int = Field(default=30, ge=6, le=3660, strict=True)
    image_batch: ImageBatchSpec | None = None

    @model_validator(mode="after")
    def check_timeout(self):
        if (self.workload == "image-batch") != (self.image_batch is not None):
            raise ValueError("Image batches require an image_batch specification; checksum jobs must omit it.")
        if self.timeout_seconds < self.run_seconds + 5:
            raise ValueError("Timeout must allow run_seconds plus five seconds of startup margin.")
        return self


class SubmitRequest(Model):
    optimization: OptimizeRequest
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")

    @model_validator(mode="after")
    def check_budget(self):
        if self.execution.timeout_seconds > self.optimization.job.duration_minutes * 60:
            raise ValueError("The reserved duration must cover the execution timeout.")
        return self


class LiveCarbonReading(Model):
    source: Literal["electricitymaps"] = "electricitymaps"
    source_endpoint: Literal["https://api.electricitymaps.com/v4/carbon-intensity/latest"] = "https://api.electricitymaps.com/v4/carbon-intensity/latest"
    data_kind: Literal["latest_observation"] = "latest_observation"
    is_simulated: Literal[False] = False
    used_for_scheduling: Literal[False] = False
    zone: str = Field(pattern=r"^[A-Z0-9][A-Z0-9_-]{0,63}$")
    carbon_intensity_gco2e_per_kwh: float = Field(ge=0, le=10000, strict=True)
    data_at: AwareDatetime
    retrieved_at: AwareDatetime
    provider_updated_at: AwareDatetime | None = None
    is_estimated: bool = Field(strict=True)
    estimation_method: str | None = Field(default=None, max_length=200)
    emission_factor_type: Literal["lifecycle"] = "lifecycle"
    flow_traced: Literal[True] = True
    temporal_granularity: Literal["hourly"] = "hourly"

    @field_validator("data_at", "retrieved_at", "provider_updated_at")
    @classmethod
    def normalize_reading_time(cls, value):
        return value.astimezone(timezone.utc) if value else None
