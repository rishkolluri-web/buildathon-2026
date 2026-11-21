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
