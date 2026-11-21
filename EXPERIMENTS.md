# Experiments — RWX Blast Radius

Official baseline (run every test, 3 workers): **1614.1 s**. Lower is better.
Record only measured numbers. Sample scores are local estimates (official `score.py`, one run, local CPU), not leaderboard scores.
Perfect-knowledge oracle on the 30 sample commits: **94.4 s**.

| Date | Candidate | Local sample score | Misses on sample | CPU/run time | Official score | Official missed commits | Valid/rejected | Hypothesis | Keep/reject | Notes |
|------|-----------|--------------------|------------------|--------------|----------------|-------------------------|----------------|------------|-------------|-------|
| 2026-11-20 | starter kit | 1614.1 (stated) | 0 | — | — | — | — | run everything, 3 workers | baseline | |
| 2026-11-20 | line coverage only (pools not traced) | 1312.6 | 4 | 0.25 s | — | — | — | per-test line coverage + fixture opens | reject | optimizer/executor tests run work in spawned ProcessPoolExecutor, invisible to coverage |
| 2026-11-20 | + inline pool during mapgen | 696.0 | 2 | 0.17 s | — | — | — | credit pool work to the calling test | reject | missed serde.py: real pools pickle expressions |
| 2026-11-20 | + pickle round-trip in inline pool (experiment/coverage-selector) | **174.1** | **0** | 0.16 s host; 0.47–0.49 s container (amd64 emulated, 3 runs) | not submitted | — | VALID: clean no-cache amd64 build from `git archive d4cdcd3`; official `make sample` 173.972 s, 0 misses; runs as nobody, read-only rootfs; protocol PASS | coverage + fixtures + class users + AST diff | KEEP | expected board ≈ 174.5 s (simulated + CPU) |
| 2026-11-20 | dca2229 (official submission BB-055F40E6) | 174.0 (sample) | 0 (sample) | 0.76–0.78 s (server) | **500.4** | **1** | VALID, scored | — | investigate | practice set: 199.7 s simulated excl. penalty; 3 identical runs → deterministic blind spot |
| 2026-11-20 | a6ee059 (official submission BB-3F1A5954, experiment/safety-v2) | 178.7 (sample, Docker) | 0 (sample) | 0.78 s (server median) | **340.0** | **0** | VALID, scored | setUpClass/collection crediting, import-time + cache + family fallbacks, idle fill | KEEP (known-good) | practice simulated 339.238 s vs 199.665 s for dca2229 excl. penalty: +139.6 s of safety cost |
| 2026-11-20 | experiment/narrow-v3 (candidate, not submitted) | 178.7 (sample) | 0 (sample) | — | — | — | pending Docker + mutation truth | stop escalating def/decorator/stub lines; module-level names -> readers via reference index | INVESTIGATE | 77-mutant cost: v1 737.2 s, v2 1196.8 s, v3 810.2 s; all-tests escalations 8/24/8; ground truth so far 9/77, 0 misses for all three |

## Autoloop (experiment/autoloop): one rule per iteration, kept only if scripts/gate.py PASSes

| Iter | Hypothesis | Gate | Sample | Mutants | Δ total | Truth mutants | Notes |
|---|---|---|---|---|---|---|---|
| 0 | baseline = narrow-v3 71f6133 | — | 178.7 | 810.2 | — | 12 | |
| — | drop tpcds whale on `is`↔`==` edits (2haz, 66ks) | not tried | | | | | rejected: not provably equivalent (str-Enum `==` vs `is`, `__eq__` overrides) |
| — | class data ∩ runtime readers of the attribute (4d63) | not tried | | | | | rejected: NUMERIC_ESCAPES read in _Dialect.__new__ at import → falls back to family anyway |
| 1 | method args/defaults-only header change: body coverage, no class users (rename/decorator change still adds them) | PASS | 178.7 | 785.7 | −24.5 | 12 | m044/m045/m046 narrowed; m045's failing test still selected |
| 2 | import-time line -> users of the module-level statements that triggered it at import (traced import pass) | FAIL (unverified) | 178.7 | 776.3 | −9.4 | 12 | reverted; narrows m014/m016/m021/m051/m052 which have no ground truth yet; patch saved for retry. Gate gained check 6: narrowed patches must have ground truth (iter 1's m046 is pending truth) |
| 3 | class-level data (attrs never read at import) -> class/family users ∩ runtime readers of the attribute | FAIL (unverified) | 170.3 | 778.7 | −15.4 | 12 | reverted, patch saved; sample −8.4 s with 0 sample misses; narrows m034 (no ground truth yet) |
| 1 (re-check) | iter 1 vs 76630bd under strict gate (check 6) once m046 truth arrived | PASS | 178.7 | 785.7 | −24.5 | 14 | all narrowed patches (m044/m045/m046) have ground truth, 0 misses |
| 3 (retry) | H3 class data ∩ attribute readers, after m034 truth | PASS (strict, vs HEAD) | 170.3 | 778.7 | −15.4 | 16 | NOT committed (awaiting review); patch saved |
| — | gate vs a6ee059 for HEAD 965a4a7 | FAIL (unverified) | 178.7 | 785.7 | −411.2 vs a6ee059 | 14 | 11 narrowed patches lack truth: m038-m042, m048, m049, m053-m055, m067 |
| — | SAFETY: Person 1 found R2 (module_change, in 71f6133 and 965a4a7) selects 0 tests for (a) a new module-level Expression class, (b) a removed `from x import *` | reproduced | | | | | both select 0 tests locally; do NOT submit 71f6133/965a4a7 until fixed; a6ee059 unaffected. Person 1 preparing fix + 5 regression tests |
| — | m042 / m048 (0 tests selected) code review | reviewed | | | | | m042: lineage logger used only at lineage.py:342, executed by no test. m048: stub rebound by later defs; no test reads overloads/annotations/signatures (test_sqlglotc mocks mypyc). Ground truth queued next |
| 2026-11-20 | **c003640 (official submission BB-7C706349, experiment/autoloop)** | 178.7 (sample, Docker) | 0 (sample) | 0.97–1.00 s (server) | **190.8** | **0** | VALID, scored; entered in final | V3 narrowing + iter 1 + module_change hole fix | KEEP (new best) | practice simulated 189.804 s vs 339.238 s for a6ee059 (−149.4 s); 3 identical runs |

## experiment/perf-v5 (base c003640 = official 190.8 s)

Baseline c003640 on 77 mutants: 785.7 s, 8 all-test escalations = 294.5 s (37 %): 6 import-time lines (m014 m016 m021 m050 m051 m052), removed import (m066), syntax error (m068). Ground truth 19/77; m042 and m048 now verified (0 failing; c003640 selects 0).

| Iter | Rule | Gate vs c003640 | Sample | Mutants | Δ | Patches changed (removed tests, truth) |
|---|---|---|---|---|---|---|
| 1 | H3: class-level data whose attributes are never read at import -> class/family users ∩ runtime readers of the attribute | PASS (19 truth) | 178.7→170.3 | 785.7→778.7 | −15.4 s | s:535z 11.24→2.85 (−221 tests; failing test_simplify kept); m:m034 9.03→2.04 (−24 tests; truth 0 failing) |
| 2 | H2: import-time line -> users of the module-level statements that triggered it (traced import pass) | **FAIL: real miss** | 170.3 | 769.3 | −9.4 s | m016 (tokenizer_core.py:729, 1572 failing) misses test 1054; m051/m052 unverified. Import-time effects reach tests through shared state beyond trigger statements -> rejected, reverted; the all-tests fallback for import-time lines stays |

| 3 | Class data: readers-only (union of runtime attribute readers, no ∩ with instantiating users: covers class-object reads like `dialect.parser_class.X`); enum-like classes fall back to users+family; new non-test module -> tests that enumerate packages (iter_modules/walk_packages/listdir/glob…); any conftest.py or new tests/**/__init__.py -> all | PASS (22 truth) | 178.7→170.4 | 785.7→779.4 | −14.7 s | vs iter 1: m:m034 2.04→2.72; x_t11_035 (Drill ARRAY_SIZE_NAME) 12→38 tests (c003640: 767). Targeted truth (../stress_h3 t1–t6) in progress: t1/t2 done, t3–t6 pending; x_t11_035 full-suite truth queued. CPU sample 0.212→0.231 s, mutants 0.708→0.773 s (local) |

PR #2 (Person 1, tests/test_selector_risks.py, 5 failure-based regression tests) merged at 5c6dbf5: 5/5 pass on c003640, perf-v5 and iter 3; unittest 39 OK (1 skipped); sample unchanged 170.349 s, 0 misses. Packing: pack() equals the LPT lower bound on every patch (949.051 s = LB), no packing change possible.

### perf-v5 validation (b702e01, 2026-11-20)

Targeted full-suite ground truth (../stress_h3, disposable copies, 2 jobs x 4 xdist workers, pools capped at 2 CPUs, 900 s per-test timeout; baseline failures = 3 test_sqlglotc obsolete-extension tests, subtracted; every new failure maps to a tests.json id):

| Mutant | Failing | c003640 | 6b41c7e (H3 ∩ users) | b702e01 |
|---|---|---|---|---|
| t1 new module in sqlglot/typing/ | 1 (test_typing, #1619) | **MISS** (0 selected) | **MISS** | caught (1) |
| t2 new unreferenced module | 0 | 0 | 0 | 1 |
| t3 class attr read via class object | 2 | 1543 | 39 | 39 |
| t4 enum member -> alias | 64 | 1485 | 1485 | 1485 |
| t5 inherited dict entry | 2 | 1381 | | 57, 0 missed |
| t6 class-level set entry removed | 0 | 1490 | | 5, 0 missed |

Gate vs c003640 with --stress ../stress --stress ../stress_h3: PASS, 0/30 sample, 0/28 mutants missed, total 1083.8 -> 994.5 s (-89.3 s). x_t11_035 (Drill ARRAY_SIZE_NAME; c003640 767 tests, 6b41c7e 12, b702e01 38): fresh baseline identical to cached base; 2 new failures (test_dialect::test_array, test_postgres::test_array_length), no collection errors; b702e01 selects both.

Final gate vs c003640 with --stress ../stress --stress ../stress_h3 --stress ../stress_x: **PASS**, 0/30 sample, 0/29 mutants missed, unit tests OK (39, incl. PR #2), fill 0.0 s, total 1094.4 -> 1002.1 s (-92.3 s). Patches differing from c003640 (all with truth, 0 missed): t3 36.82->5.10, t6 27.45->2.60, t5 27.61->5.55, s:535z 11.24->2.86, m034 9.03->2.72, x_t11_035 10.69->7.66, t1 0->2.01 (c003640 missed), t2 0->2.01. Selector CPU sample median 0.227 -> 0.241 s.

Docker (b702e01, no-cache linux/amd64, kit make sample): **170.356 s, 0 misses**; output identical to local. (Earlier "170.349" was the local evaluator's per-test ms rounding; official score.py uses float seconds.) Recommended next submission: b702e01, pending team review.

test_execute_tpcds (21.454 s) review: selected through coverage of the changed lines (2haz 2jrq 3utz 66ks, m003 m005 m009 m019 m026 m037 m054 m056), module-name readers (m038 m041), duckdb family (m049), file users (t4), or all-tests escalations. It fails in 4/6 coverage-selected cases with truth (2jrq 3utz m003 m005). The two non-failing sample cases (2haz 66ks, `is` -> `==`) are equivalent only given runtime types without __eq__ overrides, not provable from the source -> kept. Packing: 114 patches, pack 1002.112 s vs lower bound 1002.111 s -> no packing gain.

### Follow-up safety audit (uncommitted)

- A new `sqlglot/dialects/myqsl.py` defining `Myqsl(Dialect)` made the existing `tests/dialects/test_dialect.py::TestDialect::test_get_or_raise` fail (`ValueError not raised`). Before the guard, the selector chose 1 test and omitted this failing test; the new-dialect guard selects all tests. This does not change the 30 sample or 29 existing truth-mutant selections: the multi-corpus gate passes with 0 misses, and 40 unit tests pass (1 optional check skipped).
- The three `tests/test_sqlglotc.py` baseline failures in the stress corpus pass when run directly with Python 3.13 and `unittest` (3/3). The pytest-based mutation harness still records them as baseline failures and subtracts them, so its ground truth for those IDs remains limited.
- Diagnosis: the truth harness copied only `sqlglot/` and `tests/`; `test_sqlglotc.py` reads `<root>/sqlglotc/setup.py` in setUpClass, so all three tests errored in every copy (baseline included) and were subtracted. With `sqlglotc/` copied they pass under pytest (3/3). Harness now copies it; a tagged subset run (`--subset tests/test_sqlglotc.py --tag sqlglotc`) over all 84 existing mutants + 3 baselines: 3/3 pass everywhere, so these ids are now verified (gate prints 0 unverifiable baseline ids per corpus).
- Generalized new-module guard: packages whose submodules sqlglot imports by a runtime name (`import_module(f"pkg.{x}")`, or `f"{__name__}.{x}"` in a package `__init__`), derived from the pinned source = {sqlglot.dialects, sqlglot.optimizer}; a new module there -> all tests (optimizer's lazy `__getattr__` turns a missing attribute into a submodule). Cost: t2 (new optimizer module) 2.01 -> 36.82 s.
- 55 stress mutants without truth: none selects fewer tests than c003640.
- Gate vs c003640 (stress + stress_h3 + stress_x): PASS, 0/30 sample, 0/29 mutants missed, total 1094.4 -> 1036.9 s (-57.5 s), fill 0.0 s. Unit tests 42 OK (1 skipped: protocol run needs RWX_DATA/RWX_OUTPUT).
- Follow-up targeted corpus ../stress_u (u1 new dialect, u2 new optimizer module, u3 registration across two files, u4 two class-data edits in two files, u5 base flag, u6 dialect name list, u7 lazy optimizer attribute table): full-suite truth running; then m050/m066/m068 truth (`--only`). Not yet in the gate: u4/u5 are narrower than c003640.
- Follow-up truth (../stress_u, new harness: sqlglotc copied, baseline 1621 passed, 0 errors). Failing = new failures vs baseline; no collection errors, all ids mapped:

  | Mutant | Failing | c003640 | b702e01 | 107a4c3 |
  |---|---|---|---|---|
  | u1 new dialect file | 1 (#193 test_get_or_raise) | **MISS** (0 sel) | **MISS** (1 sel) | caught (all) |
  | u2 new optimizer module | 0 | 0 sel | 1 sel | all |
  | u3 registration across two files | 4 | all | all | all |
  | u4 two class-data edits, two files | 4 (= t3 ∪ x_t11_035 failures) | 1548, 0 missed | 63, 0 missed | 63, 0 missed |
  | u5 base flag SUPPORTS_UESCAPE | 2 | 1381, 0 missed | 26, 0 missed | 26, 0 missed |

  u6/u7 and m050/m066/m068 truth still running. Docker on exact 107a4c3 (no-cache amd64, kit make sample): 170.356 s, 0 misses; protocol check on its output OK.

### Official BB-709BA77F (107a4c3): 222.0 s, 0 misses (simulated 220.777 s) vs c003640 190.8 s (189.804 s)

Regression investigation (hidden commits unknown; only selector.py differs: map.json.gz, Dockerfile, docker/ identical; CPU +0.2 s official, negligible locally):
1. Class data -> all runtime readers by attribute NAME: over all 3701 class-level assignments in pinned sqlglot, wider than c003640 on 148 (avg +15.3 s, up to +25 s: generic dialect flags NULL_ORDERING/TABLE_HINTS/LIMIT_FETCH/SUPPORTS_*, and _hash_raw_args), narrower on 916. Most likely cause (dialect-flag tweaks are common).
2. New module in sqlglot/dialects or sqlglot/optimizer -> all tests: +34.8 s per such commit (c003640: 0 tests).
3. New module elsewhere -> package enumerators: +2 s. 4. Nested conftest/new tests __init__ -> all: never triggered. 5. CPU: ~0.
Fixes (this commit): class data -> readers ∩ (class users ∪ dialect family ∪ tests executing any line of the file); sweep: wider 148 -> 15 (all _hash_raw_args), narrower 916 -> 1051. New module in a name-loaded package -> tests executing the name-based import calls (unknown names are never cached) ∪ enumerators, ALL if none: u1 36.82 -> 9.43 s (95 tests, #193 kept), u2/t2 36.82 -> 12.61 s.
Truth added: u6 0, u7 1, m050 0, m066 0, m068 463 (incl. COLLECT test_mysql.py) failing; all selected.
Gate vs c003640 (stress + stress_h3 + stress_x + stress_u): PASS, 0/30 sample, 0/38 mutants, total 1269.3 -> 1148.0 s (-121.3 s), fill 0.0 s, unit tests 42 OK (1 skipped). Docker (exact snapshot da383f7, no-cache amd64, make sample): 170.349 s, 0 misses, protocol OK. CPU sample 0.244 (c003640) / 0.237 (107a4c3) / 0.249 s; 91 mutations 1.43 / 1.61 / 1.63 s.
c003640 known gap: a new sqlglot/dialects/<name>.py (u1) breaks test_get_or_raise; c003640 selects 0 tests.

### Audit of b2c241e (in progress)

- Docker snapshot da383f7 vs b2c241e: selector.py, map.json.gz, Dockerfile, docker/fetch_sqlglot.py byte-identical (same blob ids); only EXPERIMENTS.md differs -> Docker 170.349 s / 0 misses applies to b2c241e.
- Re-checked truth (b2c241e, 0 missed): t3 39 sel/2 failing, t5 57/2, t6 5/0, x_t11_035 12/2, u1 95/1 (#193; c003640 misses it), u4 43/4, u5 26/2, u7 all/1.
- Class-object read audit: the only such reads in sqlglot/tests are `*_class.NAME`; with readers applicable, only parser.py:107 `dialect.parser_class.LOG_DEFAULTS_TO_LN` has executing tests outside the scope (e.g. 5/6 for DremioParser). Argued safe (read on the dialect in use -> its family users), but verified only by truth below.
- Person 1's f11/f21 TABLE_HINTS patches are not in any branch or local corpus; own risk-class mutants instead (../stress_v, `targeted_mutations.py --set v`): v1/v2 Dremio/Drill LOG_DEFAULTS_TO_LN, v3/v4 Teradata/Drill TABLE_HINTS, v5 Drill NULL_ORDERING, v6 _hash_raw_args, v7/v8 new dialect modules (probed unknown name; test-defined name). Selection cost c003640 / 107a4c3 / b2c241e: v3 10.57/27.45/3.62 s, v4 10.69/27.45/3.83, v5 10.69/26.97/3.75, v6 4.86/27.24/27.06, v7/v8 0/36.82/9.43. Full-suite truth running.

### Cross-dialect miss (Person 1 audit) and fix — experiment/crossdialect-v6

- Confirmed miss: teradata.py `"DD": "%d"` -> `"%e"` breaks tests/dialects/test_presto.py::TestPresto::test_to_char (reproduced in fresh disposable copies, no bytecode: baseline 1 passed / 16 subtests; patched `FORMAT_DATE('%e', ts) != FORMAT_DATE('%d', ts)`). Missed by c003640, 107a4c3 and b2c241e.
- Root cause: parsers/presto.py:94 `build_formatted_time(exp.TimeToStr, "teradata")` -> `Dialect["teradata"]` returns the CLASS (no instance) and format_time reads its TIME_MAPPING; the selector credited class data only to users of the Teradata family. Same blind spot class: Presto copies MySQL.TIME_MAPPING/TIME_FORMAT at import; generators/presto.py reads Hive.INVERSE_TIME_MAPPING; SingleStore names "mysql" in its FUNCTIONS builders.
- Fix: family_users(path) follows cross-dialect references derived from the pinned source (any line importing another family's module or naming its dialect key in a string literal; registry files dialect.py / dialects/__init__.py skipped). Runtime reference line -> tests executing it; import-time reference -> every user of the referencing family, transitively; non-family import-time line -> tests touching that file. 0.08 s CPU, lazy.
- Never narrower than b2c241e on 132 patches; changed: c1 87 -> 88 (adds test_to_char, cost 10.58 s unchanged), c2 MySQL mapping 12.69 -> 27.45 s, c3 Hive mapping 13.16 -> 27.45 s, m001/m036 +1 test (no cost). Sample 170.3 s unchanged.
- Audit set v truth (../stress_v): b2c241e and fix 0 missed on all 8 (v1 1 failing, v2 1, v3 0, v4 0, v5 1, v6 1, v7 1 [c003640 misses], v8 0). Gate vs c003640 (stress, h3, x, u, v): PASS 0/30, 0/47, -115.9 s, fill 0.0 s. Unit tests 44 OK (1 skipped). Cross-dialect corpus ../stress_c (c1 teradata, c2 mysql, c3 hive mapping) truth running.
