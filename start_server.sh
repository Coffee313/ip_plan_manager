#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
exec gunicorn \
  --workers "${IP_PLAN_WORKERS:-2}" \
  --threads "${IP_PLAN_THREADS:-4}" \
  --worker-class gthread \
  --bind "${IP_PLAN_BIND:-0.0.0.0:5080}" \
  --timeout 60 \
  app:app
