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
python3 scripts/gate.py [--base 4bde6ef] [--allow-cost-increase]   # latter for correctness fixes
# gate mutants = every ../stress*/ corpus; each needs truth/<id>.json or narrowed patches FAIL check 6

# cold-start CPU / RSS / simulated cost of a revision (WORKTREE or a git rev); run on an idle machine
python3 scripts/bench.py --rev WORKTREE --kit ../blast-radius-sample --src ../sqlglot --python ../.venv313/bin/python --runs 5
# which patches fall back to ALL, and why (sample + all ../stress*/ corpora)
python3 scripts/all_census.py --kit ../blast-radius-sample --src ../sqlglot --python ../.venv313/bin/python

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
# other shipped data, also built from the pinned suite only (copy outputs into the repo root):
mapgen/run_eqtrace.sh ../sqlglot ../.venv313/bin/python ../eqoutX            # -> eqsites.json
mapgen/run_vtrace.sh ../sqlglot ../.venv313/bin/python ../vtoutX ../blast-radius-sample/sample/tests.json  # -> vmap.json.gz (rejects incomplete traces)
python3 mapgen/heavy_closure.py --src ../sqlglot --python $PWD/../.venv313/bin/python \
    --tests ../blast-radius-sample/sample/tests.json --out heavy.json          # closure + lines of tests > 3 s
python3 mapgen/iso_imp.py --src ../sqlglot --python $PWD/../.venv313/bin/python \
    --tests ../blast-radius-sample/sample/tests.json --map map.json.gz --out iso_imp.json.gz --jobs 7  # ≈4 min

# official Docker check, exactly as the scorer runs it (amd64; Docker CLI lives in ~/.docker/bin)
export PATH=$HOME/.docker/bin:$PATH
docker build --no-cache --platform linux/amd64 -t blast-radius-entry <dir with `git archive <sha>` output>
cd ../blast-radius-sample && make -o build sample IMAGE=blast-radius-entry   # -o build: don't rebuild the kit's own Dockerfile
```

Selector env vars: `BR_DATA` (default `/data`), `BR_SRC` (pinned sources, `/opt/sqlglot` in the image),
`BR_MAP` (`map.json.gz`), `BR_EQ` / `BR_VMAP` / `BR_HEAVY` / `BR_ISO` / `BR_IDX` (other data files, default next to
`selector.py`; each is optional — a missing file just disables its rules), `BR_OFF` (comma list of rules to
disable, for ablation only — never set in the image; any `BR_OFF` also skips the static index).

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
- `eq_trace.py` (→ `eqsites.json`): `is`/`==` comparison sites where identity and equality agreed on every
  traced execution. `vtrace.py` + `vtrace_plugin.py` (→ `vmap.json.gz`): per-test value trace — class-dict
  entry reads, relational comparison outcomes, comparison-constant operand values, UPPER_CASE attribute reads
  with owner class. `heavy_closure.py` (→ `heavy.json`): sqlglot modules each test > 3 s imports when run
  alone, **including spawned pool children** (tpcds imports dialect parsers only in its workers), plus the
  lines they execute. `iso_imp.py` (→ `iso_imp.json.gz`): every test run alone with coverage in all its
  processes; per import-time line, the tests whose own run executes it (tests failing alone → `unknown`,
  always selected). Most core import-time lines come from SQL templates parsed in class bodies
  (`generators/duckdb.py`, `typing/hive.py`).

**Runtime (`selector.py`)** — per patch: parse the diff, apply hunks to `BR_SRC`, parse old/new with
`ast`, and let `Differ` find the smallest differing statements (comment/whitespace/docstring-only
edits produce identical ASTs → nothing). Regions resolve to tests:
`lines` (function bodies → coverage; import-time body lines → all tests), `lambda` (lambda bodies in
class dicts → coverage), `class` (class-level data → class users ∪ dialect family
`{dialects,parsers,generators,tokenizers,typing}/<name>.py`), `module` (module-level statements →
`module_change()` via the reference index; unresolvable → all). Data files → tests that opened them.
Syntax errors, deleted/renamed modules, pytest config, analysis exceptions → **every test** (never crash).
Value-sensitive rules run before the generic ones and return `None` to fall through: `swap_only_sites`
(eq swaps), `op_change` (operator edits), `const_change` (comparison constants), `dict_value_change`,
`traced_attr_readers` (class settings). An import-time function-body line selects coverage ∪ the tests whose
isolated run executes it (`iso_tests`) instead of ALL. An ALL caused only by in-place edits of sqlglot modules becomes
`trimmed_all()` (a `TrimmedAll` sentinel): every test minus slow tests whose closure excludes the module,
fail-closed on import-time side effects (`import_time_mutations`). Test-file edits resolve through the static
`tests/` import graph (`test_importers`). Pure static analyses are precomputed at image build
(`selector.py --build-index` → `static_index.pkl`, `STATIC` attrs) and recomputed lazily if absent.
Then `pack()` tries k = 1..8 workers longest-first and keeps the cheapest; `fill_idle()` adds related
tests to idle worker capacity without raising the busiest load (free insurance).

`Dockerfile` downloads the pinned sqlglot GitHub archive at build time (`docker/fetch_sqlglot.py`, keeps
only `.py` under `sqlglot/` and `tests/`) and copies `selector.py` plus the data files listed above, then builds `static_index.pkl`. A new data file
must be added to the `Dockerfile` COPY/chmod lines and to the export lists in `scripts/gate.py` and
`scripts/bench.py`.

Cost anatomy that drives priorities: `test_max_nodes` (28.8 s) and `test_execute_tpcds` (21.5 s) dominate.
An ALL selection costs ≈36.8 s; most other commits cost ~2–5 s. Sample-only narrowing has repeatedly
not moved the official score.

## Working conventions in this repo

- Every candidate is gated by `scripts/gate.py`: 0 sample misses, 0 misses on mutants with ground truth,
  unit tests pass, cost strictly lower than the base, fill adds 0.0 s, and **every patch whose selection
  shrinks must have ground truth** (zero known misses on unverified patches is not evidence). Change one
  rule at a time; revert on FAIL; log every iteration (kept or reverted) in `EXPERIMENTS.md` with measured
  numbers only.
- Official submissions are preserved (tags `submitted-BB-*`; full table in `EXPERIMENTS.md`). Best official
  so far: 174.5 s, 0 misses (ddd9678, 8991e94, 01f0536); earlier champion 8238fd6 181.1 s. Never rewrite or
  force-push submitted commits; new work goes on new `experiment/*` branches, one rule per branch.
- Rejected approaches — do not revive without explicit approval: v10's runtime import-state subprocesses,
  and general per-file offline import-dependency narrowing (`experiment/importdeps-study`). The heavy-test
  closure rule is the approved narrow exception.
- Ground truth for new rules comes from a dedicated corpus generator (`scripts/*_mutations.py` → new
  `../stress_<x>/`, then `stress_mutate.py truth`); never delete existing corpora, truth or worktrees.
- Team split (see the team playbook): Person 1 owns production integration/submission; Person 2
  (verification) owns `tests/`, analysis scripts and `EXPERIMENTS.md`. Don't push or submit without the
  user's explicit approval; the submission is made by hand on the event site with the full 40-char SHA.
- Local gotchas: `~/Desktop` is iCloud-synced — evicted ("dataless") kit files cause `OSError(5)` inside
  Docker (read them on the host first); mutation-truth runs must keep `__pycache__` and cap process pools
  (`PYTHON_CPU_COUNT`) or heavy TPC-DS tests time out and produce false failures. The disk is nearly full:
check `df -h` before heavy validation; don't prune Docker or clean storage. `HANDOFF.md` is local-only (gitignored).
