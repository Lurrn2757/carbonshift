"""Provider seam: future adapters return this same validated CarbonForecast."""

from datetime import datetime, timedelta, timezone
from typing import Protocol
import json
import os
import re
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener
from pydantic import ValidationError

from .models import CarbonForecast, CarbonInterval, LiveCarbonReading

LIVE_ENDPOINT = "https://api.electricitymaps.com/v4/carbon-intensity/latest"


class CarbonProviderError(RuntimeError):
    """Public-safe error: never includes auth headers or provider response bodies."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CarbonProviderError("Electricity Maps redirected the request. Check the configured API version; credentials were not forwarded.")


def fetch_electricitymaps_live(zone="IN-WE", api_key=None):
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,63}", zone):
        raise CarbonProviderError("Invalid zone code. Example: IN-WE.")
    from .credentials import read_api_key
    try:
        key = (api_key if api_key is not None else read_api_key()).strip()
    except (OSError, ValueError):
        raise CarbonProviderError("Cannot read the saved API key. Run bash configure-api.sh or check its file permissions.") from None
    if not key:
        raise CarbonProviderError("Set ELECTRICITYMAPS_API_KEY in the server terminal or run bash configure-api.sh, then restart CarbonShift.")
    if len(key) > 4096 or not key.isascii() or any(c.isspace() for c in key):
        raise CarbonProviderError("API key format is invalid. Check the local environment value.")
    query = urlencode({"zone": zone, "temporalGranularity": "hourly", "emissionFactorType": "lifecycle", "flowTraced": "true", "disableCallerLookup": "true"})
    request = Request(LIVE_ENDPOINT + "?" + query, headers={"auth-token": key, "Accept": "application/json", "User-Agent": "CarbonShift/0.4.0"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            payload = response.read(65537)
        if len(payload) > 65536:
            raise CarbonProviderError("Electricity Maps response exceeded the expected size.")
    except HTTPError as exc:
        messages = {401: "API key rejected. Check your Electricity Maps key.",
                    403: "Your Electricity Maps account does not permit this request. Verify zone and signal entitlement.",
                    404: "No latest reading is available for this zone or endpoint.",
                    429: "Electricity Maps rate limit reached. Wait before fetching again."}
        raise CarbonProviderError(messages.get(exc.code, f"Electricity Maps returned HTTP {exc.code}. No observation was saved.")) from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise CarbonProviderError("Electricity Maps is unreachable or timed out. No observation was saved.") from None
    return parse_live_reading(payload, zone, datetime.now(timezone.utc))


def parse_live_reading(payload, zone, retrieved_at):
    try:
        data = json.loads(payload)
        if not isinstance(data, dict) or data.get("zone") != zone:
            raise ValueError("zone")
        # Require the semantics we requested; do not silently mix signal types.
        if data.get("emissionFactorType") != "lifecycle" or data.get("flowTraced") is not True or data.get("temporalGranularity") != "hourly":
            raise ValueError("semantics")
        reading = LiveCarbonReading(
            zone=data["zone"], carbon_intensity_gco2e_per_kwh=data["carbonIntensity"],
            data_at=data["datetime"], retrieved_at=retrieved_at,
            provider_updated_at=data.get("updatedAt"), is_estimated=data["isEstimated"],
            estimation_method=data.get("estimationMethod"),
        )
        if reading.data_at > retrieved_at + timedelta(minutes=5):
            raise ValueError("future")
        return reading
    except (ValueError, TypeError, KeyError, ValidationError):
        raise CarbonProviderError("Electricity Maps returned invalid data: check zone, numeric intensity, timestamps and signal metadata. No observation was saved.") from None


class CarbonProvider(Protocol):
    def forecast(self, start: datetime, end: datetime, grid_zone: str) -> CarbonForecast: ...


class SimulatedCarbonProvider:
    def forecast(self, start, end, grid_zone):
        if start.tzinfo is None or end.tzinfo is None or not start < end:
            raise ValueError("Provide timezone-aware start/end in increasing order.")
        cursor = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        points = []
        # Arbitrary repeating UTC-hour profile. Not an Indian grid model.
        profile = [650, 660, 670, 640, 550, 450, 320, 300, 340, 420, 510, 590,
                   650, 720, 760, 740, 680, 630, 600, 570, 560, 590, 610, 630]
        while cursor < end:
            points.append(CarbonInterval(start=cursor, end=cursor + timedelta(hours=1), intensity_gco2e_per_kwh=profile[cursor.hour]))
            cursor += timedelta(hours=1)
        return CarbonForecast(
            source="simulated-carbon-v1", is_simulated=True, grid_zone=grid_zone,
            generated_at=datetime.now(timezone.utc), intervals=points,
            note="Invented UTC-hour carbon profile for development; not observed or forecast grid data for this zone.",
        )
