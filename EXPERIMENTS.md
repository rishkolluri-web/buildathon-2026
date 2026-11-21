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
