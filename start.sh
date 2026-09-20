#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import fastapi, uvicorn, pydantic, PIL' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt
fi
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  if ! docker image inspect carbonshift-workload:0.4 >/dev/null 2>&1; then
    echo 'Building the local checksum workload image…'
    if ! docker build -t carbonshift-workload:0.4 ./workloads; then
      echo 'Image build failed. The planner will work; execution remains unavailable.'
    fi
  fi
else
  echo 'Docker is unavailable. Starting the planner; the dashboard will show execution readiness.'
fi
exec .venv/bin/python -m carbonshift app "$@"
