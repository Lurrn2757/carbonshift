"""Single-job, non-preemptive schedule preview. No execution or reservations."""

from datetime import timedelta
from bisect import bisect_right

from .models import OptimizeRequest


def optimize(request: OptimizeRequest) -> dict:
    job, site, forecast = request.job, request.site, request.forecast
    duration = timedelta(minutes=job.duration_minutes)
    latest_start = job.deadline - duration
    intervals = forecast.intervals
    carbon = request.carbon_forecast
    # Require complete coverage, including the immediate-run baseline. Missing
    # hours must never be silently interpreted as free/clean energy.
    if intervals[0].start > job.earliest_start or intervals[-1].end < job.deadline:
        raise ValueError("Forecast must cover the whole earliest-start-to-deadline window.")
    if carbon and (carbon.intervals[0].start > job.earliest_start or carbon.intervals[-1].end < job.deadline):
        raise ValueError("Carbon forecast must cover the entire scheduling window.")

    total_kwh = job.estimated_power_w / 1000 * job.duration_minutes / 60

    # Align solar and carbon boundaries once, then use prefix integrals. This
    # handles different provider resolutions without scanning the entire carbon
    # forecast for each candidate window.
    boundaries = {job.earliest_start, job.deadline}
    for interval in intervals:
        boundaries.update((interval.start, interval.end))
    if carbon:
        for point in carbon.intervals:
            boundaries.update((point.start, point.end))
    boundaries = sorted(t for t in boundaries if job.earliest_start <= t <= job.deadline)
    segments, solar_prefix, carbon_prefix = [], [0.0], [0.0]
    solar_index = carbon_index = 0
    for left, right in zip(boundaries, boundaries[1:]):
        while intervals[solar_index].end <= left:
            solar_index += 1
        surplus = max(0.0, intervals[solar_index].estimated_solar_kw - site.base_load_kw)
        solar_kw = 0.0 if request.objective == "carbon" else min(job.estimated_power_w / 1000, surplus)
        intensity = 0.0
        if carbon:
            while carbon.intervals[carbon_index].end <= left:
                carbon_index += 1
            intensity = carbon.intervals[carbon_index].intensity_gco2e_per_kwh
        carbon_g_per_hour = (job.estimated_power_w / 1000 - solar_kw) * intensity
        hours = (right - left).total_seconds() / 3600
        segments.append((left, right, solar_kw, carbon_g_per_hour))
        solar_prefix.append(solar_prefix[-1] + solar_kw * hours)
        carbon_prefix.append(carbon_prefix[-1] + carbon_g_per_hour * hours)
    segment_starts = [segment[0] for segment in segments]

    def accumulated(when, prefix, rate_index):
        index = bisect_right(segment_starts, when) - 1
        return prefix[index] + segments[index][rate_index] * (when - segments[index][0]).total_seconds() / 3600

    def evaluate(start):
        finish = start + duration
        solar_kwh = min(total_kwh, max(0.0, accumulated(finish, solar_prefix, 2) - accumulated(start, solar_prefix, 2)))
        emissions_g = max(0.0, accumulated(finish, carbon_prefix, 3) - accumulated(start, carbon_prefix, 3)) if carbon else None
        grid_kwh = max(0.0, total_kwh - solar_kwh)
        return {
            "start": start.isoformat(),
            "finish": finish.isoformat(),
            "estimated_total_energy_kwh": total_kwh,
            "estimated_solar_energy_kwh": solar_kwh,
            "estimated_grid_energy_kwh": grid_kwh,
            "estimated_solar_fraction": min(1.0, solar_kwh / total_kwh),
            "estimated_grid_emissions_gco2e": emissions_g,
        }

    starts = []
    start = job.earliest_start
    while start <= latest_start:
        starts.append(start)
        start += timedelta(minutes=request.step_minutes)
    # Include an off-grid latest feasible start so a tight deadline isn't lost.
    if starts[-1] != latest_start:
        starts.append(latest_start)

    baseline = evaluate(job.earliest_start)
    selected = baseline
    score_key = "estimated_grid_energy_kwh" if request.objective == "solar" else "estimated_grid_emissions_gco2e"
    for start in starts[1:]:
        candidate = evaluate(start)
        # Stable earliest-start tie break; do not delay a job for no benefit.
        if candidate[score_key] < selected[score_key] - 1e-12:
            selected = candidate

    avoided = baseline["estimated_grid_energy_kwh"] - selected["estimated_grid_energy_kwh"]
    baseline_grid = baseline["estimated_grid_energy_kwh"]
    return {
        "status": "preview_only",
        "objective": request.objective,
        "carbon_source": carbon.source if carbon else None,
        "carbon_is_simulated": carbon.is_simulated if carbon else None,
        "carbon_note": carbon.note if carbon else None,
        "grid_zone": site.grid_zone,
        "job_name": job.name,
        "forecast_source": forecast.source,
        "forecast_generated_at": forecast.generated_at.isoformat(),
        "forecast_note": forecast.note,
        "site_name": site.name,
        "site_base_load_kw": site.base_load_kw,
        "job_estimated_power_w": job.estimated_power_w,
        "site_configuration_is_simulated": site.configuration_is_simulated,
        "baseline": baseline,
        "recommended": selected,
        "estimated_grid_energy_reduction_kwh": avoided,
        "estimated_grid_energy_reduction_percent": 100 * avoided / baseline_grid if baseline_grid > 1e-12 else 0.0,
        "estimated_grid_emissions_reduction_gco2e": baseline["estimated_grid_emissions_gco2e"] - selected["estimated_grid_emissions_gco2e"] if carbon else None,
        "candidate_count": len(starts),
        "step_minutes": request.step_minutes,
        "explanation": (
            f"Selected the lowest {score_key} among candidate starts, "
            "covering the entire job duration and respecting its deadline. Equal results "
            "prefer an earlier start. The baseline starts at earliest_start."
        ),
        "assumptions": [
            "Power and runtime are user estimates; total job energy is unchanged by shifting.",
            "Solar serves site base load first; remaining solar can serve this one job.",
            "No battery, export credits, other scheduled jobs, or varying base load are modeled.",
            "These are modeled job-level grid-import changes, not measured carbon savings.",
            "Carbon objective assumes grid-only power; hybrid accounts for local solar. Carbon factors are attribution estimates, not proof of marginal emissions avoided.",
            "A multi-job worker must reserve both compute capacity and available solar.",
            "This is a preview; it neither persists a job nor starts a container.",
        ],
    }
