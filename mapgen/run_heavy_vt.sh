#!/usr/bin/env bash
# Solo value + identity traces of every test > 3 s (mapgen/heavy_vt.py) -> <out>/heavy_vt.json.
# Usage: mapgen/run_heavy_vt.sh <sqlglot checkout @ eb11aec> <python with requirements.lock> <out dir> <tests.json> [seeds...]
# A run that does not pass cleanly writes no `ok` marker, and heavy_vt.py then drops that test (never narrowed).
set -uo pipefail
SRC=$(cd "$1" && pwd); PY=$2; OUT=$(mkdir -p "$3" && cd "$3" && pwd); TESTS=$(cd "$(dirname "$4")" && pwd)/$(basename "$4")
shift 4; SEEDS=${*:-0 1}
HERE=$(cd "$(dirname "$0")" && pwd)
IDS=$("$PY" -c "import json,sys; print(' '.join(t['id'] for t in json.load(open('$TESTS'))['tests'] if t['seconds'] > 3))")
cd "$SRC"
run() {  # <nodeid> <seed>
  local d="$OUT/raw/$(echo "$1" | tr '/:.' '___')/s$2"
  rm -rf "$d"; mkdir -p "$d/vraw" "$d/eraw"
  PATH="$(dirname "$PY"):$PATH" PYTHONHASHSEED=$2 SKIP_INTEGRATION=0 VTRACE_SRC="$SRC" VTRACE_OUT="$d/vraw" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SRC:$HERE:$HERE/vtsite" \
    "$PY" -m pytest "$1" -p vtrace_plugin -p no:cacheprovider -p no:xdist --timeout 5400 -q -rfE > "$d/vtrace.log" 2>&1 || return
  PATH="$(dirname "$PY"):$PATH" PYTHONHASHSEED=$2 SKIP_INTEGRATION=0 EQTRACE_SRC="$SRC" EQTRACE_OUT="$d/eraw" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SRC:$HERE/eqsite" \
    "$PY" -m pytest "$1" -p no:cacheprovider -p no:xdist --timeout 5400 -q -rfE > "$d/eq.log" 2>&1 || return
  grep -Eq "^1 passed" "$d/vtrace.log" && grep -Eq "^1 passed" "$d/eq.log" && ! grep -Eq "failed|error" "$d/vtrace.log" "$d/eq.log" && echo "$1" > "$d/ok"
}
for s in $SEEDS; do
  for id in $IDS; do run "$id" "$s" & done
  wait
done
"$PY" "$HERE/heavy_vt.py" "$OUT/raw" "$TESTS" "$OUT/heavy_vt.json"
