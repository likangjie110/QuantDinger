"""The failure reason a stored job hands back.

``_set_failure`` writes the exception plus ``traceback.format_exc()[-2000:]``, so the column
carries the worker's absolute paths and module layout. Every job kind -- backtest, strategy
evolution, factor mining -- reads its row through ``agent_jobs.get_job``, so the guarantee that
only the reason leaves belongs there rather than at any one feature's boundary. These tests pin it
at that owner.

The DB is stubbed rather than hit: this is about what the lookup emits, not about SQL.
"""
from __future__ import annotations

from contextlib import contextmanager

from app.utils import agent_jobs


class _Cursor:
    def __init__(self, row):
        self._row = row

    def execute(self, *args, **kwargs):
        pass

    def fetchone(self):
        return self._row

    def close(self):
        pass


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _stub_row(monkeypatch, row):
    connection = _Connection(_Cursor(row))

    @contextmanager
    def _connect():
        yield connection

    monkeypatch.setattr(agent_jobs, "get_db_connection", _connect)


# What the column actually holds after a failed run, path layout included.
STORED_STACK = (
    "factorMining.sourceNotFound\n"
    "Traceback (most recent call last):\n"
    r'  File "G:\project\QuantDinger\backend_api_python\app\utils\agent_jobs.py", line 173, in _run'
    "\n    result = runner(request_payload, _on_progress)\n"
    "ValueError: factorMining.sourceNotFound"
)


def test_the_stored_stack_does_not_leave_with_the_job(monkeypatch):
    _stub_row(monkeypatch, {"job_id": "job-1", "status": "failed", "request": {},
                            "error": STORED_STACK})

    row = agent_jobs.get_job("job-1", user_id=4)

    assert row["error"] == "factorMining.sourceNotFound"
    assert "Traceback" not in row["error"]
    assert "QuantDinger" not in row["error"]


def test_an_error_that_never_had_a_stack_is_returned_as_it_stands(monkeypatch):
    # The dispatch and expiry paths write bare codes rather than an exception plus a stack.
    _stub_row(monkeypatch, {"job_id": "job-2", "status": "failed", "request": {},
                            "error": "AGENT_JOB_DISPATCH_FAILED"})

    assert agent_jobs.get_job("job-2", user_id=4)["error"] == "AGENT_JOB_DISPATCH_FAILED"


def test_a_job_that_never_failed_keeps_the_error_it_was_written_with(monkeypatch):
    # Not normalised to one falsy value or the other: the column distinguishes them and so does
    # the reader.
    _stub_row(monkeypatch, {"job_id": "job-3", "status": "succeeded", "request": {}, "error": None})
    assert agent_jobs.get_job("job-3", user_id=4)["error"] is None

    _stub_row(monkeypatch, {"job_id": "job-4", "status": "queued", "request": {}, "error": ""})
    assert agent_jobs.get_job("job-4", user_id=4)["error"] == ""


def test_a_job_that_is_not_there_still_reads_as_absent(monkeypatch):
    # The sanitising step is guarded on the row existing; a miss must not become an error.
    _stub_row(monkeypatch, None)
    assert agent_jobs.get_job("job-404", user_id=4) is None
