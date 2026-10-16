"""Factor mining HTTP layer: envelopes, the request-key whitelist, and the two lookups.

The service is a double here on purpose -- what a payload *means* is task 10's suite. What this
file pins is the facade's own work: which request keys reach the service, which status a
rejection or a missing row turns into, and that a stored timestamp leaves as ISO-8601 rather
than the RFC-822 ``jsonify`` renders for a native ``datetime``.
"""

import dataclasses
import inspect
from datetime import datetime, timezone

import pytest
from flask import Flask, g

from app.routes import factor_mining as mining_routes
from app.services.factor_mining.engine import MiningConfig


USER_ID = 23


class _StubService:
    """A service double. Every call is recorded under its method name; a canned exception raises."""

    def __init__(self, **responses):
        self.responses = responses
        self.calls: dict[str, dict] = {}

    def _reply(self, name: str, **kwargs):
        self.calls[name] = kwargs
        response = self.responses.get(name)
        if isinstance(response, Exception):
            raise response
        return response

    def estimate(self, payload):
        return self._reply("estimate", payload=payload)

    def submit(self, *, user_id, payload):
        return self._reply("submit", user_id=user_id, payload=payload)

    def get_job(self, job_id, *, user_id):
        return self._reply("get_job", job_id=job_id, user_id=user_id)

    def list_jobs(self, *, user_id, limit):
        return self._reply("list_jobs", user_id=user_id, limit=limit)

    def cancel_job(self, job_id, *, user_id):
        return self._reply("cancel_job", job_id=job_id, user_id=user_id)

    def list_candidates(self, *, user_id, run_id=None, status="", market="", limit=50, offset=0):
        return self._reply(
            "list_candidates", user_id=user_id, run_id=run_id,
            status=status, market=market, limit=limit, offset=offset,
        )

    def get_candidate(self, candidate_id, *, user_id):
        return self._reply("get_candidate", candidate_id=candidate_id, user_id=user_id)


@pytest.fixture
def app():
    return Flask(__name__)


@pytest.fixture
def service(monkeypatch):
    """Install a stub as the route module's service factory and hand it back."""

    def install(**responses):
        stub = _StubService(**responses)
        monkeypatch.setattr(mining_routes, "get_factor_mining_service", lambda: stub)
        return stub

    return install


def _invoke(app, view, *args, path="/", **request_kwargs):
    """Call a view directly, past ``login_required``, in a request context as ``USER_ID``."""
    with app.test_request_context(path, **request_kwargs):
        g.user_id = USER_ID
        return inspect.unwrap(view)(*args)


def _camel(name: str) -> str:
    """``template_ids`` -> ``templateIds``: the mapping the run payload's keys are spelled with."""
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _receipt(**overrides):
    base = {
        "jobId": "job-9",
        "status": "queued",
        "progress": {},
        "config": {"sourceId": 1, "market": "USStock"},
        "result": None,
        "error": None,
        "createdAt": None,
        "startedAt": None,
        "finishedAt": None,
    }
    base.update(overrides)
    return base


def _candidate(**overrides):
    """A library row as ``FactorMiningRepository._hydrate_candidate`` hands it over."""
    row = {
        "id": 5, "userId": USER_ID, "runId": 7, "expressionHash": "a" * 64, "origin": "template",
        "templateId": "momentum", "market": "USStock", "timeframe": "1D", "status": "frozen",
        "reason": "", "createdAt": None, "updatedAt": None, "expression": {"op": "close"},
    }
    row.update(overrides)
    return row


def test_run_returns_202_with_job_receipt(app, service):
    stub = service(submit=_receipt())
    response, status = _invoke(
        app, mining_routes.run_factor_mining,
        path="/api/factor-mining/run", json={"sourceId": 1, "market": "USStock"},
    )
    assert status == 202
    assert response.get_json() == {
        "code": 1, "msg": "common.success", "data": _receipt(),
    }
    assert stub.calls["submit"] == {
        "user_id": USER_ID,
        "payload": {"sourceId": 1, "market": "USStock"},
    }


def test_run_surfaces_validation_error_as_400(app, service):
    service(submit=ValueError("factorMining.sourceRequired"))
    response, status = _invoke(
        app, mining_routes.run_factor_mining, path="/api/factor-mining/run", json={},
    )
    assert status == 400
    assert response.get_json() == {"code": 0, "msg": "factorMining.sourceRequired", "data": None}


def test_run_returns_500_envelope_when_the_service_raises_unexpectedly(app, service):
    service(submit=RuntimeError("connection reset"))
    response, status = _invoke(
        app, mining_routes.run_factor_mining, path="/api/factor-mining/run", json={"sourceId": 1},
    )
    assert status == 500
    assert response.get_json()["msg"] == "factorMining.runFailed"


def test_run_drops_request_keys_a_run_config_never_reads(app, service):
    stub = service(submit=_receipt())
    _invoke(
        app, mining_routes.run_factor_mining, path="/api/factor-mining/run",
        json={
            "sourceId": 1, "budget": 20,
            "__billing": {"charged": 1}, "initialCapital": 50000, "kind": "strategy_evolution",
            "thresholds": {"min_icir": 0.4},
        },
    )
    assert stub.calls["submit"]["payload"] == {
        "sourceId": 1, "budget": 20, "thresholds": {"min_icir": 0.4},
    }


def test_run_and_estimate_pass_the_search_mode_and_pool_settings(app, service):
    """The whitelist is all that stands between the body and the service: a dropped key searches
    something else, silently. ``/estimate`` is priced the same either way and answers the same two
    keys -- the client knows which mode it submitted, so the body does not echo one back."""
    body = {"sourceId": 1, "searchMode": "evolution", "population": 40, "generations": 5,
            "eliteRatio": 0.2}
    stub = service(submit=_receipt(), estimate={"templateCount": 8, "candidateBudget": 40})
    _invoke(app, mining_routes.run_factor_mining, path="/api/factor-mining/run", json=body)
    estimate = _invoke(app, mining_routes.estimate_factor_mining,
                       path="/api/factor-mining/estimate", json=body)
    assert stub.calls["submit"]["payload"] == body
    assert stub.calls["estimate"]["payload"] == body
    assert estimate.get_json()["data"] == {"templateCount": 8, "candidateBudget": 40}


def test_run_field_whitelist_matches_the_config_it_feeds():
    """The whitelist is a hand-copied key set, so a field added to ``MiningConfig`` and forgotten
    here would drop the caller's value silently: the job would search the default instead."""
    assert {_camel(field.name) for field in dataclasses.fields(MiningConfig)} == mining_routes._RUN_FIELDS


def test_estimate_prices_the_search_and_drops_unknown_keys(app, service):
    stub = service(estimate={"templateCount": 8, "candidateBudget": 40})
    response = _invoke(
        app, mining_routes.estimate_factor_mining,
        path="/api/factor-mining/estimate", json={"sourceId": 1, "brandNew": True},
    )
    assert response.status_code == 200
    assert response.get_json()["data"] == {"templateCount": 8, "candidateBudget": 40}
    assert stub.calls["estimate"] == {"payload": {"sourceId": 1}}


def test_estimate_returns_500_envelope_when_the_service_raises_unexpectedly(app, service):
    """``/run`` has a closed key for an unexpected failure; ``/estimate`` answers the same way."""
    service(estimate=RuntimeError("connection reset"))
    response, status = _invoke(
        app, mining_routes.estimate_factor_mining,
        path="/api/factor-mining/estimate", json={"sourceId": 1},
    )
    assert status == 500
    assert response.get_json() == {"code": 0, "msg": "factorMining.estimateFailed", "data": None}


def test_job_list_clamps_the_page_size(app, service):
    stub = service(list_jobs=[_receipt(jobId="job-1"), _receipt(jobId="job-2")])
    response = _invoke(
        app, mining_routes.list_factor_mining_jobs,
        path="/api/factor-mining/jobs?limit=500",
    )
    assert stub.calls["list_jobs"] == {"user_id": USER_ID, "limit": 50}
    assert [item["jobId"] for item in response.get_json()["data"]["items"]] == ["job-1", "job-2"]


def test_a_listed_job_carries_the_config_it_was_opened_with(app, service):
    """A listing withholds ``result``, so ``config`` is the only thing naming the job to a poller.

    It is the market, window and budget a caller recognises their own submission by; a list of
    bare job ids would make the poll loop unusable.
    """
    service(list_jobs=[_receipt(jobId="job-1",
                                config={"market": "CNStock", "startDate": "2023-01-02",
                                        "endDate": "2023-06-30", "budget": 50})])
    response = _invoke(
        app, mining_routes.list_factor_mining_jobs, path="/api/factor-mining/jobs",
    )
    assert response.get_json()["data"]["items"][0]["config"] == {
        "market": "CNStock", "startDate": "2023-01-02", "endDate": "2023-06-30", "budget": 50,
    }


def test_job_detail_returns_404_when_the_service_reports_no_visible_job(app, service):
    """There is one answer for an unknown job and for another tenant's: the service sees neither."""
    stub = service(get_job=None)
    response, status = _invoke(app, mining_routes.get_factor_mining_job, "job-404")
    assert status == 404
    assert response.get_json()["msg"] == "factorMining.jobNotFound"
    assert stub.calls["get_job"] == {"job_id": "job-404", "user_id": USER_ID}


def test_job_detail_serializes_stored_timestamps_as_iso8601(app, service):
    service(get_job=_receipt(
        status="succeeded",
        # The columns are naive UTC: ``datetime.utcnow()`` is written and the reader elsewhere in
        # the job store appends ``Z``, so a naive value here is UTC and must say so.
        createdAt=datetime(2026, 10, 13, 18, 0),
        startedAt=datetime(2026, 10, 13, 18, 0, 1, tzinfo=timezone.utc),
    ))
    response = _invoke(app, mining_routes.get_factor_mining_job, "job-9")
    data = response.get_json()["data"]
    assert data["createdAt"] == "2026-10-13T18:00:00+00:00"
    assert data["startedAt"] == "2026-10-13T18:00:01+00:00"
    assert data["finishedAt"] is None


def test_cancel_returns_the_receipt_and_404s_for_an_unknown_job(app, service):
    stub = service(cancel_job=_receipt(status="cancelled"))
    response = _invoke(app, mining_routes.cancel_factor_mining_job, "job-9")
    assert response.get_json()["data"]["status"] == "cancelled"
    assert stub.calls["cancel_job"] == {"job_id": "job-9", "user_id": USER_ID}

    service(cancel_job=None)
    response, status = _invoke(app, mining_routes.cancel_factor_mining_job, "job-404")
    assert status == 404
    assert response.get_json()["msg"] == "factorMining.jobNotFound"


def test_candidate_list_forwards_the_library_filters(app, service):
    stub = service(list_candidates=[_candidate()])
    response = _invoke(
        app, mining_routes.list_factor_mining_candidates,
        path="/api/factor-mining/candidates?runId=7&status=frozen&market=USStock&limit=10&offset=20",
    )
    assert stub.calls["list_candidates"] == {
        "user_id": USER_ID, "run_id": 7, "status": "frozen",
        "market": "USStock", "limit": 10, "offset": 20,
    }
    assert response.get_json()["data"]["items"] == [_candidate()]


def test_candidate_timestamps_leave_as_iso8601(app, service):
    """The library columns are naive UTC like the job columns, so both exits normalize."""
    service(
        list_candidates=[_candidate(createdAt=datetime(2026, 10, 13, 18, 0))],
        get_candidate=_candidate(
            createdAt=datetime(2026, 10, 13, 18, 0),
            updatedAt=datetime(2026, 10, 13, 19, 30, tzinfo=timezone.utc),
        ),
    )
    listed = _invoke(app, mining_routes.list_factor_mining_candidates).get_json()["data"]["items"]
    detail = _invoke(app, mining_routes.get_candidate, "5").get_json()["data"]
    assert listed[0]["createdAt"] == "2026-10-13T18:00:00+00:00"
    assert listed[0]["updatedAt"] is None
    assert detail["createdAt"] == "2026-10-13T18:00:00+00:00"
    assert detail["updatedAt"] == "2026-10-13T19:30:00+00:00"


def test_candidate_list_rejects_an_unreadable_filter(app, service):
    stub = service(list_candidates=[])
    response, status = _invoke(
        app, mining_routes.list_factor_mining_candidates,
        path="/api/factor-mining/candidates?runId=seven",
    )
    assert status == 400
    assert response.get_json()["msg"] == "factorMining.invalidQuery"
    assert stub.calls == {}


def test_candidate_detail_returns_404_for_unknown_id(app, service):
    stub = service(get_candidate=None)
    response, status = _invoke(app, mining_routes.get_candidate, "999")
    assert status == 404
    assert response.get_json()["msg"] == "factorMining.candidateNotFound"
    assert stub.calls["get_candidate"] == {"candidate_id": 999, "user_id": USER_ID}


def test_candidate_detail_returns_the_candidate_with_its_metrics(app, service):
    service(get_candidate=_candidate(status="frozen", metrics={"rankIc": 0.03}))
    response = _invoke(app, mining_routes.get_candidate, "9")
    assert response.status_code == 200
    assert response.get_json()["data"]["metrics"] == {"rankIc": 0.03}


def test_catalog_lists_fields_operators_and_templates(app):
    response = _invoke(app, mining_routes.factor_mining_catalog)
    payload = response.get_json()["data"]
    assert "close" in payload["fields"]
    assert "ts_mean" in payload["operators"]
    assert payload["templates"]


def test_catalog_templates_carry_metadata_but_never_the_build_callable(app):
    payload = _invoke(app, mining_routes.factor_mining_catalog).get_json()["data"]
    for template in payload["templates"]:
        assert set(template) == {"templateId", "hypothesisI18nKey", "category", "parameterGrid"}
    assert {template["templateId"] for template in payload["templates"]} == {
        "momentum", "reversal", "low_volatility", "liquidity",
        "liquidity_level", "volume_price_confirm", "risk_adjusted_momentum", "conditional_filter",
    }
