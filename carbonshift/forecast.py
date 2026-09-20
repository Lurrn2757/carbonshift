"""Weather adapter. All intervals are UTC; radiation is a preceding-hour mean."""

import json
import math
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import Forecast, Interval


def parse_open_meteo(payload: dict, pv_capacity_kwp: float, performance_ratio: float) -> Forecast:
    if not math.isfinite(pv_capacity_kwp) or not 0 <= pv_capacity_kwp <= 100000:
        raise ValueError("Invalid PV capacity.")
    if not math.isfinite(performance_ratio) or not 0 < performance_ratio <= 1:
        raise ValueError("Performance ratio must be in (0, 1].")
    if not isinstance(payload, dict):
        raise ValueError("Weather response must be an object.")
    if payload.get("utc_offset_seconds") != 0:
        raise ValueError("Expected UTC weather timestamps.")
    units = payload.get("hourly_units")
    if not isinstance(units, dict) or units.get("shortwave_radiation") != "W/m²":
        raise ValueError("Unexpected radiation units.")
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        raise ValueError("Weather response must contain hourly data.")
    timestamps, radiation = hourly["time"], hourly["shortwave_radiation"]
    if not isinstance(timestamps, list) or not isinstance(radiation, list):
        raise ValueError("Weather time and radiation must be arrays.")
    if not timestamps or len(timestamps) != len(radiation):
        raise ValueError("Weather arrays are empty or differ in length.")
    intervals = []
    for timestamp, irradiance in zip(timestamps, radiation):
        if not isinstance(timestamp, str):
            raise ValueError("Weather timestamp must be a string.")
        if isinstance(irradiance, bool) or not isinstance(irradiance, (int, float)):
            raise ValueError("Weather contains a missing or invalid radiation value.")
        if not math.isfinite(irradiance) or irradiance < 0:
            raise ValueError("Weather contains invalid radiation.")
        end = datetime.fromisoformat(timestamp)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        elif end.utcoffset() != timedelta(0):
            raise ValueError("Expected UTC weather timestamps.")
        # Open-Meteo labels an hourly radiation mean with its END time.
        # GHI is only a first-order proxy for panel irradiance here.
        solar_kw = min(pv_capacity_kwp, pv_capacity_kwp * irradiance / 1000 * performance_ratio)
        intervals.append(Interval(start=end - timedelta(hours=1), end=end, estimated_solar_kw=solar_kw))
    return Forecast(
        source="open-meteo",
        generated_at=datetime.now(timezone.utc),
        intervals=intervals,
        note=(
            "Weather retrieved from Open-Meteo (https://open-meteo.com/), CC BY 4.0. "
            "generated_at is retrieval time, not model issuance time. Solar power is a "
            "simplified estimate: min(capacity, capacity × GHI/1000 × performance ratio). "
            "Panel tilt, orientation, shading and temperature losses are not individually modeled. "
            "This is not a grid-carbon forecast or measured PV output."
        ),
    )


def fetch_open_meteo(latitude=19.076, longitude=72.8777, pv_capacity_kwp=0.5, performance_ratio=0.8):
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("Invalid coordinates.")
    query = urlencode({
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "shortwave_radiation",
        "timezone": "UTC",
        "forecast_days": 3,
    })
    request = Request("https://api.open-meteo.com/v1/forecast?" + query, headers={"User-Agent": "CarbonShift/0.1"})
    with urlopen(request, timeout=15) as response:
        payload = json.load(response)
    return parse_open_meteo(payload, pv_capacity_kwp, performance_ratio)
