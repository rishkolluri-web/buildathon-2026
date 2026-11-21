#!/usr/bin/env bash
# Per-test value trace (mapgen/vtrace.py) over sqlglot's OWN test suite at the pinned commit.
# Usage: mapgen/run_vtrace.sh <sqlglot checkout @ eb11aec> <python with requirements.lock> <out dir> <tests.json>
set -euo pipefail
SRC=$(cd "$1" && pwd); PY=$2; OUT=$(mkdir -p "$3" && cd "$3" && pwd); TESTS=$(cd "$(dirname "$4")" && pwd)/$(basename "$4")
HERE=$(cd "$(dirname "$0")" && pwd)
rm -rf "$OUT" && mkdir -p "$OUT/raw"
cd "$SRC"
PATH="$(dirname "$PY"):$PATH" PYTHONHASHSEED="${VTRACE_SEED:-random}" SKIP_INTEGRATION=0 VTRACE_SRC="$SRC" VTRACE_OUT="$OUT/raw" PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$SRC:$HERE:$HERE/vtsite" \
  "$PY" -m pytest tests -p vtrace_plugin -p no:cacheprovider -n "${MAPGEN_WORKERS:-6}" --timeout 900 -q -rfE \
  > "$OUT/pytest.log" 2>&1 || true
tail -3 "$OUT/pytest.log"
"$PY" "$HERE/vtrace.py" merge "$OUT/raw" "$TESTS" "$OUT/vmap.json.gz"
