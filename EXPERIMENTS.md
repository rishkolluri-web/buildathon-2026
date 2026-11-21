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

### Official BB-977554CD (aa103eb, experiment/crossdialect-v6): 196.7 s, 0 misses

| Submission | SHA | Score | Simulated | CPU | Missed |
|---|---|---|---|---|---|
| BB-977554CD | aa103ebfcbbfd2c407640ddbb37bc90f5a3265ce | 196.7 s | 195.335 s | 1.34 s | 0 |
| (best) BB-7C706349 | c00364018665e0abfad31a10173d9044892e7c69 | 190.8 s | 189.804 s | ~1.0 s | 0 |
| BB-709BA77F | 107a4c369c20be07204f3ab85719d92807f57640 | 222.0 s | 220.777 s | ~1.2 s | 0 |

c1 (Teradata "DD") full-suite truth: 2 new failures; c003640 and b2c241e miss test_presto::test_to_char (#624), aa103eb selects both.

### experiment/attrdeps-v7 (WIP, NOT a submission candidate yet)

One rule vs aa103eb: cross-dialect propagation is attribute-level. A reference to family Y whose uses provably read only raw UPPER_CASE constants other than the changed ones (not stored/mutated by code = not metaclass-derived, not class/def names) adds nothing; subclass-only uses follow references to the subclass family without adding users; import-time copies continue with attributes unknown; string-literal lookups (Dialect["teradata"]), star imports, unknown attrs -> aa103eb behaviour. Also catches `import sqlglot.dialects.tsql` (generator.py -> TSQL.CONVERT_FORMAT_MAPPING), invisible to aa103eb's regex.
- Sweep of 710 class settings in referenced dialects: saves 785.9 s on 85 settings vs aa103eb, never wider.
- Cross-dialect truth (../stress_c): c1 Teradata "DD" 2 failing (test_to_char #624 + test_teradata::test_cast); c2 MySQL TIME_MAPPING 16 failing -- c003640 and b2c241e MISS test_hive::test_time (#434); c3 Hive TIME_MAPPING 9 failing. aa103eb and v7 select all.
- Narrowing truth (../stress_w): w1 MySQL SAFE_TO_ELIMINATE_DOUBLE_NEGATION 1 failing, v7 selects 7 tests, 0 missed (aa103eb 652, c003640 1096). w2-w6 pending (truth runs stalled by iCloud eviction of ../.venv313; re-downloading).
- Regression tests: Teradata->Presto test_to_char; MySQL mapping -> test_hive::test_time, Presto test_to_char/test_time, SingleStore, T-SQL; Hive mapping -> Presto/Spark/T-SQL. Unit tests OK.
- CPU (loaded machine): sample c003640 0.214 / aa103eb 0.322 / v7 0.440 s.

### experiment/typecheck-v8 (WIP from champion 8238fd6; NOT a candidate until truth for ../stress_e and ../stress_b is in)

Two independent rules (ablation: BR_OFF=typecheck / BR_OFF=eqswap):
- B (typecheck): a class imported from a dialect family and used only as isinstance/issubclass's class operand reads none of its attributes (no __instancecheck__/__subclasscheck__ in sqlglot). Fixes optimizer/pushdown_predicates.py's isinstance(dialect, (Athena, Presto)) pulling the 94 tests of line 42 (incl. test_execute_tpcds) into Athena/Presto setting changes. Not solved by attrdeps-v7 (it classified this as "other" -> use). Settings sweep: 8 more Athena settings narrowed, 25.45 -> 10.69 s each.
- A (eqswap): mapgen/eq_trace.py rewrites every single-op is/is not/==/!= comparison in pinned sqlglot into a recorder (installed via sitecustomize, so subprocesses too) and runs the full pinned suite (1621 passed under tracing, i.e. no behaviour change). A site is "interchangeable" if EVERY execution compared None vs a plain/builtin type, two members of the same Enum, two bools, or two objects with object.__eq__/__ne__ (never ints/strs/custom __eq__ such as Expression or the _Dialect metaclass). 1626 sites observed, 578 interchangeable -> eqsites.json (20 KB, shipped in the image). A single-file patch whose ONLY AST difference is is<->== / is not<->!= at interchangeable sites runs exactly like the original (same suite, same pinned code up to that comparison, same result there) -> selects nothing. Anything else (other edits, more files, chained comparisons, unobserved or unsafe sites) -> normal selection.
- Priority C (4d63, Snowflake NUMERIC_ESCAPES value): the import-time read (dialect.py:404) uses keys only, but tokens.py:606 passes the full dict to every Snowflake tokenization at runtime -> every Snowflake tokenizer user can observe it; no defensible narrowing. Kept; import-time fallback (m016) untouched.

| Config | Sample | 9 mutation corpora |
|---|---|---|
| champion 8238fd6 | 170.35 | 1575.88 |
| A only | 120.11 | 1354.98 |
| B only | 170.35 | 1531.58 |
| A+B | 120.11 | 1310.68 |

Sample: 2haz 25.45 -> 0 s, 66ks 24.79 -> 0 s (both 0 failing in answers). Corpora ../stress_e (6 interchangeable swaps that must have 0 failing + 4 unsafe negative controls) and ../stress_b (3 Athena settings): full-suite truth running. Unit tests 49 OK.

#### typecheck-v8 validation (candidate 5bf7a1edd3d213e6da2cf0fca9e5be8190fabdce)

- ../stress_e truth: 6 interchangeable swaps (e1-e6) 0 failing each, candidate selects 0; negative controls kept (all tests): e8 `.upper() is text` 1141 failing incl. 34 collection errors, e7/e9/e10 0 failing.
- ../stress_e2: 20 random interchangeable sites (seed 20261121, both directions, is/==, is not/!=): 1621 passed each, 0 failing; candidate selects 0 for all.
- ../stress_b (Athena settings, rule B): b1 1, b2 1, b3 2 failing; 0 missed (454 selected vs champion 450 incl. fill; cost 25.45 -> 10.69 s).
- Gate vs champion 8238fd6, sample + 10 corpora: PASS, 0/30, 0/89 truth mutants, fill 0.0 s, all narrowed patches verified; total 2038.0 -> 1430.8 s (-607.2 s, mostly the swap corpora). Unit tests 49 OK.
- Docker (exact 5bf7a1e, no-cache amd64, make sample): 120.108 s, 0 misses (champion 170.349 s); eqsites.json in /app, mode 644; protocol OK.
- CPU: sample 0.434 -> 0.558 s; 77 non-swap patches 0.961 -> 0.957 s; each real swap patch ~+0.27 s (AST compare of large files).

## experiment/perf-v9 (base: typecheck-v8 candidate 5bf7a1e, code-identical to e790318)

Bench: `scripts/bench.py` (fresh process per run = cold start, revisions interleaved, user+sys CPU of
the selector process via wait4, max RSS). "mut" = all 141 corpus mutants in one data dir.
Profile of 5bf7a1e on mut (cProfile, 42.7 s inflated): swap_only_sites 81 % (deepcopy + normalise +
ast.dump of both whole trees on every patch passing the is/== prefilter: 30/141), Differ.key ast.dump
2.1 s, enum_like_classes 1.5 s, family_users/import_uses 0.9 s, family_dependents 0.11 s; map load 33 ms.

### Track B (selector CPU; selections byte-identical to 5bf7a1e on sample + 141 mutants)

| Step | Change | Sample CPU (median, 5–7 runs) | Mutants CPU | Peak RSS (sample / mut) | Kept |
|---|---|---|---|---|---|
| 0 | 5bf7a1e (v8) | 0.577 s (sd 0.001) | 13.81 s | 112.5 / 196.9 MiB | — |
| 1 | swap_only_sites: copy-free parallel walk, untouched statements skipped (e1ded18) | 0.442 s | 3.69 s | 109.4 / 144.3 MiB | yes |
| 2 | enum_like_classes lazily, auto() scan once per class (e1ded18) | 0.431 s | 3.53 s | 109.4 / 147.1 MiB | yes |
| 3 | build-time static index of pinned-source analyses (4498636; /app/static_index.pkl via `--build-index`) | 0.224 s | 3.23 s | 89.6 / 146.1 MiB | yes |
| 4 | hash-consed AST fingerprint for Differ.key | 0.202 s | 3.45 s (+7 %) | — | **rejected** (no sample gain alone, mutants slower) |
| 5 | header-line walk only when a region hits import-time lines (370184c) | 0.203 s | 3.07 s | 89.7 / 146.0 MiB | yes |

Equivalence evidence: scripts/diff_swap.py over EVERY single-op is/==/is not/!= site in pinned sqlglot
(9106 synthetic patches: swap, wrong-negation, ==→<=, swap+edit, swap+swap, swap+whitespace):
0 mismatches between the old and new swap classification (4962 swap-only by both).
Unit test: build-time index == lazy == corrupt-index output on every edge-case patch.
gate.py fix: the base revision now also gets its eqsites.json (before, gating against v8 ran v8 without
its swap rule and overstated the base cost).

### Official BB-D230C8FC (01f0536, v12): 174.5 s, 0 misses (identical to the previous champion)

### experiment/attr-read-v13 (4bde6ef10696a6cc7269cc8961949ce0f7bf28b7, base 01f0536)

- Traced attribute reads (owner-resolved) replace line-coverage readers in class_data_readers.
- Setting-flip corpus (all 803 one-line boolean UPPER class settings, projection): 6968.0 -> 5771.8 s (−1196.2 s),
  498 improved, 0 slower. Ground truth on 40 of them (../stress_ar: 22 largest savings, 13 random, 5 unchanged
  controls): 0 missed; projected 709.3 -> 202.9 s on those 40.
- Dialect.SAFE_TO_ELIMINATE_DOUBLE_NEGATION flip (the audit's f08 type): NOT improved (25.454 s) — the trace shows
  test_execute_tpcds genuinely evaluates the setting (owner Dialect, 7 readers).
- Gate vs 01f0536, sample + 18 corpora: PASS, 0/30, 0/259 truth, unit tests OK; total 3923.1 -> 3391.6 s.
  Sample 101.552 -> 101.368 s (535z narrowed, its failing test kept); 141 older mutants 1301.6 -> 1276.7 s (14 improved,
  0 slower); v12 comparison-constant corpus unchanged (456.5 s).
- CPU (bench, 3 runs): sample 0.395 -> 0.425 s; 141 mutants 3.253 -> 3.591 s. vmap 160 -> 234 KB.
- Docker (no-cache amd64, git archive 4bde6ef): make sample 101.368 s, 0 misses; image == local on all 40 flip mutants.

### experiment/heavy-all-v14 (base 4bde6ef = v13): trimmed ALL fallbacks — code 91c5abd

Diagnosis: 3 official submissions at exactly 174.5 s while the sample improved. The sample has 0 ALL commits; the hidden set costs ≈72 s more ≈ 2 × 36.82 s (cost of an ALL selection, floored by test_max_nodes 28.8 s + 4 workers).
- ALL census (`scripts/all_census.py`, 338 local patches on v13): 33 ALL patches. 28 are import-time lines, mostly in core modules (parser, tokenizer_core, expressions/core, dialects/dialect), which every test imports.
- Heavy closures (`mapgen/heavy_closure.py` → heavy.json): each test > 3 s runs alone and the sqlglot modules it imports are recorded. Counts: test_max_nodes 37/185 modules, tpcds 90, tpch 67, optimized_tpch 71, generator_memory 151.
  - A parent-process-only first attempt was wrong. It missed tpcds's spawned pool children importing parsers/mysql.py, so on m068 it would have dropped tpcds, which really fails.
  - With children recorded: 0 violations over the 32 heavy-test failures in 258 earlier truth runs.
- Rule: an ALL from in-place edits of sqlglot modules → every test minus the slow tests whose closure contains none of the changed modules. Fail-closed (plain ALL) when:
  - the import-time code (old or new) mutates shared state, or defines a class that could replace a registered dialect;
  - the change is a rename, delete, new file, config file or test file;
  - heavy.json is missing.
- Validation corpora with full-suite truth: ../stress_hv (32 syntax errors in non-core modules) and ../stress_hv2 (7 import-time edits; `scripts/all_mutations.py`). Heavy tests fail 31 times there and all 31 are selected.
- Gate vs 4bde6ef (all 19 corpora): PASS. 0/30 sample, 0/298 truth mutants. Total 4827.5 → 4279.2 s (−548.3 s). Fill 0.0 s. Unit tests OK, including 4 new regression tests (m068-like mysql syntax error keeps tpcds, spark2 import-time line, foreign-mutation and dialect-name fail-closed cases).
- Per trimmed patch: 36.82 → 19.08 s when tpcds is also exempt; 36.82 → ≈28.3 s when tpcds is kept.
- CPU on the sample (bench, 5 runs × 2 interleaved, idle): 0.423/0.424 s (4bde6ef) vs 0.423/0.428 s.
- Docker (no-cache amd64, `git archive 91c5abd`, kit make sample): 101.368 s, 0 misses.
  - Image on the 43 trimmable patches (as nobody, read-only, no network): byte-identical to local output, all 43 trimmed.
- Expected official effect: only on hidden ALL commits that edit non-core modules (dialect, parser, generator or optimizer files). Core-module ALL commits are unchanged.

### experiment/heavy-lines-v15 (base 91c5abd = v14): import-time lines → tests executing them in isolation — code 4a0f29d

- Why: almost all core-module ALL fallbacks are "import-time lines". parser.py has 3132 of them, tokenizer_core 829, expressions/core 1149. They come mostly from SQL templates parsed in class bodies (generators/duckdb.py lines 95–102 and 2052–2388, typing/hive.py 97–98), found by tracing the innermost module or class-body frame.
- `mapgen/iso_imp.py` → iso_imp.json.gz (87 KB). Every numbered test runs alone, with coverage in every process (pool children included). For each import-time line it records the tests whose own run executes it. Build took 247 s with 7 jobs; 1 unknown test (test_dialect::test_lazy_load, fails in isolation), which is always selected.
- `heavy.json` now also holds the slow tests' executed lines; this is the fallback when iso_imp is absent. In the earlier truth runs, every non-syntax heavy-test failure (42) hit lines that test executes.
- Rule: an import-time function-body line selects coverage ∪ the tests whose isolated run executes it ∪ unknown tests, instead of ALL.
- New corpus ../stress_iso: 30 core import-time mutants (itb/ops in dialect, expressions/core, errors, generator, helper, jsonpath, parser, tokenizer_core). Full-suite truth: 0 misses, with up to 1255 failing tests per mutant.
- Gate vs 91c5abd (all 20 corpora): PASS. 0/30 sample, 0/328 truth mutants. Total 5383.7 → 5160.1 s (−223.6 s). Fill 0.0 s. Unit tests OK.
- The gain per core ALL is modest: 36.8 → about 27.5 s when test_max_nodes doesn't execute the line. Most tests execute core tokenizer and parser lines anyway.
- CPU on the sample (bench, 5 runs): 0.430 s (v14) vs 0.430 s.
- Docker (no-cache amd64, `git archive 4a0f29d`, kit make sample): 101.368 s, 0 misses. Image output on ../stress_iso (as nobody, read-only, no network) is byte-identical to local.

### experiment/iso-mutsig-v16 (base 88ea19c = v15 4a0f29d + log): fail closed when an import-time edit changes mutation targets — code f84987c

- Parallel audit of v15 (cost / safety / runtime investigators):
  - Runtime: clean. All 6 data files in Dockerfile + gate/bench export lists; image-source subset (250 .py) gives
    byte-identical selections on 92 patches; 35 missing/corrupt data-file variants never narrower; CPU not worth pursuing.
  - Cost: the import-channel idea saves 9.4 s on 407 local patches (flip-parser-1960 only): dropped. 127 patches
    still >= 25 s; floor test_execute_tpcds 25.45 s (fails in 16 of 51 generator-style ones), test_max_nodes fails in 3.
  - Safety: new corpus ../stress_sa (3 mutants, full-suite truth, base 0 failing):
    - sa1 dialect.py:355 `gen_cls.TRANSFORMS.pop` -> `Generator.TRANSFORMS.pop`: 7 failing; v15 selects 1363 and
      MISSES 3 (test_jsonpath x2, test_optimizer::test_eliminate_subqueries); v14 selects ALL. iso_tests had no
      shared-state guard (tests that never import a Trino dialect alone share Generator.TRANSFORMS with one).
    - sa2 generators/solr.py adds `SUPPORTED_JSON_PATH_PARTS = set()`: 3 failing, v14 AND v15 select 12 and miss all 3
      (class rule; the metaclass pops from the inherited base TRANSFORMS). Pre-existing, not fixed here.
    - sa3 dialect.py:424 False -> True (own-class store): 2 failing, v15 0 missed.
- Rule: `mutation_sites(tree)` = multiset of store/delete targets, mutating-call receivers, dynamic/registration calls,
  global/nonlocal (ast.dump, position-free). On an import-time line edit, if old != new -> plain ALL (v14 behaviour;
  the slow-test line trim has the same blind spot). Value-only edits keep v15's isolated coverage. Unit test
  `test_import_time_mutation_target_change_selects_everything` (sa1 shape -> ALL, sa3 shape stays narrower).
- Gate (all 21 corpora incl. stress_sa, --allow-cost-increase): sample 0/30; only mutation miss = sa2 (same miss in
  4a0f29d and 91c5abd); unit tests 64 OK; fill 0.0 s; narrowed patches verified.
  vs v15: 5226.5 -> 5235.1 s (+8.6): sa1 28.22 -> 36.82, itb-expressions-core-1379 1554 -> 1620 tests (cost unchanged;
  its subscript key edit counts as a target change). No other patch changes. vs v14 91c5abd: 5466.1 -> 5235.1 s (-231.0).
- CPU (bench, sample, 5 runs x 2 interleaved; cloudd busy): v15 0.425/0.430 s, candidate 0.428/0.428 s; sim 101.368 s.
- Docker (no-cache amd64, `git archive f84987c`, kit make -o build sample): 101.368 s, 0 misses. Image run with the kit RUN_FLAGS (uid 65534, read-only, no network) on ../stress_sa: byte-identical to local output.

### Official BB-9E4A99AD (f84987c, v16 = v15 + mutation-target guard): 172.2 s, 0 misses

- Runs: 171.078 / 171.078 / 171.078 s simulated, CPU 1.08 / 1.09 / 1.08 s, 0 commits missed. Identical to v14
  (91c5abd, BB-A7FC9370) to the millisecond.
- Reading: no hidden commit's busiest-worker load changed between v14 and v16, so the hidden set has no import-time
  function-body edit that v15's isolated coverage narrows enough to drop a slow test (or none at all). The remaining
  hidden excess over the sample (~70 s) is not reached by the v15/v16 rules.

### experiment/sharedmut-v17 (base 8b67f16 = v16 f84987c + logs): guard class settings that steer import-time mutation of shared containers

- Official BB-9E4A99AD (v16) = v14 to the millisecond: stop treating v15's local corpus savings as hidden-set evidence.
- Miss fixed: ../stress_sa sa2 (SolrGenerator gains `SUPPORTED_JSON_PATH_PARTS = set()`; `_Dialect.__new__` then pops
  every JSONPath transform from the TRANSFORMS dict SolrGenerator inherits = base Generator.TRANSFORMS; 3 failing tests
  that never import Solr alone). v14, v15, v16 all selected 12 and missed all 3.
- Rule (source-derived, no names hard-coded): `shared_mutation_keys(fn)` finds in-place mutations of containers reached
  through an attribute (`x.Y.pop/update/...`, `x.Y[k] = / del`) in every function body that runs at import, minus
  containers the function itself assigned and subscript chains on locals; per mutation, the attribute/string keys
  whose value reaches it (source-order reaching definitions, comprehension scopes excluded, attribute stores are not
  rebinds) or controls it (enclosing if/while/for). Kept only if the container name is a valued class attribute somewhere
  in sqlglot (inheritable). Result at the pinned commit: containers {TRANSFORMS, _classes}; value keys include
  SUPPORTED_JSON_PATH_PARTS, generator_class (plus generic registry-key names). A class-level edit that changes a
  value-key binding, or adds/removes a container binding, -> plain ALL with `no_trim` (a process that imported the
  module earlier shares the mutated container, so slow-test closures are no bound; the v16 sa1 guard sets it too).
  Precomputed in static_index.pkl (`_shared_mut`); lazily otherwise.
- Variant corpus ../stress_sm (scripts/sharedmut_mutations.py, full-suite truth, base 0 failing):
  sm1 Dune inherits Trino TRANSFORMS + narrower set: 0 failing (v16 14 sel, v17 ALL); sm2 SQLite owned set drops
  JSONPathSubscript: 3 failing (v16 696 sel 0 missed, v17 ALL); sm3 Solr own TRANSFORMS copy + empty set: 0 failing
  (v16 12, v17 ALL); sm4 negative control Dune TRY_SUPPORTED False->True: 0 failing, guard does not fire (0 sel both).
- Gate vs f84987c (22 corpora incl. stress_sa, stress_sm; --allow-cost-increase): PASS. 0/30 sample, 0/335 truth mutants,
  unit tests 65 OK, fill 0.0 s, narrowed verified. Cost 5263.4 -> 5373.6 s (+110.3): only sa2, sm1, sm2, sm3 change
  (each -> 36.817 s); the other 410 patches (incl. the whole sample) are byte-identical.
- CPU with each revision's own static index (as in the image; 7 interleaved runs): v16 0.228 s, v17 0.231 s; outputs
  identical. Without an index (bench.py's mode) v17 is 1.27 s vs 0.43 s (lazy class-attribute scan): image-irrelevant.
- Docker (no-cache amd64, `git archive ecf5f82`, kit make -o build sample): 101.368 s, 0 misses; static index built in
  the image (incl. `_shared_mut`); image run with the kit RUN_FLAGS on ../stress_sa and ../stress_sm byte-identical to local.

### Speed study after v17 (no rule kept)

- Uniform single-token site census (scripts/site_census.py: every is/==, ==/!=, </<=, and/or, dropped not, int ±1,
  True/False site in sqlglot/*.py = 13,753 sites; random 1,500, seed 20261121; v17 + its static index):
  mean 8.69 s/site (sample 3.4 s/commit, so the hidden generator is not uniform). 322 sites >= 25 s: 229 plain line
  coverage (optimizer 123, parser 70), 84 import-time iso, 9 other. Cost floors: test_execute_tpcds alone 25.45 s on
  166 sites; test_max_nodes in only 66 of 1,500 (36.82 s on 63).
- Only and/or swaps can be proved result-preserving by a value trace (dropped `not`, bool and int constants always
  change the value; tpcds exclusion would need output-equivalence proof). Prototype truthiness trace of every 2-operand
  and/or in sqlglot function bodies, each slow test run alone (pool children included, all 5 pass under the rewrite):
  of 87 census and/or sites >= 25 s, tpcds saw only result-preserving combinations at 13, test_max_nodes at 4.
  Expected hidden gain ~1-2 s per submission (and/or ~15 % of sites x 15 % droppable x ~20 s x ~2 costly commits):
  negligible against a vtrace rebuild + truth corpus + gate. Not implemented.

### Official BB-D4968574 (ecf5f82, v17 = v16 + shared-container guard): 172.2 s, 0 misses

- Runs: 171.078 / 171.078 / 171.078 s simulated, CPU 1.13 / 1.14 / 1.14 s, 0 commits missed. Simulated identical to v14 and
  v16 (as expected: no hidden commit reaches the guard). CPU +0.05 s vs v16 (1.08/1.09/1.08): unrounded totals
  ≈ 172.22 (v17) vs ≈ 172.16 (v16); both display 172.2 s. Local selector CPU with the index was 0.228 vs 0.231 s, so the
  extra is mostly container/load variance or the larger static index; not investigated further.
