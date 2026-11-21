# Loaded by every Python process when mapgen/eqsite is on PYTHONPATH (eq_trace.py installs
# its import hook only if EQTRACE_SRC and EQTRACE_OUT are set).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import eq_trace

    eq_trace.install()
except Exception as e:  # never break the interpreter
    print(f"eq_trace not installed: {e!r}", file=sys.stderr)
