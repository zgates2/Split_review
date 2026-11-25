#!/usr/bin/env bash
# Start the review service.  Usage: ./run.sh [--host 0.0.0.0] [--port 18770]
set -euo pipefail
cd "$(dirname "$0")"
PY="${SPLIT_PYTHON:-/data/g2/software/miniconda3/envs/lerobot/bin/python}"
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
export SPLIT_CACHE_DIR="${SPLIT_CACHE_DIR:-$PWD/state/cache}"
mkdir -p state/cache state/preview out
exec "$PY" -m split_review --db "${SPLIT_DB:-state/review.db}" serve "$@"
