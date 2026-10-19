"""Factor mining orchestration: the job boundary, its validators, and the run ledger.

No database and no data source runs here. The service's collaborators are injected -- repository,
engine factory, backtest service -- and ``agent_jobs`` is monkeypatched where the job row would
otherwise be written, so what is asserted is what the service *asks for*: which payload reaches
the job store, which count prices the frozen review, and what a failure leaves behind. The one
thing a fake cannot prove -- that PostgreSQL accepts the statements -- belongs to task 12's
integration check.
"""

import copy
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone

import pytest

from app.services.factor_mining import service as mining_service
from app.services.factor_mining.dsl import DSL_VERSION
from app.services.factor_mining.engine import DEFAULT_SEED, MiningConfig, MiningDataProvider
from app.services.factor_mining.gates import GateThresholds
from app.services.factor_mining.service import FactorMiningService
from app.services.factor_mining.templates import DEFAULT_BUDGET, expand_candidates
from app.tasks import agent_jobs as celery_agent_jobs


USER_ID = 4
RUN_ID = 7
_PROGRESS = lambda event: event  # noqa: E731 - a stand-in for the job's progress callback

# Minimal compilable portfolio source: the manifest needs initialize/universe/subscription and
# more than one instrument to compile as a portfolio strategy.
_SOURCE_CODE = """
def initialize(context):
    context.set_universe(["USStock:AAPL", "USStock:MSFT", "USStock:NVDA"])
    context.subscribe(frequency="1d")

def handle_data(context, data):
    pass
"""

_SOURCE = {"id": 3, "name": "US equity portfolio", "code": _SOURCE_CODE}


def _payload(**overrides):
    base = {"sourceId": 3, "market": "USStock", "timeframe": "1d",
            "startDate": "2024-01-01", "endDate": "2024-10-31", "budget": 20}
    base.update(overrides)
    return base


def _trial(fingerprint: str, verdict: str) -> dict:
    return {"fingerprint": fingerprint * 64, "stage": "L4" if verdict == "pass" else "L1",
            "verdict": verdict, "reason": "" if verdict == "pass" else "factorMining.reject.invalid",
            "metrics": {}}


def _run_result(trials: list[dict], candidates: list[dict]) -> dict:
    """The engine's run record: its ``summary.frozen`` is the placeholder the service overwrites."""
    return {
        "config": {"market": "USStock"},
        "split": {"searchEnd": "2024-08-01T00:00:00"},
        "trials": trials,
        "candidates": candidates,
        "summary": {"snapshotId": "b" * 64, "warnings": [], "trials": len(trials),
                    "candidates": len(candidates), "frozen": 0, "executionAssumptions": {}},
    }


class _SourceService:
    """``get_script_source_service()``'s tenant-scoped read, recorded."""

    def __init__(self, source=None):
        self.source = source
        self.calls = []

    def get_source(self, source_id, *, user_id):
        self.calls.append((source_id, user_id))
        return self.source


class _Repository:
    """The ledger writes, in the order the service issued them."""

    def __init__(self):
        self.trace = []
        self.runs = []
        self.trials = []
        self.candidates = []
        self.finished = []
        # The job -> run lookup a replay and the proposals endpoint read, and the recorded
        # proposals a replay copies. Keyed the way the real repository answers them.
        self.runs_by_job = {}
        self.proposals = {}

    def persist_run(self, **kwargs):
        self.trace.append("persist_run")
        self.runs.append(kwargs)
        return RUN_ID

    def save_trials(self, run_id, *, user_id, trials):
        self.trace.append("save_trials")
        self.trials.append({"run_id": run_id, "user_id": user_id, "trials": list(trials)})
        return len(trials)

    def upsert_candidates(self, *, user_id, run_id, candidates):
        self.trace.append("upsert_candidates")
        self.candidates.append(list(candidates))
        return len(candidates)

    def finish_run(self, run_id, *, user_id, status, result, error_message=""):
        self.trace.append("finish_run")
        self.finished.append({"run_id": run_id, "user_id": user_id, "status": status,
                              "result": result, "error_message": error_message})

    def get_run_by_job_id(self, job_id, *, user_id):
        return self.runs_by_job.get(str(job_id))

    def list_proposals(self, run_id, *, user_id, limit=50, offset=0):
        rows = list(self.proposals.get(run_id, []))
        return rows[offset:offset + limit]


class _BacktestService:
    """The data path a provider reads through, recording the calls it receives."""

    def __init__(self, candidates=()):
        self.calls = []
        self.candidates = list(candidates)

    def resolve_candidates(self, *, user_id, manifest, start_date, end_date):
        self.calls.append("resolve_candidates")
        return list(self.candidates), 0


class _Engine:
    """A stand-in for ``FactorMiningEngine`` that records what the search and review were told."""

    def __init__(self, trace, *, run_result=None, reviewed=(), error=None, review_error=None):
        self.trace = trace
        self.run_result = run_result
        self.reviewed = list(reviewed)
        self.error = error
        self.review_error = review_error
        self.trials_counts = []
        self.search_modes = []
        self.survivors = None
        self.configs = []
        self.calls = []

    def run(self, *, user_id, code, config, search_mode=None, ai=None):
        self.trace.append("engine.run")
        self.configs.append(config)
        self.search_modes.append(search_mode)
        self.calls.append({"config": config, "search_mode": search_mode, "ai": ai})
        if self.error is not None:
            raise self.error
        return self.run_result

    def review_frozen(self, *, user_id, code, config, survivors, trials_count, search_mode=None):
        self.trace.append("review_frozen")
        self.search_modes.append(search_mode)
        self.survivors = list(survivors)
        self.trials_counts.append(trials_count)
        if self.review_error is not None:
            raise self.review_error
        return self.reviewed


def _factory(trace, engine):
    """An ``engine_factory`` that hands back ``engine`` and records how it was built."""
    built = {}

    def _build(*, data_provider, on_progress):
        trace.append("engine_factory")
        built.update(data_provider=data_provider, on_progress=on_progress)
        return engine

    return _build, built


def _job_row(**overrides):
    row = {"job_id": "job-1", "kind": "factor_mining", "status": "succeeded",
           "result": {"summary": {"frozen": 1}}, "progress": {"phase": "succeeded"},
           "request": {"sourceId": 7, "market": "USStock", "startDate": "2023-01-02",
                       "endDate": "2023-06-30", "budget": 50, "__userId": 4},
           "error": None, "created_at": datetime(2026, 1, 1), "started_at": None,
           "finished_at": datetime(2026, 1, 1, 0, 5)}
    row.update(overrides)
    return row


class _Jobs:
    """The job store's two submit-side calls, backed by the list the tests read as ``jobs``."""

    def __init__(self):
        self.jobs = []

    def count_active_jobs(self, **_):
        return 0

    def submit_job(self, *, job_id=None, request_payload=None, runner=None, **kwargs):
        self.jobs.append({
            "job_id": job_id, "kind": kwargs.get("kind"), "status": "queued",
            "request": dict(request_payload or {}), "runner": runner, "result": None,
        })
        return {"job_id": job_id, "status": "queued"}


def _service(run_result=None):
    """A service wired to in-memory collaborators for the AI and replay tests.

    ``_restore_module_hooks`` puts back the two module hooks this patches -- the source lookup
    and the job store -- so one helper can cover the submit and the runner in the same test.
    ``proposer_factory`` hands over a proposer that refuses to be called: these tests either
    inject no model call (replay, notes) or assert the wiring without running the stream.
    """
    repository = _Repository()
    engine = _Engine(repository.trace,
                     run_result=_run_result([], []) if run_result is None else run_result)
    build, _ = _factory(repository.trace, engine)
    jobs = _Jobs()
    proposer = lambda system_prompt, user_prompt: pytest.fail("the model must not be called")
    service = FactorMiningService(
        engine_factory=build,
        repository=repository,
        proposer_factory=lambda: proposer,
    )
    service.jobs = jobs.jobs
    service.repository = repository
    service.engine_calls = engine.calls
    mining_service.get_script_source_service = lambda: _SourceService(_SOURCE)
    mining_service.agent_jobs.submit_job = jobs.submit_job
    mining_service.agent_jobs.count_active_jobs = jobs.count_active_jobs
    return service


@pytest.fixture(autouse=True)
def _restore_module_hooks():
    """Undo whatever ``_service`` patched, after every test in this module."""
    source = mining_service.get_script_source_service
    submit_job = mining_service.agent_jobs.submit_job
    count_active_jobs = mining_service.agent_jobs.count_active_jobs
    yield
    mining_service.get_script_source_service = source
    mining_service.agent_jobs.submit_job = submit_job
    mining_service.agent_jobs.count_active_jobs = count_active_jobs


def _original_config():
    """A stored run's config: the snake_case ``asdict`` shape ``persist_run`` writes."""
    return {
        "source_id": 3, "source_name": "US equity portfolio", "market": "USStock",
        "timeframe": "1D", "start_date": "2024-01-01T00:00:00",
        "end_date": "2024-10-31T23:59:59", "freeze_ratio": 0.3, "budget": 20, "seed": 42,
        "template_ids": [], "groups": 5, "holding_period": 5, "commission": 0.0005,
        "slippage": 0.0005, "neutralize_industry": False,
        "thresholds": asdict(GateThresholds()), "search_mode": "ai", "population": 32,
        "generations": 20, "elite_ratio": 0.25, "ai_rounds": 2,
        "ai_proposals_per_round": 10, "replay_job_id": "",
    }


_REPLAY_PROPOSAL = {"expression": {"op": "close"}, "status": "evaluated"}


def _ai_run_result(snapshot_id: str = "b" * 64) -> dict:
    """A run record as the AI engine writes it: ``summary.ai`` present, snapshot settable."""
    return {**_run_result([], []), "summary": {
        **_run_result([], [])["summary"], "snapshotId": snapshot_id,
        "ai": {"enabled": True, "replayOf": "", "snapshotChanged": False},
    }}


def _camel(name: str) -> str:
    """``template_ids`` -> ``templateIds``: the spelling a run payload uses."""
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


# ─────────────────────────────── the job boundary ───────────────────────────────

def test_submit_uses_factor_mining_job_kind(monkeypatch):
    captured = {}

    def _fake_submit_job(**kwargs):
        captured.update(kwargs)
        return {"job_id": "job-1", "status": "queued"}

    monkeypatch.setattr(mining_service.agent_jobs, "submit_job", _fake_submit_job)
    monkeypatch.setattr(mining_service.agent_jobs, "count_active_jobs", lambda **_: 0)

    receipt = FactorMiningService().submit(user_id=4, payload=_payload())

    assert captured["kind"] == "factor_mining"
    assert captured["user_id"] == 4
    assert captured["request_payload"]["__userId"] == 4
    assert receipt["jobId"] == "job-1"


def test_submit_rejects_second_concurrent_job(monkeypatch):
    monkeypatch.setattr(mining_service.agent_jobs, "count_active_jobs", lambda **_: 1)
    with pytest.raises(ValueError, match="factorMining.concurrentLimit"):
        FactorMiningService().submit(user_id=4, payload=_payload())


def test_the_receipt_says_what_was_accepted(monkeypatch):
    """The job store's receipt names the job only, so the config is attached at this boundary.

    Without it a caller cannot tell from the 202 alone whether their own market and window were
    the ones queued, and the field would read as an empty object on every receipt while the same
    job read back by id shows the config -- the same job answering twice.
    """
    monkeypatch.setattr(mining_service.agent_jobs, "count_active_jobs", lambda **_: 0)
    monkeypatch.setattr(mining_service.agent_jobs, "submit_job",
                        lambda **_: {"job_id": "job-1", "status": "queued"})

    receipt = FactorMiningService().submit(user_id=4, payload=_payload())

    assert receipt["config"] == _payload()
    # The injected tenant scope is the job store's own channel and never leaves through it.
    assert "__userId" not in receipt["config"]


def test_submit_validates_before_it_opens_a_job(monkeypatch):
    """A rejected payload must not leave a queued row behind, so nothing may reach ``submit_job``."""
    monkeypatch.setattr(mining_service.agent_jobs, "submit_job",
                        lambda **_: pytest.fail("an invalid payload must not create a job"))
    monkeypatch.setattr(mining_service.agent_jobs, "count_active_jobs", lambda **_: 0)
    with pytest.raises(ValueError, match="factorMining.invalidMarket"):
        FactorMiningService().submit(user_id=4, payload=_payload(market="NASDAQ"))


def test_submit_strips_client_supplied_reserved_keys(monkeypatch):
    """``__`` keys are the job store's own channel -- ``__billing`` is refunded from them."""
    captured = {}

    def _fake_submit_job(**kwargs):
        captured.update(kwargs)
        return {"job_id": "job-1", "status": "queued"}

    monkeypatch.setattr(mining_service.agent_jobs, "submit_job", _fake_submit_job)
    monkeypatch.setattr(mining_service.agent_jobs, "count_active_jobs", lambda **_: 0)

    FactorMiningService().submit(
        user_id=4,
        payload=_payload(**{"__billing": {"credits": 999}, "__userId": 99}),
    )

    assert captured["request_payload"] == {**_payload(), "__userId": 4,
                                           "__jobId": captured["job_id"]}
    assert captured["job_id"]


def test_get_job_speaks_the_camel_case_envelope(monkeypatch):
    monkeypatch.setattr(mining_service.agent_jobs, "get_job",
                        lambda job_id, *, user_id: _job_row(job_id=job_id))
    job = FactorMiningService().get_job("job-1", user_id=USER_ID)
    assert job["jobId"] == "job-1"
    assert job["result"] == {"summary": {"frozen": 1}}
    assert job["hasResult"] is True
    assert job["createdAt"] == datetime(2026, 1, 1)
    assert set(job) == {"jobId", "status", "progress", "config", "result", "hasResult", "error",
                        "createdAt", "startedAt", "finishedAt"}


def test_a_job_carries_the_config_its_caller_submitted(monkeypatch):
    """The list view has no result, so this is what tells a poller which job it is looking at."""
    monkeypatch.setattr(mining_service.agent_jobs, "get_job",
                        lambda job_id, *, user_id: _job_row(job_id=job_id))
    job = FactorMiningService().get_job("job-1", user_id=USER_ID)
    assert job["config"] == {"sourceId": 7, "market": "USStock", "startDate": "2023-01-02",
                             "endDate": "2023-06-30", "budget": 50}


def test_a_job_carries_the_search_mode_it_was_submitted_with(monkeypatch):
    """A listed job withholds ``result``, so ``config`` is the only place its mode is visible.

    ``_RUN_FIELDS`` lets the mode and the breeding pool into the stored request; a read-back that
    dropped them would leave a poller unable to tell an evolving job from an enumerating one --
    the two searches cost different amounts and take different lengths of history.
    """
    monkeypatch.setattr(mining_service.agent_jobs, "get_job", lambda job_id, *, user_id: _job_row(
        job_id=job_id, request={"sourceId": 7, "market": "USStock", "searchMode": "evolution",
                                "population": 40, "generations": 5, "eliteRatio": 0.2}))
    job = FactorMiningService().get_job("job-1", user_id=USER_ID)
    assert job["config"] == {"sourceId": 7, "market": "USStock", "searchMode": "evolution",
                             "population": 40, "generations": 5, "eliteRatio": 0.2}


def test_the_job_config_never_echoes_the_job_stores_own_channel(monkeypatch):
    """``request`` holds ``__userId`` and ``__billing``; neither may reach a response."""
    monkeypatch.setattr(
        mining_service.agent_jobs, "get_job",
        lambda job_id, *, user_id: _job_row(job_id=job_id, request={
            "sourceId": 7, "__userId": 4, "__billing": {"credits": 999},
            "clientKey": "not a run field",
        }),
    )
    job = FactorMiningService().get_job("job-1", user_id=USER_ID)
    assert job["config"] == {"sourceId": 7}


def test_a_job_without_a_stored_request_still_lists(monkeypatch):
    """An older row reads as an empty config rather than an error: it is still worth listing."""
    monkeypatch.setattr(mining_service.agent_jobs, "get_job",
                        lambda job_id, *, user_id: _job_row(job_id=job_id, request=None))
    job = FactorMiningService().get_job("job-1", user_id=USER_ID)
    assert job["config"] == {}



def test_get_job_hides_another_features_job(monkeypatch):
    monkeypatch.setattr(mining_service.agent_jobs, "get_job",
                        lambda job_id, *, user_id: _job_row(kind="strategy_evolution"))
    assert FactorMiningService().get_job("job-1", user_id=USER_ID) is None


def test_list_jobs_asks_for_this_kind_and_maps_every_row(monkeypatch):
    asked = {}

    def _list_jobs(**kwargs):
        asked.update(kwargs)
        return [{"job_id": "job-1"}, {"job_id": "job-2"}]

    monkeypatch.setattr(mining_service.agent_jobs, "list_jobs", _list_jobs)
    monkeypatch.setattr(mining_service.agent_jobs, "get_job",
                        lambda job_id, *, user_id: _job_row(job_id=job_id))

    jobs = FactorMiningService().list_jobs(user_id=USER_ID, limit=5)

    assert asked == {"user_id": USER_ID, "kind": "factor_mining", "limit": 5}
    assert [job["jobId"] for job in jobs] == ["job-1", "job-2"]


def _list_one_running_job(monkeypatch, started_at, failed):
    monkeypatch.setattr(
        mining_service.agent_jobs, "list_jobs",
        lambda **_: [{"job_id": "job-1", "status": "running", "started_at": started_at}],
    )
    monkeypatch.setattr(mining_service.agent_jobs, "get_job",
                        lambda job_id, *, user_id: _job_row(job_id=job_id, status="failed"))
    monkeypatch.setattr(mining_service.agent_jobs, "_set_failure",
                        lambda job_id, message: failed.append((job_id, message)))
    return FactorMiningService().list_jobs(user_id=USER_ID)


def test_list_jobs_retires_a_job_whose_worker_died(monkeypatch):
    """An unswept ghost blocks every later submission: the concurrency gate counts active jobs."""
    failed = []
    stale = datetime.now(timezone.utc) - timedelta(seconds=10_000)

    jobs = _list_one_running_job(monkeypatch, stale, failed)

    assert failed == [("job-1", "factorMining.workerInterrupted")]
    assert jobs[0]["status"] == "failed"


def test_list_jobs_leaves_a_running_job_inside_its_time_limit_alone(monkeypatch):
    """Inside the limit a job may simply be slow, and retiring it would free its slot to a second run."""
    failed = []
    fresh = datetime.now(timezone.utc) - timedelta(seconds=60)

    _list_one_running_job(monkeypatch, fresh, failed)

    assert failed == []


def test_list_jobs_leaves_the_run_record_out_of_every_row(monkeypatch):
    """A listing is not a run record: the result stays behind ``hasResult``.

    A finished run's result carries every surviving candidate's full metrics and every trial's
    ledger row, so a page of twenty of them is more JSON than a poller can use -- it lists jobs
    to find the one that finished.
    """
    monkeypatch.setattr(mining_service.agent_jobs, "list_jobs",
                        lambda **_: [{"job_id": "job-1"}, {"job_id": "job-2"}])
    monkeypatch.setattr(
        mining_service.agent_jobs, "get_job",
        lambda job_id, *, user_id: _job_row(
            job_id=job_id, result={"summary": {"frozen": 1}} if job_id == "job-1" else None),
    )

    jobs = FactorMiningService().list_jobs(user_id=USER_ID)

    assert [job["result"] for job in jobs] == [None, None]
    assert [job["hasResult"] for job in jobs] == [True, False]
    # The detail read is where the record is, unchanged and still flagged.
    detail = FactorMiningService().get_job("job-1", user_id=USER_ID)
    assert detail["result"] == {"summary": {"frozen": 1}}
    assert detail["hasResult"] is True


def test_the_active_job_gate_counts_this_features_jobs_only(monkeypatch):
    """A running backtest is not a mining slot, and a mining job of one's own is.

    The stand-in models the filter the counter's SQL applies, so the subject here is which kind
    the service asks for; ``test_count_active_jobs_narrows_to_a_kind`` pins the statement itself.
    """
    rows = [{"user_id": USER_ID, "kind": "backtest"}]

    def _count_active_jobs(*, user_id, agent_token_id=None, kind=None):
        return sum(1 for row in rows if row["user_id"] == user_id
                   and (kind is None or row["kind"] == kind))

    monkeypatch.setattr(mining_service.agent_jobs, "count_active_jobs", _count_active_jobs)
    monkeypatch.setattr(mining_service.agent_jobs, "submit_job",
                        lambda **_: {"job_id": "job-1", "status": "queued"})

    assert FactorMiningService().submit(user_id=USER_ID, payload=_payload())["jobId"] == "job-1"

    rows.append({"user_id": USER_ID, "kind": "factor_mining"})
    with pytest.raises(ValueError, match="factorMining.concurrentLimit"):
        FactorMiningService().submit(user_id=USER_ID, payload=_payload())

    # The other tenant's job is still nobody's business: the count stays scoped by user.
    rows[:] = [{"user_id": USER_ID + 1, "kind": "factor_mining"}]
    assert FactorMiningService().submit(user_id=USER_ID, payload=_payload())["jobId"] == "job-1"


def test_count_active_jobs_narrows_to_a_kind(monkeypatch):
    """The shared counter's own statement: a ``kind`` narrows it, and its absence does not."""
    from app.utils import agent_jobs as jobs

    executed: list[tuple[str, tuple]] = []

    class _Cursor:
        def execute(self, statement, parameters=()):
            executed.append((" ".join(statement.split()), parameters))

        def fetchone(self):
            return {"count": 0}

        def close(self):
            pass

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(jobs, "get_db_connection", _Connection)

    assert jobs.count_active_jobs(user_id=USER_ID, kind="factor_mining") == 0
    statement, parameters = executed[-1]
    assert "kind = %s" in statement
    assert parameters == (USER_ID, "factor_mining")

    jobs.count_active_jobs(user_id=USER_ID, agent_token_id=9)
    statement, parameters = executed[-1]
    assert "kind" not in statement
    assert "agent_token_id = %s" in statement
    assert parameters == (USER_ID, 9)


def test_cancel_job_cancels_only_the_callers_own_job(monkeypatch):
    cancelled = []
    monkeypatch.setattr(mining_service.agent_jobs, "get_job",
                        lambda job_id, *, user_id: _job_row(job_id=job_id, status="running"))
    monkeypatch.setattr(
        mining_service.agent_jobs, "cancel_job",
        lambda job_id, *, user_id: cancelled.append((job_id, user_id))
        or _job_row(job_id=job_id, status="cancelled"),
    )
    job = FactorMiningService().cancel_job("job-1", user_id=USER_ID)
    assert cancelled == [("job-1", USER_ID)]
    assert job["status"] == "cancelled"


def test_cancel_job_leaves_an_unknown_id_alone(monkeypatch):
    monkeypatch.setattr(mining_service.agent_jobs, "get_job", lambda job_id, *, user_id: None)
    monkeypatch.setattr(mining_service.agent_jobs, "cancel_job",
                        lambda job_id, *, user_id: pytest.fail("nothing to cancel"))
    assert FactorMiningService().cancel_job("job-9", user_id=USER_ID) is None


# ──────────────────────────── the candidate library ────────────────────────────

class _CandidateRepository:
    """The two reads the service forwards to; the run ledger is not needed for them."""

    def __init__(self):
        self.calls = []

    def list_candidates(self, **kwargs):
        self.calls.append(("list_candidates", kwargs))
        return [{"id": 5, "status": "frozen"}]

    def get_candidate(self, candidate_id, *, user_id):
        self.calls.append(("get_candidate", {"candidate_id": candidate_id, "user_id": user_id}))
        return {"id": candidate_id} if candidate_id == 5 else None


def test_candidate_reads_forward_to_the_repository_untouched():
    """Filtering, paging, and the not-found ``None`` are the repository's: forward, add nothing."""
    repository = _CandidateRepository()
    service = FactorMiningService(repository=repository)

    items = service.list_candidates(user_id=USER_ID, run_id=RUN_ID, status="frozen", market="USStock")
    candidate = service.get_candidate(5, user_id=USER_ID)
    missing = service.get_candidate(999, user_id=USER_ID)

    assert repository.calls == [
        ("list_candidates", {"user_id": USER_ID, "run_id": RUN_ID, "status": "frozen",
                             "market": "USStock", "limit": 50, "offset": 0}),
        ("get_candidate", {"candidate_id": 5, "user_id": USER_ID}),
        ("get_candidate", {"candidate_id": 999, "user_id": USER_ID}),
    ]
    assert items == [{"id": 5, "status": "frozen"}]
    assert candidate == {"id": 5}
    assert missing is None


# ────────────────────────────────── validation ──────────────────────────────────

def test_submit_requires_source_and_dates():
    service = FactorMiningService()
    with pytest.raises(ValueError, match="factorMining.sourceRequired"):
        service.estimate(_payload(sourceId=0))
    with pytest.raises(ValueError, match="factorMining.dateRangeRequired"):
        service.estimate(_payload(endDate=""))


@pytest.mark.parametrize("budget", [0, -5])
def test_budget_below_one_is_rejected_by_key(budget):
    with pytest.raises(ValueError, match="factorMining.budgetInvalid"):
        FactorMiningService().estimate(_payload(budget=budget))


def test_blank_integer_fields_read_as_absent(monkeypatch):
    """``""`` is a present value the engine would ``int()``, so the boundary reads it as absent."""
    monkeypatch.setattr(mining_service, "get_script_source_service",
                        lambda: pytest.fail("estimate must not read the source"))
    estimate = FactorMiningService().estimate(
        _payload(budget="", seed="", groups="", holdingPeriod=""),
    )
    assert estimate["candidateBudget"] == len(
        expand_candidates(template_ids=None, budget=DEFAULT_BUDGET, seed=DEFAULT_SEED)
    )
    with pytest.raises(ValueError, match="factorMining.sourceRequired"):
        FactorMiningService().estimate(_payload(sourceId=""))


def test_blank_evolution_fields_read_as_absent_for_enumeration():
    """The breeder's three numbers are optional where they are optional, and refused where not.

    The product contract has the three optional in enumeration mode and bounded whenever they
    are given, and an empty form box serialized as ``""`` is the shape that contract has to
    survive. The boundary reads ``""`` as an absent key, the same rule the integer fields above
    follow, so enumeration prices the search the defaults describe -- and the engine's own
    requirement is untouched: evolution is not a mode a payload defaults into, so a blank there
    is still refused by key rather than priced as the placeholder. Both halves matter: a
    boundary that tolerated the blank while the engine priced it would search a population the
    caller never named.
    """
    payload = _payload(searchMode="enumeration", population="", generations="", eliteRatio="")
    assert FactorMiningService().estimate(payload)["candidateBudget"] == len(
        expand_candidates(template_ids=None, budget=_payload()["budget"], seed=DEFAULT_SEED)
    )
    with pytest.raises(ValueError, match="factorMining.invalidParameter"):
        FactorMiningService().estimate(
            _payload(searchMode="evolution", population="", generations="", eliteRatio=""),
        )


@pytest.mark.parametrize("market", ["", "NASDAQ"])
def test_unknown_market_is_rejected_by_key(market):
    with pytest.raises(ValueError, match="factorMining.invalidMarket"):
        FactorMiningService().estimate(_payload(market=market))


@pytest.mark.parametrize("field", ["seed", "groups", "holdingPeriod", "population", "generations"])
@pytest.mark.parametrize("value", [[], "abc", {}, float("inf")], ids=["list", "text", "object", "infinity"])
def test_an_unreadable_integer_field_is_refused_by_key(field, value):
    """``int([])`` is a ``TypeError`` and ``int("abc")`` a bare ``ValueError``.

    Neither reaches the caller as a key: the route turns the first into a 500 with no key at all
    and hands the second's message -- the internals of ``int()`` -- straight back as ``msg``.
    """
    with pytest.raises(ValueError, match="factorMining.invalidParameter"):
        FactorMiningService().estimate(_payload(**{field: value}))


@pytest.mark.parametrize("value", [[], "abc", float("inf")], ids=["list", "text", "infinity"])
def test_an_unreadable_budget_keeps_its_own_key(value):
    """The generic key must not swallow the field that already owns a specific one."""
    with pytest.raises(ValueError, match="factorMining.budgetInvalid"):
        FactorMiningService().estimate(_payload(budget=value))


def test_an_unreadable_source_id_is_not_a_positive_int():
    """``int(inf)`` raises ``OverflowError``, which is not a ``ValueError``.

    Left uncaught it escapes ``_positive_int`` and the closed-key path with it, so a nine-character
    body would be answered with a 500 instead of the key that names the missing field. (A huge
    *integer* does not belong here: ``int()`` on an existing ``int`` converts nothing, so only a
    float reaching the integer domain overflows.)
    """
    with pytest.raises(ValueError, match="factorMining.sourceRequired"):
        FactorMiningService().estimate(_payload(sourceId=float("inf")))


@pytest.mark.parametrize("timeframe", ["1e400m", "1" + "0" * 309 + "m", "abc", "1d2"],
                         ids=["exponent", "huge-int", "word", "extra-digit"])
def test_a_timeframe_the_readers_cannot_price_is_refused_by_key(timeframe):
    """Two shared readers turn this field into numbers, and neither can price every string.

    ``periods_per_year`` reads the digits with a bare ``int()`` and
    ``backtest_warmup_calendar_days`` multiplies them, so ``"1e400m"`` (six characters) raises --
    per candidate, inside the candidate guard -- and the run is recorded as a *success* whose
    every trial blames the DSL. Refusing the shape at the boundary keeps the blame on the request.
    """
    with pytest.raises(ValueError, match="factorMining.invalidParameter"):
        FactorMiningService().estimate(_payload(timeframe=timeframe))


@pytest.mark.parametrize("timeframe,expected", [("60m", "1H"), ("4h", "4H"), ("1week", "1W"),
                                                ("d", "1D"), ("15m", "15m"), ("1D", "1D")])
def test_the_timeframe_contract_keeps_the_existing_alias_behaviour(timeframe, expected):
    """A shape bound must not quietly rewrite what callers already send."""
    config = MiningConfig.from_payload({**_payload(timeframe=timeframe)},
                                       default_commission=0.0005, default_slippage=0.0005)
    assert config.timeframe == expected


@pytest.mark.parametrize("field,key", [("freezeRatio", "factorMining.freezeRatioInvalid"),
                                       ("commission", "factorMining.invalidParameter"),
                                       ("slippage", "factorMining.invalidParameter"),
                                       ("eliteRatio", "factorMining.invalidParameter")])
def test_an_overflowing_number_is_refused_by_the_fields_own_key(field, key):
    """``float(10**400)`` overflows in three more readers on the payload path.

    A JSON integer of 401 digits is legal input, so this is reachable with a short body; without
    ``OverflowError`` in the handler each of these escapes as a 500 with a key that blames the
    service rather than the request.
    """
    with pytest.raises(ValueError, match=key):
        FactorMiningService().estimate(_payload(**{field: 10 ** 400}))


@pytest.mark.parametrize("thresholds", [[1], "min_icir=0.4", 5], ids=["list", "text", "number"])
def test_thresholds_that_are_not_an_object_are_refused_by_key(thresholds):
    """A list has no ``.get``: reading one is an ``AttributeError``, which is a 500 with no key."""
    with pytest.raises(ValueError, match="factorMining.thresholdsInvalid"):
        FactorMiningService().estimate(_payload(thresholds=thresholds))


@pytest.mark.parametrize("mode", ["genetic", "EVolUTION", False, 0],
                         ids=["unknown", "mis-cased", "boolean", "zero"])
def test_a_search_mode_the_engine_does_not_have_is_refused(mode):
    """A mode is refused, never read as enumeration: the two search different candidate sets.

    Only an absent key takes the default. A value that is merely falsy is not a mode anyone named,
    and reading it as enumeration would run the template search while the run record carried what
    the caller actually sent.
    """
    with pytest.raises(ValueError, match="factorMining.invalidParameter"):
        FactorMiningService().estimate(_payload(searchMode=mode))


@pytest.mark.parametrize("missing", ["population", "generations", "eliteRatio"])
def test_evolution_mode_requires_a_population_size(missing):
    """A run that breeds states its population; evolution is not a mode a payload defaults into.

    The engine's placeholder settings are gone, so the numbers have to come from the run -- and a
    ``population`` this layer invented would be a search the caller never asked for, which is the
    reading the budget cap already takes. Every one of the three is required, not just the size:
    a generation's elite quota is ``int(population * elite_ratio)``, so the share is as much a
    part of "which population" as the count is.
    """
    settings = {"population": 40, "generations": 5, "eliteRatio": 0.2}
    with pytest.raises(ValueError, match="factorMining.invalidParameter"):
        FactorMiningService().estimate(
            _payload(searchMode="evolution", **{k: v for k, v in settings.items() if k != missing}),
        )


@pytest.mark.parametrize("blank", ["population", "generations", "eliteRatio"])
def test_a_blank_breeding_setting_is_refused_like_a_missing_one(blank):
    """A blank field is not a stated value, and all three are read the same way.

    The existence check looked for ``None`` alone while ``eliteRatio``'s own read treated ``""``
    as absent, so a blank share was silently priced as the placeholder 0.25 instead of being
    refused -- a population this layer invented, which is the objection that makes all three
    required. A caller who leaves the field empty has stated no more than one who omits the key.
    """
    settings = {"population": 40, "generations": 5, "eliteRatio": 0.2, blank: ""}
    with pytest.raises(ValueError, match="factorMining.invalidParameter"):
        FactorMiningService().estimate(_payload(searchMode="evolution", **settings))


@pytest.mark.parametrize("overrides", [
    {"population": 0}, {"population": -3}, {"generations": 0}, {"generations": -1},
    {"eliteRatio": 1}, {"eliteRatio": 1.5}, {"eliteRatio": -0.25},
], ids=["population-zero", "population-negative", "generations-zero", "generations-negative",
        "elite-ratio-one", "elite-ratio-above-one", "elite-ratio-negative"])
def test_breeding_settings_out_of_range_are_refused_rather_than_clamped(overrides):
    """``evolution.py`` turns these into slices and loop bounds without checking any of them.

    An ``elite_ratio`` of 1 or more makes every slot of a generation an elite, so the breeding
    loop has no room for children left and the stream ends before it yields anything: the run is
    recorded as a success that searched the template expansion and bred nothing, silently. A
    negative share reads as a slice from the end of the ranking instead. A population or
    generation count below one ends the stream the same way. All of them are refused by key
    here, where a client first reaches them, rather than clamped to a legal range -- the
    budget cap's precedent is that silently searching something else is the worse outcome.
    """
    settings = {**{"population": 40, "generations": 5, "eliteRatio": 0.2}, **overrides}
    with pytest.raises(ValueError, match="factorMining.invalidParameter"):
        FactorMiningService().estimate(_payload(searchMode="evolution", **settings))


def test_estimate_reports_candidate_count_within_budget():
    estimate = FactorMiningService().estimate(_payload(budget=20))
    # The two keys v1 promised, and nothing else: a search mode the client already knows it
    # submitted is not something this body has to echo back.
    assert set(estimate) == {"templateCount", "candidateBudget"}
    assert estimate["candidateBudget"] == 20
    assert estimate["templateCount"] >= 1


def test_an_evolution_payload_is_priced_by_the_same_expansion():
    """``estimate`` counts the template expansion, and every mode seeds the search with it.

    The two numbers are what the caller submits against, and the expansion is the same one in
    either mode -- evolution breeds *from* it rather than replacing it -- so the price does not
    move with the mode.
    """
    estimate = FactorMiningService().estimate(_payload(
        budget=20, searchMode="evolution", population=40, generations=5, eliteRatio=0.2,
    ))
    assert estimate == FactorMiningService().estimate(_payload(budget=20))


def test_estimate_narrows_the_candidate_count_to_a_small_template_set():
    """The count is the expansion's own, not the budget: one template cannot fill a budget of 20."""
    estimate = FactorMiningService().estimate(
        _payload(budget=20, templateIds=["momentum"]),
    )
    assert estimate["templateCount"] == 1
    assert estimate["candidateBudget"] < 20


def test_estimate_counts_each_template_once():
    """``expand_candidates`` dedupes by fingerprint, so a repeated id must not inflate the count."""
    estimate = FactorMiningService().estimate(
        _payload(budget=20, templateIds=["momentum", "momentum"]),
    )
    assert estimate["templateCount"] == 1
    assert estimate["candidateBudget"] == len(
        expand_candidates(template_ids=["momentum"], budget=20, seed=DEFAULT_SEED)
    )


def test_estimate_validates_without_reading_the_source(monkeypatch):
    monkeypatch.setattr(mining_service, "get_script_source_service",
                        lambda: pytest.fail("estimate must not read the source"))
    assert FactorMiningService().estimate(_payload())["candidateBudget"] == 20


# ─────────────────────────────── the run ledger ───────────────────────────────

def test_runner_ledgers_every_trial_and_writes_the_frozen_count_back(monkeypatch):
    """The whole run, in order, with the two counts that only the orchestration can know.

    ``trials_count`` is the deflated-Sharpe denominator, so it must be the *whole* ledger --
    rejected candidates included -- and ``summary.frozen`` is the engine's placeholder until the
    review's statuses are counted here.
    """
    trials = [_trial("a", "reject"), _trial("b", "pass"), _trial("c", "reject")]
    survivor = {"expression": {"op": "close"}, "fingerprint": "b" * 64, "origin": "template",
                "template_id": "momentum", "status": "search", "reason": "", "metrics": {},
                "outOfSample": {}}
    reviewed = [{**survivor, "status": "frozen", "reason": "", "outOfSample": {"rankIc": 0.4}}]
    repository = _Repository()
    run_result = _run_result(trials, [survivor])
    engine_summary = copy.deepcopy(run_result["summary"])
    engine = _Engine(repository.trace, run_result=run_result, reviewed=reviewed)
    build, built = _factory(repository.trace, engine)
    sources = _SourceService(_SOURCE)
    monkeypatch.setattr(mining_service, "get_script_source_service", lambda: sources)

    service = FactorMiningService(engine_factory=build, repository=repository)
    result = service._run_mining_job({"__userId": USER_ID, **_payload(budget=3)}, _PROGRESS)

    assert repository.trace == ["persist_run", "engine_factory", "engine.run", "save_trials",
                                "upsert_candidates", "review_frozen", "upsert_candidates",
                                "finish_run"]
    assert sources.calls == [(3, USER_ID)]
    assert isinstance(built["data_provider"], MiningDataProvider)
    assert built["on_progress"] is _PROGRESS
    assert repository.runs[0]["user_id"] == USER_ID
    assert repository.runs[0]["source_name"] == "US equity portfolio"
    assert repository.runs[0]["config"]["budget"] == 3
    assert repository.runs[0]["config"]["market"] == "USStock"
    assert repository.runs[0]["manifest"]["strategyType"] == "portfolio"
    assert repository.trials[0]["trials"] == trials
    assert repository.candidates[0] == [survivor]
    assert repository.candidates[1] == reviewed
    assert engine.survivors == [survivor]
    # One survivor, three trials: the denominator is the whole ledger, rejects included.
    assert engine.trials_counts == [len(trials)]
    assert repository.finished[0]["status"] == "success"
    assert repository.finished[0]["error_message"] == ""
    assert repository.finished[0]["result"]["candidates"] == reviewed
    # The service writes the review back and nothing else: every other field the engine handed it
    # is persisted unchanged, and only ``summary.frozen`` differs from the engine's placeholder.
    persisted = repository.finished[0]["result"]
    assert persisted["summary"] == {**engine_summary, "frozen": 1}
    assert persisted["trials"] == trials
    assert persisted["split"] == run_result["split"]
    assert result["summary"]["frozen"] == 1


def test_search_mode_defaults_to_enumeration(monkeypatch):
    """A payload that names no mode searches v1's templates, and says so to *both* engine calls.

    The mode is also the warmup rule the provider fetches the panel with, so a default that
    reached only the search would leave the frozen review reaching back the template expansion's
    depth across a tail that evolved survivors -- whose lookback is the DSL's ceiling -- were
    measured over. Nothing raises when that happens: the out-of-sample window is simply shorter
    than the candidates being judged on it.
    """
    repository = _Repository()
    engine = _Engine(repository.trace, run_result=_run_result([], []))
    build, _ = _factory(repository.trace, engine)
    monkeypatch.setattr(mining_service, "get_script_source_service", lambda: _SourceService(_SOURCE))

    service = FactorMiningService(engine_factory=build, repository=repository)
    service._run_mining_job({"__userId": USER_ID, **_payload()}, _PROGRESS)

    assert engine.configs[0].search_mode == "enumeration"
    assert engine.search_modes == ["enumeration", "enumeration"]


def test_the_frozen_review_carries_the_modes_own_warmup_rule(monkeypatch):
    """An evolution run tells the review its mode, not only the search.

    The review is where the cost of getting this wrong lands. The mode is the warmup rule the
    panel is fetched with, and an evolved survivor's lookback is the DSL's ceiling while
    enumeration's is the template expansion's depth -- so a review left on the engine's default
    would fetch the frozen tail shorter than the candidates it judges were measured over, and
    nothing about that raises: the out-of-sample window is simply truncated.
    """
    repository = _Repository()
    engine = _Engine(repository.trace, run_result=_run_result([], []))
    build, _ = _factory(repository.trace, engine)
    monkeypatch.setattr(mining_service, "get_script_source_service", lambda: _SourceService(_SOURCE))

    service = FactorMiningService(engine_factory=build, repository=repository)
    service._run_mining_job(
        {"__userId": USER_ID, **_payload(
            searchMode="evolution", population=40, generations=5, eliteRatio=0.2,
        )},
        _PROGRESS,
    )

    assert engine.configs[0].search_mode == "evolution"
    assert engine.search_modes == ["evolution", "evolution"]


def test_runner_reads_panels_through_the_injected_backtest_service(monkeypatch):
    """The provider is observable only through its data path: reaching the fake proves the wiring.

    ``prepare`` stops at the universe check -- the fake returns no candidates -- so nothing is
    fetched and no real service is ever constructed.
    """
    repository = _Repository()
    engine = _Engine(repository.trace, run_result=_run_result([], []))
    build, built = _factory(repository.trace, engine)
    backtest = _BacktestService()
    monkeypatch.setattr(mining_service, "get_script_source_service", lambda: _SourceService(_SOURCE))

    FactorMiningService(engine_factory=build, repository=repository,
                        backtest_service=backtest)._run_mining_job(
        {"__userId": USER_ID, **_payload()}, _PROGRESS,
    )

    with pytest.raises(ValueError, match="factorMining.universeTooSmall"):
        built["data_provider"].prepare(
            user_id=USER_ID, code=_SOURCE_CODE, config=engine.configs[0],
        )
    assert backtest.calls == ["resolve_candidates"]


def test_runner_closes_the_run_as_failed_then_reraises(monkeypatch):
    repository = _Repository()
    engine = _Engine(repository.trace, error=ValueError("factorMining.universeTooSmall"))
    build, _ = _factory(repository.trace, engine)
    monkeypatch.setattr(mining_service, "get_script_source_service", lambda: _SourceService(_SOURCE))

    service = FactorMiningService(engine_factory=build, repository=repository)
    with pytest.raises(ValueError, match="factorMining.universeTooSmall"):
        service._run_mining_job({"__userId": USER_ID, **_payload()}, _PROGRESS)

    assert repository.trace == ["persist_run", "engine_factory", "engine.run", "finish_run"]
    assert repository.finished[0]["run_id"] == RUN_ID
    assert repository.finished[0]["user_id"] == USER_ID
    assert repository.finished[0]["status"] == "failed"
    assert repository.finished[0]["error_message"] == "factorMining.universeTooSmall"
    assert repository.finished[0]["result"] == {}


def test_runner_refuses_a_missing_source_before_opening_a_run(monkeypatch):
    repository = _Repository()
    monkeypatch.setattr(mining_service, "get_script_source_service",
                        lambda: _SourceService(None))
    service = FactorMiningService(repository=repository)

    with pytest.raises(ValueError, match="factorMining.sourceNotFound"):
        service._run_mining_job({"__userId": USER_ID, **_payload()}, _PROGRESS)

    assert repository.trace == []


def test_runner_keeps_the_search_counts_when_the_review_fails(monkeypatch):
    """A review that fails after the ledger was written must not persist zero counts against it."""
    trials = [_trial("a", "reject"), _trial("b", "pass")]
    survivor = {"expression": {"op": "close"}, "fingerprint": "b" * 64, "origin": "template",
                "template_id": "momentum", "status": "search", "reason": "", "metrics": {},
                "outOfSample": {}}
    repository = _Repository()
    engine = _Engine(repository.trace, run_result=_run_result(trials, [survivor]),
                     review_error=ValueError("factorMining.noMarketData"))
    build, _ = _factory(repository.trace, engine)
    monkeypatch.setattr(mining_service, "get_script_source_service", lambda: _SourceService(_SOURCE))

    service = FactorMiningService(engine_factory=build, repository=repository)
    with pytest.raises(ValueError, match="factorMining.noMarketData"):
        service._run_mining_job({"__userId": USER_ID, **_payload()}, _PROGRESS)

    finished = repository.finished[0]
    assert finished["status"] == "failed"
    assert finished["error_message"] == "factorMining.noMarketData"
    assert finished["result"]["summary"]["trials"] == len(trials)
    assert finished["result"]["summary"]["snapshotId"] == "b" * 64


def test_runner_marks_a_cancelled_run_as_cancelled(monkeypatch):
    """``agent_jobs`` distinguishes a user's cancellation from a failure, and the run row must too."""
    repository = _Repository()
    engine = _Engine(repository.trace,
                     error=mining_service.agent_jobs.JobCancelledError("JOB_CANCELLED"))
    build, _ = _factory(repository.trace, engine)
    monkeypatch.setattr(mining_service, "get_script_source_service", lambda: _SourceService(_SOURCE))

    service = FactorMiningService(engine_factory=build, repository=repository)
    with pytest.raises(mining_service.agent_jobs.JobCancelledError):
        service._run_mining_job({"__userId": USER_ID, **_payload()}, _PROGRESS)

    assert repository.trace == ["persist_run", "engine_factory", "engine.run", "finish_run"]
    assert repository.finished[0]["status"] == "cancelled"
    assert repository.finished[0]["error_message"] == ""


# ─────────────────────────────── celery dispatch ───────────────────────────────

def test_celery_dispatches_factor_mining_to_the_service_runner(monkeypatch):
    """An unregistered kind degrades to the in-process pool, where a restart loses the job."""
    assert celery_agent_jobs.supports_kind("factor_mining") is True

    captured = {}

    def runner(payload, on_progress):
        captured.update(payload=payload, on_progress=on_progress)
        return {"summary": {"frozen": 1}}

    monkeypatch.setattr(mining_service, "run_mining_job", runner)
    progress = lambda event: event

    result = celery_agent_jobs._execute("factor_mining", {"__userId": 17, "sourceId": 9}, progress)

    assert result == {"summary": {"frozen": 1}}
    assert captured == {"payload": {"__userId": 17, "sourceId": 9}, "on_progress": progress}


def test_run_mining_job_shell_delegates_to_the_process_singleton(monkeypatch):
    """The durable entry point is a shell: the orchestration stays on the injected service."""
    captured = {}

    class _Service:
        def _run_mining_job(self, payload, on_progress):
            captured.update(payload=payload, on_progress=on_progress)
            return {"ok": True}

    monkeypatch.setattr(mining_service, "get_factor_mining_service", lambda: _Service())

    assert mining_service.run_mining_job({"__userId": 17}, None) == {"ok": True}
    assert captured == {"payload": {"__userId": 17}, "on_progress": None}


# ──────────────────────────── AI proposals and replay ────────────────────────────

def test_ai_fields_are_refused_outside_ai_mode():
    service = _service()
    with pytest.raises(ValueError, match="factorMining.invalidParameter"):
        service.submit(user_id=4, payload=_payload(searchMode="enumeration", aiRounds=2))


def test_research_notes_are_capped_and_not_in_the_public_config():
    service = _service()
    with pytest.raises(ValueError, match="factorMining.invalidParameter"):
        service.submit(user_id=4, payload=_payload(searchMode="ai", researchNotes="x" * 8001))
    receipt = service.submit(user_id=4, payload=_payload(
        searchMode="ai", researchNotes="secret note"))
    assert "researchNotes" not in receipt["config"]
    assert service.jobs[-1]["request"]["researchNotes"] == "secret note"


def test_ai_run_passes_run_id_and_notes_to_the_engine():
    service = _service()
    service.submit(user_id=4, payload=_payload(searchMode="ai", researchNotes="note"))
    service.jobs[-1]["runner"](service.jobs[-1]["request"], None)
    assert service.engine_calls[-1]["ai"].run_id == RUN_ID
    assert service.engine_calls[-1]["ai"].research_notes == "note"


def test_replay_uses_the_original_config_and_proposals():
    service = _service()
    service.repository.runs_by_job["orig"] = {"id": 9, "config": _original_config(),
                                              "sourceId": 3, "snapshotId": "b" * 64,
                                              "status": "success"}
    service.repository.proposals[9] = [{"expression": {"op": "close"}, "status": "evaluated"}]
    service.jobs.append({"job_id": "orig", "kind": "factor_mining", "status": "succeeded",
                         "result": {"runId": 9}})
    service.submit(user_id=4, payload={"searchMode": "ai", "replayJobId": "orig"})
    result = service.jobs[-1]["runner"](service.jobs[-1]["request"], None)
    ai = service.engine_calls[-1]["ai"]
    assert ai.replay_proposals == tuple(service.repository.proposals[9])
    assert service.engine_calls[-1]["config"].ai_rounds == 2
    # The replay opens its own run under its own job id: the proposal rows are keyed by
    # ``(run_id, round, ordinal)``, so reusing the original run id would collide with them.
    assert service.jobs[-1]["job_id"] != "orig"
    assert service.repository.runs[0]["job_id"] == service.jobs[-1]["job_id"]
    assert result["runId"] == RUN_ID


def test_replay_of_an_unknown_job_is_refused_by_key():
    service = _service()
    with pytest.raises(ValueError, match="factorMining.aiReplayNotFound"):
        service.submit(user_id=4, payload={"searchMode": "ai", "replayJobId": "missing"})


def test_replay_refuses_a_field_that_disagrees_with_the_record():
    """A client value equal to the record is accepted; a different one is not silently replaced."""
    service = _service()
    service.repository.runs_by_job["orig"] = {"id": 9, "config": _original_config(),
                                              "status": "success"}
    service.repository.proposals[9] = [dict(_REPLAY_PROPOSAL)]
    service.submit(user_id=4, payload={"searchMode": "ai", "replayJobId": "orig", "budget": 20})
    assert service.jobs[-1]["request"]["budget"] == 20
    with pytest.raises(ValueError, match="factorMining.aiReplayIncompatible"):
        service.submit(user_id=4, payload={"searchMode": "ai", "replayJobId": "orig",
                                           "budget": 99})


def test_replay_compares_the_effective_thresholds_not_the_spelling():
    """A partial override equal to the record's bars is the same run, so the replay is legal."""
    service = _service()
    service.repository.runs_by_job["orig"] = {"id": 9, "config": _original_config(),
                                              "status": "success"}
    service.repository.proposals[9] = [dict(_REPLAY_PROPOSAL)]
    original = GateThresholds.from_payload(_original_config()["thresholds"])
    service.submit(user_id=4, payload={
        "searchMode": "ai", "replayJobId": "orig",
        "thresholds": {"min_icir": original.min_icir},
    })
    # The record still wins the stored request: the client's spelling does not replace it.
    assert service.jobs[-1]["request"]["thresholds"] == _original_config()["thresholds"]


def test_replay_refuses_thresholds_that_change_the_effective_bars():
    service = _service()
    service.repository.runs_by_job["orig"] = {"id": 9, "config": _original_config(),
                                              "status": "success"}
    service.repository.proposals[9] = [dict(_REPLAY_PROPOSAL)]
    original = GateThresholds.from_payload(_original_config()["thresholds"])
    with pytest.raises(ValueError, match="factorMining.aiReplayIncompatible"):
        service.submit(user_id=4, payload={
            "searchMode": "ai", "replayJobId": "orig",
            "thresholds": {"min_icir": original.min_icir + 0.1},
        })


def test_replay_takes_uncompared_fields_from_the_record():
    """Only the six run-shape fields are compared; anything else the record owns silently."""
    service = _service()
    service.repository.runs_by_job["orig"] = {"id": 9, "config": _original_config(),
                                              "status": "success"}
    service.repository.proposals[9] = [dict(_REPLAY_PROPOSAL)]
    service.submit(user_id=4, payload={"searchMode": "ai", "replayJobId": "orig",
                                       "seed": 999, "groups": 3})
    assert service.jobs[-1]["request"]["seed"] == 42
    assert service.jobs[-1]["request"]["groups"] == 5


def test_replay_of_a_failed_ai_run_uses_the_proposals_it_recorded():
    """A failed run kept its proposals but never wrote trials; replay re-evaluates them."""
    service = _service()
    service.repository.runs_by_job["orig"] = {"id": 9, "config": _original_config(),
                                              "status": "failed"}
    service.repository.proposals[9] = [dict(_REPLAY_PROPOSAL)]
    service.submit(user_id=4, payload={"searchMode": "ai", "replayJobId": "orig"})
    service.jobs[-1]["runner"](service.jobs[-1]["request"], None)
    assert service.engine_calls[-1]["ai"].replay_proposals == (
        service.repository.proposals[9][0],)


@pytest.mark.parametrize("status", ["cancelled", "running"])
def test_replay_refuses_a_run_that_never_finished(status):
    service = _service()
    service.repository.runs_by_job["orig"] = {"id": 9, "config": _original_config(),
                                              "status": status}
    service.repository.proposals[9] = [dict(_REPLAY_PROPOSAL)]
    with pytest.raises(ValueError, match="factorMining.aiReplayIncompatible"):
        service.submit(user_id=4, payload={"searchMode": "ai", "replayJobId": "orig"})


@pytest.mark.parametrize("status", ["failed", "success"])
def test_replay_refuses_a_run_that_recorded_no_proposals(status):
    """Without proposal rows there is nothing to copy, whatever the run reached."""
    service = _service()
    service.repository.runs_by_job["orig"] = {"id": 9, "config": _original_config(),
                                              "status": status}
    with pytest.raises(ValueError, match="factorMining.aiReplayIncompatible"):
        service.submit(user_id=4, payload={"searchMode": "ai", "replayJobId": "orig"})


def test_replay_refuses_a_run_that_was_not_an_ai_run():
    """A replay reproduces model proposals; another mode recorded none to reproduce."""
    service = _service()
    config = _original_config()
    config["search_mode"] = "enumeration"
    service.repository.runs_by_job["orig"] = {"id": 9, "config": config, "status": "success"}
    service.repository.proposals[9] = [dict(_REPLAY_PROPOSAL)]
    with pytest.raises(ValueError, match="factorMining.aiReplayIncompatible"):
        service.submit(user_id=4, payload={"searchMode": "ai", "replayJobId": "orig"})


def test_replay_refuses_proposals_the_current_dsl_cannot_read():
    service = _service()
    service.repository.runs_by_job["orig"] = {"id": 9, "config": _original_config(),
                                              "status": "success"}
    service.repository.proposals[9] = [
        {"expression": {"op": "close"}, "dslVersion": DSL_VERSION + 1},
    ]
    with pytest.raises(ValueError, match="factorMining.aiReplayIncompatible"):
        service.submit(user_id=4, payload={"searchMode": "ai", "replayJobId": "orig"})


def test_an_ai_run_records_that_it_replayed_nothing():
    service = _service(run_result=_ai_run_result())
    service.submit(user_id=4, payload=_payload(searchMode="ai"))
    result = service.jobs[-1]["runner"](service.jobs[-1]["request"], None)
    assert result["summary"]["ai"]["replayOf"] == ""
    assert result["summary"]["ai"]["snapshotChanged"] is False


@pytest.mark.parametrize("snapshot_id,changed", [("b" * 64, False), ("c" * 64, True)],
                         ids=["same-data", "moved-data"])
def test_a_replay_records_the_run_it_copied_and_the_snapshot_drift(snapshot_id, changed):
    service = _service(run_result=_ai_run_result(snapshot_id))
    service.repository.runs_by_job["orig"] = {"id": 9, "config": _original_config(),
                                              "snapshotId": "b" * 64, "status": "success"}
    service.repository.proposals[9] = [dict(_REPLAY_PROPOSAL)]
    service.submit(user_id=4, payload={"searchMode": "ai", "replayJobId": "orig"})
    result = service.jobs[-1]["runner"](service.jobs[-1]["request"], None)
    assert result["summary"]["ai"]["replayOf"] == "orig"
    assert result["summary"]["ai"]["snapshotChanged"] is changed


def test_a_replay_of_a_run_without_a_snapshot_reports_no_drift():
    """A record with no fingerprint cannot say the data moved; comparing to '' would."""
    service = _service(run_result=_ai_run_result("b" * 64))
    service.repository.runs_by_job["orig"] = {"id": 9, "config": _original_config(),
                                              "status": "success"}
    service.repository.proposals[9] = [dict(_REPLAY_PROPOSAL)]
    service.submit(user_id=4, payload={"searchMode": "ai", "replayJobId": "orig"})
    result = service.jobs[-1]["runner"](service.jobs[-1]["request"], None)
    assert result["summary"]["ai"]["replayOf"] == "orig"
    assert result["summary"]["ai"]["snapshotChanged"] is False


def test_the_replay_payload_covers_every_field_a_recorded_config_carries():
    """The hand-written replay list must not fall behind ``MiningConfig``.

    ``searchMode`` and ``replayJobId`` are the replay's own; the evolution settings are the one
    explicit exclusion, because a replay is an AI run and never breeds. Everything else a stored
    config holds has to travel back into the replayed submission.
    """
    evolution_only = {"population", "generations", "eliteRatio"}
    config_fields = {_camel(field.name) for field in fields(MiningConfig)}
    assert set(mining_service._REPLAY_FIELDS) == config_fields - evolution_only - {
        "searchMode", "replayJobId",
    }
    assert set(mining_service._REPLAY_CONFLICT_FIELDS) == {
        "market", "timeframe", "startDate", "endDate", "budget", "thresholds",
    }
