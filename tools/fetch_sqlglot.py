"""Download sqlglot's source at the pinned commit (build time only).

    python fetch_sqlglot.py /app/sqlglot-src

Keeps sqlglot/ and tests/*.py (the selector reads the original Python files
to compare them with each patch) and checks a SHA-256 over the kept files'
paths and contents, which does not depend on how GitHub compresses archives.
"""

import hashlib
import io
import os
import sys
import tarfile
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz"
CONTENT_SHA256 = "452fef57462d70c340df9007b101887cb22ef2c9e175240885b697e8cc785a28"


def main() -> None:
    target = sys.argv[1]
    payload = urllib.request.urlopen(URL, timeout=120).read()
    prefix = f"sqlglot-{COMMIT}/"
    kept = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile() or not member.name.startswith(prefix):
                continue
            relpath = member.name[len(prefix):]
            if not (relpath.startswith("sqlglot/") or relpath.startswith("tests/")):
                continue
            if not relpath.endswith(".py"):
                continue
            kept[relpath] = archive.extractfile(member).read()
    digest = content_digest(kept)
    if digest != CONTENT_SHA256:
        sys.exit(f"sqlglot source checksum mismatch: {digest}")
    for relpath, content in kept.items():
        destination = os.path.join(target, relpath)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "wb") as handle:
            handle.write(content)
        os.chmod(destination, 0o644)
    print(f"kept {len(kept)} files from sqlglot {COMMIT[:12]}")


def content_digest(files: dict) -> str:
    digest = hashlib.sha256()
    for relpath in sorted(files):
        digest.update(relpath.encode() + b"\0" + hashlib.sha256(files[relpath]).digest())
    return digest.hexdigest()


if __name__ == "__main__":
    main()
