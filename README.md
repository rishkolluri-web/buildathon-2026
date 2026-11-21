# buildathon-2026 — RWX Blast Radius

Picks the sqlglot tests a patch can break, and packs them onto workers.

## How it works

`selector.py` reads `/data/tests.json` and `/data/commits/*.patch` and prints one JSON line per commit.

1. **Map** (`map.json.gz`, ~440 KB, built offline by `mapgen/` from sqlglot's own test suite at the pinned commit `eb11aec`):
   - per-test line coverage of `sqlglot/` and `tests/` (pytest-cov contexts; process-pool work is run inline with pickling preserved so it is credited to the calling test);
   - which data files (SQL fixtures, …) each test opens (audit hook);
   - which sqlglot classes each test instantiates, rolled up to base classes.
2. **Per patch**: apply it to the pinned source, parse old/new with `ast`, and locate the smallest differing statements.
   - inside a function or lambda → tests that executed those lines;
   - class-level data (dict entries, constants, `arg_types`) → tests using that class or a subclass;
   - comment / whitespace / docstring-only → nothing;
   - fixture or other data file → tests that opened it;
   - syntax error, deleted/renamed module, module-level statement, pytest config → every test.
   Any analysis error also falls back to every test — never a crash.
3. **Packing**: longest-first onto k = 1..8 workers, keep the k minimising busiest worker + 2 s × k.

No sample answers are used anywhere in the selector or the map.

## Local checks (no Docker needed)

```sh
python3 tests/evaluate.py --kit ../blast-radius-sample --src ../sqlglot     # official score.py on the 30 samples
BR_SRC=../sqlglot BR_KIT=../blast-radius-sample python3 -m unittest discover -s tests -v
```

With Docker: copy the kit's `Makefile`, `score.py` and `sample/` next to this `Dockerfile` and run `make sample`.

## Rebuilding the map

```sh
mapgen/run_coverage.sh <sqlglot@eb11aec> <python 3.13 with kit requirements.lock + pytest-cov> <outdir> <tests.json>
cp <outdir>/map.json.gz map.json.gz
```
