# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The pinned sqlglot source (GitHub archive of the pinned commit, checksummed):
# the selector compares each patch against these original files.
COPY tools/fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py /app/sqlglot-src && rm /tmp/fetch_sqlglot.py

# Per-test dependency map, built from the pinned commit by tools/build_map.py.
COPY maps/depmap.json.gz /app/maps/depmap.json.gz
COPY selector.py /app/selector.py
RUN chmod -R a+rX /app

CMD ["python", "/app/selector.py"]
