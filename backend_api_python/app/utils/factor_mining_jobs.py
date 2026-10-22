"""Fenced, renewable ownership for finite factor-mining attempts.

PostgreSQL owns execution identity. Computation holds no transaction; every durable
write must lock and validate the same job row in its own transaction. Successful
publication belongs to the repository transaction, never to the outer runner.
"""

from __future__ import annotations

import copy
import json
import math
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from app.utils.db import get_db_connection


JOB_KIND = "factor_mining"
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


class JobExecutionLost(RuntimeError):
    """This attempt no longer has permission to mutate the job or its artifacts."""


class JobLeaseBusy(RuntimeError):
    """A redelivery must wait for the currently owned attempt."""

    def __init__(self, retry_after: int):
        super().__init__("FACTOR_MINING_LEASE_BUSY")
        self.retry_after = max(1, min(60, int(retry_after)))


def _lease_seconds() -> int:
    try:
        return max(30, int(os.getenv("FACTOR_MINING_JOB_LEASE_SEC", "120")))
    except (TypeError, ValueError):
        return 120


def _max_attempts() -> int:
    try:
        value = int(os.getenv("FACTOR_MINING_JOB_MAX_ATTEMPTS", "3"))
        return value if 1 <= value <= 100 else 3
    except (TypeError, ValueError):
        return 3


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _db_now(cur) -> datetime:
    # NOW() is the transaction start, which may precede a long wait for the job lock.
    cur.execute("SELECT (clock_timestamp() AT TIME ZONE 'UTC') AS db_now")
    return _utc(cur.fetchone()["db_now"])


def _locked_job(cur, job_id: str) -> dict | None:
    cur.execute(
        """SELECT job_id, user_id, kind, status, request, execution_token,
                  lease_expires_at, heartbeat_at, attempts
           FROM qd_agent_jobs WHERE job_id = %s FOR UPDATE""",
        (str(job_id),),
    )
    return cur.fetchone()


def close_running_runs(cur, *, job_id: str, user_id: int, status: str, error: str = "") -> None:
    """Close audit attempts while the caller holds their owning job row lock."""
    if status not in {"failed", "cancelled"}:
        raise ValueError("Invalid mining attempt terminal status")
    cur.execute(
        """UPDATE qd_factor_mining_runs SET status = %s, error_message = %s
           WHERE job_id = %s AND user_id = %s AND status = 'running'""",
        (status, str(error)[:6000], str(job_id), int(user_id)),
    )


def claim_job(job_id: str) -> dict | None:
    """Claim queued work or replace an expired owner, preserving previous audit rows."""
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            row = _locked_job(cur, job_id)
            if not row or row["kind"] != JOB_KIND:
                raise ValueError("FACTOR_MINING_JOB_NOT_FOUND")
            if row["status"] in _TERMINAL:
                db.commit()
                return None
            now = _db_now(cur)
            expires = row.get("lease_expires_at")
            if row["status"] == "running" and expires and _utc(expires) > now:
                raise JobLeaseBusy(math.ceil((_utc(expires) - now).total_seconds()))
            if row["status"] not in {"queued", "running"}:
                raise JobExecutionLost("FACTOR_MINING_EXECUTION_LOST")
            close_running_runs(cur, job_id=job_id, user_id=int(row["user_id"]),
                               status="failed", error="factorMining.workerInterrupted")
            if int(row.get("attempts") or 0) >= _max_attempts():
                cur.execute(
                    """UPDATE qd_agent_jobs SET status = %s, result = %s::jsonb, error = %s,
                           progress = %s::jsonb, finished_at = NOW(), lease_expires_at = NULL
                       WHERE job_id = %s AND execution_token = %s""",
                    ("failed", "null", "factorMining.workerInterrupted", '{"phase":"failed"}',
                     str(job_id), row.get("execution_token") or ""),
                )
                db.commit()
                return None
            token = uuid.uuid4().hex
            cur.execute(
                """UPDATE qd_agent_jobs SET status = 'running', execution_token = %s,
                       lease_expires_at = (clock_timestamp() AT TIME ZONE 'UTC')
                           + (%s * INTERVAL '1 second'),
                       heartbeat_at = (clock_timestamp() AT TIME ZONE 'UTC'),
                       started_at = (clock_timestamp() AT TIME ZONE 'UTC'),
                       attempts = COALESCE(attempts, 0) + 1, finished_at = NULL,
                       result = NULL, error = NULL, progress = '{"phase":"running"}'::jsonb
                   WHERE job_id = %s
                   RETURNING job_id, user_id, request, attempts""",
                (token, _lease_seconds(), str(job_id)),
            )
            claimed = dict(cur.fetchone())
            request = claimed.get("request") or {}
            claimed["request"] = json.loads(request) if isinstance(request, str) else dict(request)
            claimed["token"] = token
            db.commit()
            return claimed
        except Exception:
            db.rollback()
            raise
        finally:
            cur.close()


def assert_execution(cur, job_id: str, user_id: int, token: str) -> dict:
    """Lock and fence a write in its caller's transaction; never commit it here."""
    from app.utils.agent_jobs import JobCancelledError

    row = _locked_job(cur, job_id)
    if (not row or row["kind"] != JOB_KIND or int(row["user_id"]) != int(user_id)
            or not token or row.get("execution_token") != token):
        raise JobExecutionLost("FACTOR_MINING_EXECUTION_LOST")
    if row["status"] == "cancelled":
        raise JobCancelledError("JOB_CANCELLED")
    if row["status"] != "running":
        raise JobExecutionLost("FACTOR_MINING_EXECUTION_LOST")
    expires = row.get("lease_expires_at")
    if expires is None or _utc(expires) <= _db_now(cur):
        raise JobExecutionLost("FACTOR_MINING_LEASE_EXPIRED")
    return row


def renew_lease(job_id: str, *, user_id: int, token: str) -> bool:
    """Extend an unexpired matching lease without reviving a fenced attempt."""
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            assert_execution(cur, job_id=job_id, user_id=user_id, token=token)
            cur.execute(
                """UPDATE qd_agent_jobs SET lease_expires_at =
                       (clock_timestamp() AT TIME ZONE 'UTC') + (%s * INTERVAL '1 second'),
                       heartbeat_at = (clock_timestamp() AT TIME ZONE 'UTC')
                   WHERE job_id = %s AND user_id = %s AND execution_token = %s
                     AND kind = 'factor_mining' AND status = 'running'
                     AND lease_expires_at > (clock_timestamp() AT TIME ZONE 'UTC')""",
                (_lease_seconds(), str(job_id), int(user_id), str(token)),
            )
            if cur.rowcount != 1:
                raise JobExecutionLost("FACTOR_MINING_LEASE_EXPIRED")
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise
        finally:
            cur.close()


def finish_job(job_id: str, *, user_id: int, token: str, status: str = "failed", error: str = "") -> bool:
    """Close only this live attempt; repository publication owns the success path."""
    from app.utils.agent_jobs import JobCancelledError

    if status not in {"failed", "cancelled"}:
        raise ValueError("Mining success requires atomic repository publication")
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            assert_execution(cur, job_id=job_id, user_id=user_id, token=token)
            close_running_runs(cur, job_id=job_id, user_id=user_id, status=status, error=error)
            cur.execute(
                """UPDATE qd_agent_jobs SET status = %s, result = %s::jsonb, error = %s,
                       progress = %s::jsonb, finished_at = NOW(), lease_expires_at = NULL
                   WHERE job_id = %s AND execution_token = %s""",
                (status, "null", str(error)[:6000] or None, json.dumps({"phase": status}),
                 str(job_id), str(token)),
            )
            db.commit()
            return True
        except (JobCancelledError, JobExecutionLost):
            db.rollback()
            return False
        except Exception:
            db.rollback()
            raise
        finally:
            cur.close()


def publish_progress(job_id: str, *, user_id: int, token: str, event: Any) -> None:
    """Persist progress under ownership, using the existing SSE database polling path."""
    from app.utils import agent_jobs

    snapshot = event if isinstance(event, dict) else {"value": event}
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            assert_execution(cur, job_id=job_id, user_id=user_id, token=token)
            cur.execute("UPDATE qd_agent_jobs SET progress = %s::jsonb WHERE job_id = %s",
                        (json.dumps(snapshot, default=str), str(job_id)))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            cur.close()
    agent_jobs._job_signal(job_id).set()


class ExecutionLease:
    """Keep a single task's lease alive independently of candidate or model progress."""

    def __init__(self, *, job_id: str, user_id: int, token: str):
        self.job_id, self.user_id, self.token = job_id, user_id, token
        self.interval = max(1.0, min(30.0, _lease_seconds() / 3))
        self._stop = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="mining-lease", daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                renew_lease(self.job_id, user_id=self.user_id, token=self.token)
            except Exception as exc:
                self._failure = exc
                return

    def check(self) -> None:
        from app.utils.agent_jobs import JobCancelledError

        if isinstance(self._failure, (JobCancelledError, JobExecutionLost)):
            raise self._failure
        if self._failure is not None:
            raise JobExecutionLost("FACTOR_MINING_HEARTBEAT_LOST") from self._failure

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)


@contextmanager
def execution_scope(*, job_id: str, user_id: int, token: str) -> Iterator[ExecutionLease]:
    lease = ExecutionLease(job_id=job_id, user_id=user_id, token=token)
    lease.start()
    try:
        yield lease
    finally:
        lease.close()


def run_job(job_id: str, runner: Callable[..., Any]) -> Any:
    """Shared Celery/local runner, with authoritative identity and fenced failure cleanup."""
    from app.utils import agent_jobs

    claim = claim_job(job_id)
    if claim is None:
        return None
    user_id, token = int(claim["user_id"]), claim["token"]
    try:
        with execution_scope(job_id=job_id, user_id=user_id, token=token) as lease:
            def progress(event):
                lease.check()
                publish_progress(job_id, user_id=user_id, token=token, event=event)

            payload = copy.deepcopy(claim["request"])
            payload.update(__userId=user_id, __jobId=job_id, __executionToken=token)
            progress({"phase": "running"})
            result = runner(payload, progress) if agent_jobs._runner_accepts_progress(runner) else runner(payload)
            row = agent_jobs.get_job_for_worker(job_id)
            if not row or row.get("execution_token") != token:
                raise JobExecutionLost("FACTOR_MINING_EXECUTION_LOST")
            if row["status"] in _TERMINAL:
                agent_jobs._job_signal(job_id).set()
                return result if row["status"] == "succeeded" else None
            lease.check()
            raise RuntimeError("FACTOR_MINING_RESULT_NOT_PUBLISHED")
    except agent_jobs.JobCancelledError:
        finish_job(job_id, user_id=user_id, token=token, status="cancelled")
        agent_jobs._job_signal(job_id).set()
        return None
    except JobExecutionLost:
        return None
    except Exception as exc:
        finish_job(job_id, user_id=user_id, token=token, error=str(exc))
        agent_jobs._job_signal(job_id).set()
        raise


def recover_jobs(*, limit: int = 100, user_id: int | None = None) -> int:
    """Bounded redispatch from existing maintenance; claiming remains the task's job."""
    celery_enabled = os.getenv("CELERY_TASKS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    tenant_clause = " AND user_id = %s" if user_id is not None else ""
    parameters = (60, *((int(user_id),) if user_id is not None else ()), max(1, min(1000, int(limit))))
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                f"""SELECT job_id FROM qd_agent_jobs WHERE kind = 'factor_mining'
                   AND ((status = 'queued' AND created_at < NOW() - (%s * INTERVAL '1 second'))
                     OR (status = 'running' AND (lease_expires_at IS NULL
                         OR lease_expires_at <= (clock_timestamp() AT TIME ZONE 'UTC'))))
                   {tenant_clause} ORDER BY created_at LIMIT %s""",
                parameters,
            )
            rows = cur.fetchall()
        finally:
            cur.close()
    dispatched = 0
    for row in rows:
        try:
            if celery_enabled:
                from app.tasks.agent_jobs import execute_agent_job

                execute_agent_job.apply_async(args=[row["job_id"]], queue="mining")
            else:
                from app.services.factor_mining.service import run_mining_job
                from app.utils.agent_jobs import _get_executor

                _get_executor().submit(run_job, row["job_id"], run_mining_job)
            dispatched += 1
        except Exception:
            # Leave durable state eligible for the next existing maintenance dispatch.
            continue
    return dispatched
