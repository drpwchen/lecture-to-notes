"""Shared test setup: make `scripts/` importable the way the scripts import each
other (they are run as plain files, not as a package)."""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
FIXTURES = os.path.join(REPO, "tests", "fixtures")

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
