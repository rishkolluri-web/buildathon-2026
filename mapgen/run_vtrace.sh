#!/usr/bin/env bash
# Per-test value trace (mapgen/vtrace.py) over sqlglot's OWN test suite at the pinned commit.
# Usage: mapgen/run_vtrace.sh <sqlglot checkout @ eb11aec> <python with requirements.lock> <out dir> <tests.json>
# Fails (non-zero) if pytest does not pass cleanly or a worker's dump is missing: an incomplete trace
# must never be shipped (a test absent from a read set is not evidence that it ran).
set -euo pipefail
SRC=$(cd "$1" && pwd); PY=$2; OUT=$(mkdir -p "$3" && cd "$3" && pwd); TESTS=$(cd "$(dirname "$4")" && pwd)/$(basename "$4")
HERE=$(cd "$(dirname "$0")" && pwd)
W=${MAPGEN_WORKERS:-6}
rm -rf "$OUT" && mkdir -p "$OUT/raw"
cd "$SRC"
set +e
PATH="$(dirname "$PY"):$PATH" PYTHONHASHSEED="${VTRACE_SEED:-random}" SKIP_INTEGRATION=0 VTRACE_SRC="$SRC" \
  VTRACE_OUT="$OUT/raw" PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SRC:$HERE:$HERE/vtsite" \
  "$PY" -m pytest tests -p vtrace_plugin -p no:cacheprovider -n "$W" --timeout 900 -q -rfE > "$OUT/pytest.log" 2>&1
rc=$?
set -e
tail -1 "$OUT/pytest.log"
if [ "$rc" -ne 0 ]; then echo "vtrace: pytest exit $rc -> trace rejected" >&2; exit 1; fi
n=$(ls "$OUT/raw" | wc -l)
if [ "$n" -lt $((W + 1)) ]; then echo "vtrace: only $n dumps for $W workers + controller -> trace rejected" >&2; exit 1; fi
"$PY" "$HERE/vtrace.py" merge "$OUT/raw" "$TESTS" "$OUT/vmap.json.gz"
"$PY" -c "import gzip,json,sys; v=json.load(gzip.open('$OUT/vmap.json.gz','rt')); u=v['untraced']; print('untraced tests:', len(u)); sys.exit(1 if len(u) > 0 else 0)" \
  || { echo "vtrace: some numbered tests did not complete under tracing (see untraced)" >&2; exit 1; }
