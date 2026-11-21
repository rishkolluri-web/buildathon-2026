# Loaded by every Python process when mapgen/vtsite is on PYTHONPATH: installs vtrace's import
# hook (VTRACE_SRC/VTRACE_OUT set) so subprocesses are traced too, under "<subprocess>".
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    if os.environ.get("VTRACE_SRC"):
        import vtrace

        vtrace.install()
except Exception as e:  # never break the interpreter
    print(f"vtrace not installed: {e!r}", file=sys.stderr)
