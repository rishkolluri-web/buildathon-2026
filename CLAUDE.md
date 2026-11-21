# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A submission for the Builders Buildathon **RWX "Blast Radius"** track. For each unseen patch to
sqlglot (pinned at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`), `selector.py` prints one JSON line
`{"commit": id, "workers": [[test numbers], ...]}` choosing which sqlglot tests to run on ≤ 8 workers.
Score (lower is better, rules in the kit's `SCORING.md`): per commit, the busiest worker's recorded
test seconds + 2 s per worker; **+300 s if any failing test is left out** of that commit; plus the
container's CPU time; middle of 3 runs. A miss costs more than any plausible selection, so safety
beats precision.

Hard rules (official): no network at runtime; runs as `nobody` on a read-only disk (`/tmp` 64 MB);
2 CPUs / 4 GB / 3 min (4.5 in the final); stdout must contain only answer lines (logs → stderr);
repo ≤ 20 MB, no LFS/submodules; the build may download only from public registries, GitHub
releases at fixed tags, and sqlglot's own repo at the pinned commit. **Never put sample answers,
or any patch/edit-kind → failing-test table, into the selector or map** — maps must be derived from
the pinned sqlglot code and test suite only.

## Layout outside this repo (sibling directories, not committed)

- `../blast-radius-sample/` — official kit: `Makefile`, `score.py`, `SCORING.md`, `sample/` (30 patches, `tests.json`, `answers.json`).
- `../sqlglot/` — sqlglot checkout at the pinned commit (map generation + local `BR_SRC`).
- `../.venv313/` — Python 3.13 with the kit's `requirements.lock` + `pytest-cov` (matches the labeling env).
- `../stress/` — mutation corpus: `commits/*.patch`, `meta.json`, `truth/<id>.json` (ground-truth failing nodeids; `base.json` = failures of the unmutated copy, subtracted).

## Commands

```sh
# offline sample score via the official score.py (no Docker)
python3 tests/evaluate.py --kit ../blast-radius-sample --src ../sqlglot --python ../.venv313/bin/python

# unit tests (protocol checker + selector edge cases; edge cases skip without BR_SRC/BR_KIT)
BR_SRC=../sqlglot BR_KIT=../blast-radius-sample ../.venv313/bin/python -m unittest discover -s tests -v
# single test
BR_SRC=../sqlglot BR_KIT=../blast-radius-sample ../.venv313/bin/python -m unittest test_selector_edgecases.EdgeCases.test_syntax_error_selects_everything   # run from tests/ or with -s tests

# acceptance gate: working tree vs a git revision (default HEAD); PASS/FAIL + numbers
python3 scripts/gate.py [--base a6ee059] [--allow-cost-increase]   # latter for correctness fixes

# per-rule ablation / patch-level comparison against a reference selector
python3 scripts/ablate.py ... --config NAME=SELECTOR,MAP[,BR_OFF]
python3 scripts/compare_rules.py --ref NAME=SEL,MAP --ref-prefill ... --config ...

# mutation ground truth (slow: full sqlglot suite per mutant; cached per mutant)
python3 scripts/stress_mutate.py generate --src ../sqlglot --out ../stress
python3 scripts/stress_mutate.py truth --src ../sqlglot --out ../stress --python ../.venv313/bin/python \
    --jobs 2 --workers 4 --pool-cpus 2 --timeout 900 [--first m042,m048]
python3 scripts/stress_mutate.py eval --src ../sqlglot --out ../stress --tests ../blast-radius-sample/sample/tests.json --python ../.venv313/bin/python

# rebuild the committed map (≈6 min test run + build; then copy map.json.gz into the repo root)
MAPGEN_WORKERS=7 mapgen/run_coverage.sh ../sqlglot ../.venv313/bin/python ../mapoutX ../blast-radius-sample/sample/tests.json

# official Docker check, exactly as the scorer runs it (amd64; Docker CLI lives in ~/.docker/bin)
export PATH=$HOME/.docker/bin:$PATH
docker build --no-cache --platform linux/amd64 -t blast-radius-entry <dir with `git archive <sha>` output>
cd ../blast-radius-sample && make -o build sample IMAGE=blast-radius-entry   # -o build: don't rebuild the kit's own Dockerfile
```

Selector env vars: `BR_DATA` (default `/data`), `BR_SRC` (pinned sources, `/opt/sqlglot` in the image),
`BR_MAP` (`map.json.gz`), `BR_OFF` (comma list of rules to disable, for ablation only — never set in the image).

## Architecture

**Offline map (`mapgen/`, output `map.json.gz`)** — built only from sqlglot's own suite at the pinned commit:
- `trace_plugin.py` (pytest plugin): per-test line coverage contexts via pytest-cov plus an audit hook
  recording which non-import files each test opens (fixtures, `.py` read as data). Key fidelity fixes:
  spawned `ProcessPoolExecutor` work is run inline **with a pickle round-trip** (serde is exercised by
  pickling); `generator._DISPATCH_CACHE` is cleared per test; setup/teardown phases (`setUpClass`,
  module fixtures) and collection-time work are recorded separately and credited to **every test in
  that file** by `build_map.py`.
- `track_plugin.py`: which sqlglot classes each test instantiates → `users[module:Class]` (rolled up to bases).
- `import_lines.py`: lines executed merely by importing all of sqlglot (`imp`), optionally with the
  module-level statement that triggered each function-body line.
- `refs_index.py`: static read index per identifier — runtime reads (`rtn` plain names / `rta`
  attribute+string reads, mapped to tests via coverage) and import-time reads (`impn`/`impa` with
  context: class body, def header, module statement, import-time body), plus importers/star-importers.
- Maps store deduplicated test sets as hex bitsets indexed by `tests` (test ids); the selector maps ids
  to `tests.json` numbers at runtime.

**Runtime (`selector.py`)** — per patch: parse the diff, apply hunks to `BR_SRC`, parse old/new with
`ast`, and let `Differ` find the smallest differing statements (comment/whitespace/docstring-only
edits produce identical ASTs → nothing). Regions resolve to tests:
`lines` (function bodies → coverage; import-time body lines → all tests), `lambda` (lambda bodies in
class dicts → coverage), `class` (class-level data → class users ∪ dialect family
`{dialects,parsers,generators,tokenizers,typing}/<name>.py`), `module` (module-level statements →
`module_change()` via the reference index; unresolvable → all). Data files → tests that opened them.
Syntax errors, deleted/renamed modules, pytest config, analysis exceptions → **every test** (never crash).
Then `pack()` tries k = 1..8 workers longest-first and keeps the cheapest; `fill_idle()` adds related
tests to idle worker capacity without raising the busiest load (free insurance).

`Dockerfile` downloads the pinned sqlglot GitHub archive at build time (`docker/fetch_sqlglot.py`, keeps
only `.py` under `sqlglot/` and `tests/`) and copies `selector.py` + `map.json.gz`.

## Working conventions in this repo

- Every candidate is gated by `scripts/gate.py`: 0 sample misses, 0 misses on mutants with ground truth,
  unit tests pass, cost strictly lower than the base, fill adds 0.0 s, and **every patch whose selection
  shrinks must have ground truth** (zero known misses on unverified patches is not evidence). Change one
  rule at a time; revert on FAIL; log every iteration (kept or reverted) in `EXPERIMENTS.md` with measured
  numbers only.
- Official submissions are preserved: tags `submitted-BB-055F40E6` (dca2229, 500.4 s, 1 miss) and
  `submitted-BB-3F1A5954` (**a6ee059 on `experiment/safety-v2`, 340.0 s, 0 misses — known-good fallback**).
  Never rewrite or force-push submitted commits; new work goes on new `experiment/*` branches.
- Known open safety issue: `module_change()` (from 71f6133 onward) selects 0 tests for a newly added
  module-level class and for a removed `from x import *`; do not submit those revisions until fixed.
- Team split (see the team playbook): Person 1 owns production integration/submission; Person 2
  (verification) owns `tests/`, analysis scripts and `EXPERIMENTS.md`. Don't push or submit without the
  user's explicit approval; the submission is made by hand on the event site with the full 40-char SHA.
- Local gotchas: `~/Desktop` is iCloud-synced — evicted ("dataless") kit files cause `OSError(5)` inside
  Docker (read them on the host first); mutation-truth runs must keep `__pycache__` and cap process pools
  (`PYTHON_CPU_COUNT`) or heavy TPC-DS tests time out and produce false failures.
