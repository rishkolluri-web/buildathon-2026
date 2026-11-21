"""Build step: fetch sqlglot's own repository at the pinned commit and keep its .py sources.

The selector diffs each patch against these files. Only sqlglot/ and tests/
Python files are kept (a few MB); everything is made world-readable for `nobody`.
"""

import io
import os
import sys
import tarfile
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz"
DEST = sys.argv[1] if len(sys.argv) > 1 else "/opt/sqlglot"

data = urllib.request.urlopen(URL, timeout=120).read()
kept = 0
with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
    for member in tar.getmembers():
        parts = member.name.split("/", 1)
        if len(parts) != 2 or not member.isfile():
            continue
        rel = parts[1]
        if not rel.endswith(".py") or not (rel.startswith("sqlglot/") or rel.startswith("tests/")):
            continue
        out = os.path.join(DEST, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(tar.extractfile(member).read())
        os.chmod(out, 0o644)
        kept += 1
for root, dirs, _ in os.walk(DEST):
    for d in dirs:
        os.chmod(os.path.join(root, d), 0o755)
os.chmod(DEST, 0o755)
if kept < 200:
    sys.exit(f"only {kept} source files extracted from {URL}")
print(f"kept {kept} files from sqlglot@{COMMIT[:7]} in {DEST}")
