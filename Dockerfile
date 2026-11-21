# Same base image the kit labeled every commit in (linux/amd64 on the scorer).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Pinned sqlglot sources (sqlglot's own repository at eb11aec), used to diff patches.
COPY docker/fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py /opt/sqlglot && rm /tmp/fetch_sqlglot.py

ENV BR_SRC=/opt/sqlglot BR_MAP=/app/map.json.gz PYTHONDONTWRITEBYTECODE=1

# Selector + maps built from sqlglot's own test suite (see mapgen/): coverage/fixture/class-usage
# map, identity/equality-safe comparison sites, per-test value trace (dict entry reads, comparisons),
# import closure + executed lines of the slowest tests (mapgen/heavy_closure.py), import-time lines
# executed by each test run alone (mapgen/iso_imp.py), solo value/identity traces of the slow tests (mapgen/heavy_vt.py).
COPY selector.py map.json.gz eqsites.json vmap.json.gz heavy.json iso_imp.json.gz heavy_vt.json /app/
# Static indexes over the pinned sources (cross-dialect references, import uses), computed
# once here instead of on every run; the selector recomputes them if this file is absent.
# The selector's own bytecode is compiled once here (a script run as `python file.py` is recompiled on every
# start); unchecked-hash .pyc files are used as-is regardless of mtimes. PYTHONDONTWRITEBYTECODE only stops writes.
RUN python /app/selector.py --build-index /app/static_index.pkl \
    && python -m compileall -q --invalidation-mode unchecked-hash /app/selector.py \
    && chmod 755 /app /app/__pycache__ && chmod 644 /app/selector.py /app/map.json.gz /app/eqsites.json /app/vmap.json.gz /app/heavy.json /app/iso_imp.json.gz /app/heavy_vt.json /app/static_index.pkl /app/__pycache__/*.pyc

CMD ["python", "-c", "import sys; sys.path.insert(0, '/app'); import selector; selector.main()"]
