from urllib.error import URLError

from fastapi import Depends, FastAPI, HTTPException, Query, Request

from . import __version__
from .credentials import read_api_key
from .demo import demo_request, queue_demo_request
from .carbon import SimulatedCarbonProvider
from .forecast import fetch_open_meteo
from .models import Forecast, OptimizeRequest, SubmitRequest
from .optimizer import optimize
from .store import Store, Conflict
from typing import Literal
from pathlib import Path
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from .planner import PlanSettings, RunSettings, build_plan, build_execution_plan
from urllib.parse import urlsplit
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
import os
from .carbon import fetch_electricitymaps_live, CarbonProviderError
from .evidence import append_observation, read_observations, reading_view

app = FastAPI(
    title="CarbonShift",
    version=__version__,
    description="Solar/carbon previews and a persistent single-slot queue. Run the separate local Docker worker for execution.",
)
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])


@app.middleware("http")
async def local_browser_requests(request, call_next):
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
        parsed = urlsplit(origin)
        if parsed.scheme not in ("http", "https") or parsed.netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Open CarbonShift from its local URL to submit changes."}, status_code=403)
    return await call_next(request)


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "index.html")


@app.post("/api/planner")
def planner(settings: PlanSettings):
    try:
        request = build_plan(settings)
        return {"request": request.model_dump(mode="json"), "result": optimize(request)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.post("/api/execution/preview")
def execution_preview(settings: RunSettings):
    request = build_execution_plan(settings)
    return {"submission": request.model_dump(mode="json"), "result": optimize(request.optimization)}


@app.get("/api/carbon/observations")
def carbon_observations(limit: int = Query(default=20, ge=1, le=1000)):
    try:
        return {"configured": bool(read_api_key()), **read_observations(limit)}
    except (OSError, ValueError):
        raise HTTPException(status_code=503, detail="Cannot read the observation journal or saved key. Check configured paths and file permissions.") from None


@app.post("/api/carbon/observe")
def observe_carbon(zone: str = Query(default="IN-WE", pattern=r"^[A-Z0-9][A-Z0-9_-]{0,63}$")):
    try:
        reading = fetch_electricitymaps_live(zone)
    except CarbonProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    try:
        path = append_observation(reading)
    except (OSError, ValueError):
        raise HTTPException(status_code=503, detail="Reading retrieved but NOT saved. Check the separate journal path, permissions or incomplete final line; the queue was not changed.") from None
    return {"reading": reading_view(reading), "saved_to": str(path), "used_for_scheduling": False}


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__, "capability": "preview_and_queue", "worker_is_separate": True}


@app.get("/api/demo/request", response_model=OptimizeRequest)
def example_request(objective: Literal["solar", "carbon", "hybrid"] = "solar"):
    request = demo_request()
    if objective != "solar":
        data = request.model_dump()
        data.update(objective=objective, carbon_forecast=SimulatedCarbonProvider().forecast(request.job.earliest_start, request.job.deadline, request.site.grid_zone))
        request = OptimizeRequest.model_validate(data)
    return request


@app.get("/api/demo")
def demo(objective: Literal["solar", "carbon", "hybrid"] = "solar"):
    return optimize(example_request(objective))


@app.post("/api/optimize")
def preview(request: OptimizeRequest):
    try:
        return optimize(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/forecast", response_model=Forecast)
def forecast(
    latitude: float = Query(default=19.076, ge=-90, le=90),
    longitude: float = Query(default=72.8777, ge=-180, le=180),
    pv_capacity_kwp: float = Query(default=0.5, ge=0, le=100000),
    performance_ratio: float = Query(default=0.8, gt=0, le=1),
):
    try:
        return fetch_open_meteo(latitude, longitude, pv_capacity_kwp, performance_ratio)
    except (URLError, TimeoutError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Weather provider unavailable. Retry later; use /api/demo for an explicitly simulated example.") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="Weather response failed validation; no schedule was produced.") from exc


def get_store():
    return Store()


@app.get("/api/system")
def system_status(store: Store = Depends(get_store)):
    return {"version": __version__, **store.status()}


@app.get("/api/jobs/demo/request", response_model=SubmitRequest)
def execution_example(delay_seconds: int = Query(default=30, ge=5, le=86400), run_seconds: int = Query(default=5, ge=1, le=3600)):
    return queue_demo_request(delay_seconds, run_seconds)


@app.post("/api/jobs")
def submit_job(request: SubmitRequest, store: Store = Depends(get_store)):
    try:
        return store.submit(request)
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail="Invalid request or unavailable input files. Upload and preview again.") from exc


@app.get("/api/jobs")
def jobs(store: Store = Depends(get_store)):
    return store.list()


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str, store: Store = Depends(get_store)):
    try:
        from .assets import progress_from_logs
        job = store.get(job_id)
        if job['snapshot']['execution']['workload'] == 'image-batch':
            job['progress'] = progress_from_logs(job['logs'])
        return job
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, store: Store = Depends(get_store)):
    try:
        return store.cancel(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


from . import assets
from .planner import BatchPreviewSettings, build_batch_plan
import json
import time
import uuid
from starlette.concurrency import run_in_threadpool


@app.post('/api/batches')
async def upload_batch(request: Request):
    # Bound the body before JSON parsing; no unbounded multipart buffering.
    raw=bytearray()
    async for chunk in request.stream():
        if len(raw)+len(chunk)>assets.MAX_BODY:
            raise HTTPException(status_code=413, detail='Batch upload is too large (20 MiB decoded total).')
        raw.extend(chunk)
    try:
        payload=assets.UploadBatch.model_validate_json(raw)
        return await run_in_threadpool(assets.save_upload,payload)
    except ValueError:
        raise HTTPException(status_code=422, detail='Invalid batch: use 1–20 single-frame JPEG/PNG images, at most 5 MiB and 8 megapixels each, 20 MiB total.') from None
    except OSError:
        raise HTTPException(status_code=503, detail='Could not save the batch. Check local artifact storage permissions and free space.') from None


@app.post('/api/batches/preview')
def batch_preview(settings: BatchPreviewSettings):
    try:
        assets.validate_bundle(settings.batch.model_dump())
        submission=build_batch_plan(settings)
        if submission.optimization.job.earliest_start.timestamp() < time.time()+5:
            raise ValueError('Earliest start is too close or has passed. Choose a future time and preview again.')
        token=uuid.uuid4().hex
        plan=build_plan(settings.plan)
        result={'preview_id':token,'submission':submission.model_dump(mode='json'),
                'result':optimize(submission.optimization),'comparison':optimize(plan),
                'request':plan.model_dump(mode='json'),'timing':settings.timing}
        directory=assets.identified('previews',token)
        directory.mkdir(parents=True)
        p=directory/'plan.json'
        with p.open('x') as stream:
            json.dump(result,stream);stream.flush();os.fsync(stream.fileno())
        p.chmod(0o400)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except OSError:
        raise HTTPException(status_code=503, detail='Batch inputs or preview storage unavailable. Upload again or check storage.') from None


@app.post('/api/batches/queue/{preview_id}')
def queue_batch(preview_id: str, store: Store = Depends(get_store)):
    try:
        path=assets.identified('previews',preview_id)/'plan.json'
        data=json.loads(path.read_text())
        submission=SubmitRequest.model_validate(data['submission'])
        if optimize(submission.optimization) != data['result']:
            raise Conflict('The saved recommendation no longer matches this planner version. Generate a fresh preview.')
        return store.submit(submission)
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail='Preview or input batch is invalid. Generate a fresh preview.') from None
    except OSError:
        raise HTTPException(status_code=404, detail='Saved preview or inputs not found. Upload and preview again.') from None


@app.get('/api/jobs/{job_id}/results')
def download_results(job_id: str, store: Store = Depends(get_store)):
    try:
        job=store.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail='Job not found') from None
    artifact=(job.get('evidence') or {}).get('artifact')
    if job['state']!='SUCCEEDED' or not artifact:
        raise HTTPException(status_code=409, detail='A verified completed image batch is required for download.')
    try:
        path=assets.result_path(job_id)
        if assets.digest(path.read_bytes())!=artifact['sha256']:
            raise ValueError('Results changed since completion.')
    except (ValueError,OSError):
        raise HTTPException(status_code=409, detail='Result archive is missing or changed since completion.') from None
    return FileResponse(path,media_type='application/zip',filename=f'carbonshift-{job_id[:12]}-images.zip')
