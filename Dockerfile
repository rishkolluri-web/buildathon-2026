# Same base image the kit labeled every commit in (linux/amd64 on the scorer).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Pinned sqlglot sources (sqlglot's own repository at eb11aec), used to diff patches.
COPY docker/fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py /opt/sqlglot && rm /tmp/fetch_sqlglot.py

ENV BR_SRC=/opt/sqlglot BR_MAP=/app/map.json.gz PYTHONDONTWRITEBYTECODE=1

# Selector + maps built from sqlglot's own test suite (see mapgen/): coverage/fixture/class-usage
# map, identity/equality-safe comparison sites, per-test value trace (dict entry reads, comparisons).
COPY selector.py map.json.gz eqsites.json vmap.json.gz order_volatile.json /app/
# Static indexes over the pinned sources (cross-dialect references, import uses), computed
# once here instead of on every run; the selector recomputes them if this file is absent.
# Bytecode for the pinned sources (read-only at run time): the import-state check below imports
# the patched package in a subprocess, and only the patched files then need compiling.
# Post-import state reference of the pinned sources (selector.import_state, 4 hash seeds).
RUN python -m compileall -q /opt/sqlglot/sqlglot \
    && python /app/selector.py --build-index /app/static_index.pkl /app/import_state.json.gz \
    && chmod 755 /app && chmod 644 /app/selector.py /app/map.json.gz /app/eqsites.json /app/vmap.json.gz \
       /app/static_index.pkl /app/import_state.json.gz /app/order_volatile.json

CMD ["python", "/app/selector.py"]
