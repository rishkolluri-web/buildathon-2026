# HANDOFF — RWX Blast Radius (Builders Buildathon)

Deadline: **Sun Nov 22, 11:00 AM ET**. Only the final counts; it scores the commit the team picks.

## 1. Goal

For each sqlglot patch (pinned `eb11aec`), `selector.py` picks the tests to run on ≤ 8 workers.
Score = busiest worker's seconds + 2 s/worker, **+300 s per missed failing commit**, + CPU.
Target: **< 150 s official with zero misses** (correctness first).

| Version | Commit | Official | Local sample (Docker) | Notes |
|---|---|---|---|---|
| **Champion (in final)** | `8238fd692c02fc764e953ae4a15706a64e6c01d8` (`experiment/attrdeps-v7`) | **181.1 s, 0 missed** (BB-CD8DBEBB) | 170.349 s | Do not touch |
| **Next candidate** | `5bf7a1edd3d213e6da2cf0fca9e5be8190fabdce` (`experiment/typecheck-v8`) | not submitted | **120.108 s, 0 misses** | Gate PASS, ready for team review |
| Older | `c003640` 190.8 s · `aa103eb` 196.7 s · `107a4c3` 222.0 s | | | `c003640` has confirmed gaps (new dialect files; Teradata/MySQL time mappings) |

## 2. Key architectural decisions

- **Offline map** (`map.json.gz`, built only from the pinned suite): per-test line coverage, opened files, class users (rolled up to bases), import-time lines, a static read index. Never sample answers or patch→test tables.
- **Runtime** (`selector.py`): AST diff → regions → tests; anything unresolvable selects **all tests** (syntax errors, import-time lines (m016 lesson), star imports, new classes, conftest…). `pack()` is already at the measured lower bound; `fill_idle()` adds free tests at 0.0 s.
- **Class-level data** → runtime readers of the attribute ∩ tests that reach the class (users, dialect family, or any line of its file).
- **Cross-dialect data flow** (attrdeps-v7): references from other families (imports, dialect-name literals) are followed per attribute; import-time copies, metaclass-derived names, literal lookups (`Dialect["teradata"]`) and unknown uses stay conservative.
- **New in v8 (candidate):**
  - *Type-check-only imports*: a class used only in `isinstance`/`issubclass` is not a data dependency (fixes `pushdown_predicates.py` pulling TPC-DS into Athena/Presto setting changes).
  - *Identity/equality swaps*: `mapgen/eq_trace.py` traced every `is`/`==` comparison over the full pinned suite; `eqsites.json` lists the 578 sites where every execution compared interchangeable operands (None vs plain/builtin, same Enum, bools, identity-eq objects). A single-file patch that only swaps `is`↔`==` / `is not`↔`!=` at such sites selects nothing.
- **Verification discipline**: every narrowing needs real full-suite ground truth; `scripts/gate.py --base <champion> --stress …` must PASS (0 sample misses, 0 mutation misses, unit tests, cost down, fill 0.0 s, narrowed patches verified), then Docker `make sample` on the exact SHA.

## 3. Files modified (on `experiment/typecheck-v8`, vs champion)

- `selector.py` — `isinstance` typecheck kind in `import_uses`/`classify`; `swap_only_hunks`/`swap_only_sites` + `eq_safe` in `py_change`; `BR_EQ` path; ablation switches `BR_OFF=typecheck,eqswap`.
- `eqsites.json` (new, 20 KB) and `Dockerfile` (COPY + chmod for it).
- `mapgen/eq_trace.py`, `mapgen/eqsite/sitecustomize.py` (new) — the comparison tracer.
- `tests/test_selector_edgecases.py` — swap and isinstance cases (49 unit tests pass).
- `scripts/targeted_mutations.py` — mutation sets t/u/v/w/e.
- `EXPERIMENTS.md` — full log with every measurement and official result.

Local-only evidence (not in git; back up): `../stress`, `../stress_h3`, `../stress_x`, `../stress_u`, `../stress_v`, `../stress_c`, `../stress_w`, `../stress_e`, `../stress_e2`, `../stress_b` (patches + `truth/`), and local tags `submitted-BB-*` (not pushed).

## 4. Active bugs / blockers

- **No known misses.** Gate vs champion: 0/30 sample, 0/89 truth mutants. Ground truth for the swap rule: 26 interchangeable swaps, 0 failing tests each; the unsafe controls are kept.
- **Unproven, not broken**: the swap rule trusts one full traced run (operand types per site); the hidden final may contain change kinds we never mutated.
- **CPU**: each real swap patch costs ~+0.27 s (AST compare of big files); non-swap patches unchanged.
- **Environment**: `~/Desktop` is iCloud-synced. Low disk space evicts files ("dataless") and stalls test runs and Docker. Read the files first (`find … -flags +dataless`), or mark `~/Desktop/buildathon` "Keep Downloaded". About 8 GB free; the storage cleanup is **cancelled**.
- Docker CLI: `export PATH=$HOME/.docker/bin:$PATH`.

## 5. Exact next step

Team review, then submit **`5bf7a1edd3d213e6da2cf0fca9e5be8190fabdce`** (repo `https://github.com/rishkolluri-web/buildathon-2026`) on the track page. Keep `8238fd6` as the final pick unless the new commit officially beats **181.1 s with 0 misses**. Record the result in `EXPERIMENTS.md` and tag it `submitted-BB-<id>`.

Next research direction (only after that): extend the trace-based equivalence idea to other operator swaps (`<`/`<=`, `and`/`or`, constants), each needing per-site operand records plus new ground-truth corpora.
