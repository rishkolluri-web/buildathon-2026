#!/usr/bin/env bash
# Regenerate the selector map from sqlglot's OWN test suite at the pinned commit.
# Usage: mapgen/run_coverage.sh <sqlglot checkout @ eb11aec> <python with requirements.lock + pytest-cov> <out dir> <tests.json>
set -euo pipefail
SRC=$(cd "$1" && pwd); PY=$2; OUT=$(mkdir -p "$3" && cd "$3" && pwd); TESTS=$(cd "$(dirname "$4")" && pwd)/$(basename "$4")
HERE=$(cd "$(dirname "$0")" && pwd)
rm -rf "$OUT" && mkdir -p "$OUT"
cd "$SRC"
PATH="$(dirname "$PY"):$PATH" SKIP_INTEGRATION=0 SQLGLOT_ROOT="$SRC" MAPGEN_OUT="$OUT" \
  PYTHONPATH="$SRC:$HERE" COVERAGE_FILE="$OUT/.coverage" \
  "$PY" -m pytest tests -p trace_plugin -p track_plugin -p no:cacheprovider -n "${MAPGEN_WORKERS:-6}" \
  --timeout 600 --cov=sqlglot --cov=tests --cov-context=test --cov-report= -q -rfE \
  > "$OUT/pytest.log" 2>&1 || true
tail -3 "$OUT/pytest.log"
"$PY" "$HERE/import_lines.py" "$SRC" "$OUT/import_lines.json"
"$PY" "$HERE/build_map.py" --coverage "$OUT/.coverage" --opens "$OUT" --tracks "$OUT" --imports "$OUT/import_lines.json" \
  --tests "$TESTS" --root "$SRC" --out "$OUT/map.json.gz"
