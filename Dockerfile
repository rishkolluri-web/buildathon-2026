# Same base image the kit labeled every commit in (linux/amd64 on the scorer).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Pinned sqlglot sources (sqlglot's own repository at eb11aec), used to diff patches.
COPY docker/fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py /opt/sqlglot && rm /tmp/fetch_sqlglot.py

# Selector + coverage/fixture/class-usage map built from sqlglot's own test suite (see mapgen/).
COPY selector.py map.json.gz /app/
RUN chmod 755 /app && chmod 644 /app/selector.py /app/map.json.gz

ENV BR_SRC=/opt/sqlglot BR_MAP=/app/map.json.gz PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector.py"]
