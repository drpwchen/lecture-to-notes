"""Run scripts/test_ct2_teardown.py (unittest, fakes only, no GPU) under pytest so CI exercises it."""
import os
import subprocess
import sys


def test_ct2_teardown_suite():
    script = os.path.join(os.path.dirname(__file__), "..", "scripts", "test_ct2_teardown.py")
    r = subprocess.run([sys.executable, script], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
