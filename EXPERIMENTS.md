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

