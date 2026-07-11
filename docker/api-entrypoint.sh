#!/bin/sh
# Generates the demo warehouse on first boot (mounted volume persists it
# across restarts, so this only runs once per deployment) and then starts
# the API. CACHEOPT_DATASET_ROWS lets you size the demo dataset without
# rebuilding the image (default is small -- big enough to make the tier
# routing/latency story real, small enough to boot in seconds).
set -e

DB_PATH="${CACHEOPT_DUCKDB_PATH:-/app/data/warehouse.duckdb}"
ROWS="${CACHEOPT_DATASET_ROWS:-500000}"

if [ ! -f "$DB_PATH" ]; then
  echo "no dataset at $DB_PATH -- generating $ROWS rows"
  python scripts/generate_dataset.py --rows "$ROWS" --out "$DB_PATH"
fi

exec uvicorn cacheopt.api.app:app --app-dir src --host 0.0.0.0 --port 8000
