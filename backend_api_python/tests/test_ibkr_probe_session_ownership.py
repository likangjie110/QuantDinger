"""The IBKR session-ownership probe must stay runnable without a gateway.

Only the offline ``--selftest`` path is exercised here. The live probe deliberately talks to a
TWS/IB Gateway, so the suite must never reach it — pointing it at a developer machine that
happens to run a terminal would connect for real.
"""

import json
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND_DIR / "scripts" / "ibkr_probe_session_ownership.py"


def test_selftest_reports_a_process_private_registry():
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--selftest"],
        capture_output=True,
        text=True,
        cwd=str(BACKEND_DIR),
        timeout=180,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["process_private"] is True
    # The child registered one session; the parent process cannot see it.
    assert report["child_entries"] == 1
    assert report["parent_entries"] == 0


def test_live_probe_requires_a_target():
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(BACKEND_DIR),
        timeout=60,
        check=False,
    )

    assert completed.returncode != 0
    assert "--host and --port are required" in (completed.stderr + completed.stdout)
