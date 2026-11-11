#!/bin/bash
# Wrapper for scheduled runs: one run at a time, venv, logs in the tool's home.
set -euo pipefail
cd "$(dirname "$0")/.."
exec 9>.nvabot.lock
flock -n 9 || { echo "another run is in progress"; exit 0; }
source venv/bin/activate
python -m nvabot --config config.yaml "$@"
