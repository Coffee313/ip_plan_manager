#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
if [ ! -f ".venv/.ipplan_ready" ]; then
  python -m pip install --disable-pip-version-check -r requirements.txt
  touch .venv/.ipplan_ready
fi

python app.py
