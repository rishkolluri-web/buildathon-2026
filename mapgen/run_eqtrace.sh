#!/usr/bin/env bash
# Identity/equality trace (mapgen/eq_trace.py) over sqlglot's OWN test suite at the pinned commit.
# Usage: mapgen/run_eqtrace.sh <sqlglot checkout @ eb11aec> <python with requirements.lock> <out dir> [PYTHONHASHSEED]
set -euo pipefail
SRC=$(cd "$1" && pwd); PY=$2; OUT=$(mkdir -p "$3" && cd "$3" && pwd); SEED=${4:-random}
HERE=$(cd "$(dirname "$0")" && pwd)
rm -rf "$OUT" && mkdir -p "$OUT/raw"
cd "$SRC"
PATH="$(dirname "$PY"):$PATH" SKIP_INTEGRATION=0 EQTRACE_SRC="$SRC" EQTRACE_OUT="$OUT/raw" PYTHONDONTWRITEBYTECODE=1 \
  PYTHONHASHSEED="$SEED" PYTHONPATH="$SRC:$HERE/eqsite" \
  "$PY" -m pytest tests -p no:cacheprovider -n "${MAPGEN_WORKERS:-6}" --timeout 900 -q -rfE > "$OUT/pytest.log" 2>&1 || true
tail -1 "$OUT/pytest.log"
"$PY" "$HERE/eq_trace.py" merge "$OUT/raw" "$OUT/eqsites.json"
