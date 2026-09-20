"""One local command for dashboard + worker, using the same queue database."""

import os
import socket
import subprocess
import sys
from pathlib import Path


def launch(port=8088, db=None, no_worker=False):
    import uvicorn
    if db:
        os.environ["CARBONSHIFT_DB"] = str(Path(db).expanduser().resolve())
    # Fail before launching a worker when the dashboard port is already taken.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
    worker = None
    try:
        if not no_worker:
            worker = subprocess.Popen([sys.executable, "-m", "carbonshift.worker"],
                                      cwd=Path(__file__).resolve().parent.parent)
        print(f"\nCarbonShift: http://127.0.0.1:{port}\nCtrl+C closes the dashboard and its worker.\n", flush=True)
        uvicorn.run("carbonshift.api:app", host="127.0.0.1", port=port, access_log=False)
    finally:
        if worker is not None and worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=40)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()

