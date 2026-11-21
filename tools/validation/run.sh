#!/bin/zsh
# Offline validation only (never runs in the submitted image).
# Apply each mutant to a scratch sqlglot worktree, run the whole suite, record failing test ids.
# Usage: VENV=/path/to/py313-venv run.sh <sqlglot-worktree> <mutant-dir>
WT=$1; DIR=$2
. ${VENV:?set VENV to a Python 3.13 venv with requirements.lock installed}/bin/activate
cd $WT
for p in $DIR/*.patch; do
  n=${p:t:r}; [[ -f $DIR/$n.fail ]] && continue
  git checkout -q -- . && git apply $p || { echo APPLYFAIL > $DIR/$n.fail; continue; }
  SKIP_INTEGRATION=0 PYTHONPATH=. python -m pytest tests -q -p no:cacheprovider -n 8 --timeout=120 -rfE 2>&1 > $DIR/$n.log
  grep -E '^(FAILED|ERROR|SUBFAILED)' $DIR/$n.log | sed -E 's/^[A-Z]+(\[[^]]*\])? //; s/ - .*//; s/\[.*$//' | sort -u > $DIR/$n.fail
  echo "$n $(wc -l < $DIR/$n.fail)"
done
git checkout -q -- .
