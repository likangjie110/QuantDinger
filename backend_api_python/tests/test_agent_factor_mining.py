"""Agent Gateway factor-mining surface: same service, agent envelope and scopes."""

import pytest

from app.routes.agent_v1 import factor_mining as route
from app.utils import agent_auth


def _token(scopes: str = "R,B") -> dict:
    return {
        "id": 77, "user_id": 31, "name": "factor-agent", "scopes": scopes,
        "markets": "*", "instruments": "*", "paper_only": True,
        "rate_limit_per_min": 100, "status": "active", "expires_at": None,
    }


def _headers(key: str | None = None) -> dict:
    headers = {"Authorization": "Bearer qd_agent_FACTORTEST12345"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


class _StubService:
    def __init__(self, **responses):
        self.responses = responses
        self.calls = {}

    def _reply(self, name, **kwargs):
        self.calls[name] = kwargs
        value = self.responses.get(name)
        if isinstance(value, Exception):
            raise value
        return value

    def estimate(self, payload):
        return self._reply("estimate", payload=payload)

    def submit(self, *, user_id, payload, agent_token_id=None):
        return self._reply(
            "submit", user_id=user_id, payload=payload, agent_token_id=agent_token_id,
        )

    def get_job(self, job_id, *, user_id):
        return self._reply("get_job", job_id=job_id, user_id=user_id)

    def get_job_proposals(self, job_id, *, user_id, limit=50, offset=0):
        return self._reply(
            "get_job_proposals", job_id=job_id, user_id=user_id, limit=limit, offset=offset,
        )

    def list_jobs(self, *, user_id, limit):
        return self._reply("list_jobs", user_id=user_id, limit=limit)

    def cancel_job(self, job_id, *, user_id):
        return self._reply("cancel_job", job_id=job_id, user_id=user_id)

    def list_candidates(self, *, user_id, run_id=None, status="", market="", limit=50, offset=0):
        return self._reply(
            "list_candidates", user_id=user_id, run_id=run_id, status=status,
            market=market, limit=limit, offset=offset,
        )

    def get_candidate(self, candidate_id, *, user_id):
        return self._reply("get_candidate", candidate_id=candidate_id, user_id=user_id)


@pytest.fixture(autouse=True)
def _authorized(monkeypatch):
    token = _token()
    agent_auth._schema_ready = True
    agent_auth._rate_state.clear()
    monkeypatch.setattr(agent_auth, "_lookup_token", lambda _raw: token)
    monkeypatch.setattr(agent_auth, "_touch_token_last_used", lambda *_: None)
    monkeypatch.setattr(agent_auth, "_audit", lambda *a, **kw: None)
    monkeypatch.setattr(agent_auth, "_reserve_idempotency", lambda *_: ("reserved", None))
    monkeypatch.setattr(agent_auth, "_complete_idempotency", lambda *_: None)
    yield token
    agent_auth._rate_state.clear()


@pytest.fixture
def service(monkeypatch):
    def install(**responses):
        stub = _StubService(**responses)
        monkeypatch.setattr(route, "get_factor_mining_service", lambda: stub)
        return stub
    return install


def test_catalog_is_read_scope_and_returns_the_dsl_vocabulary(client):
    body = client.get("/api/agent/v1/factor-mining/catalog", headers=_headers()).get_json()
    assert body["code"] == 0
    assert set(body["data"]) == {"fields", "operators", "templates"}


def test_estimate_is_read_scope_and_needs_no_idempotency_key(client, service):
    stub = service(estimate={"templateCount": 8, "candidateBudget": 40})
    response = client.post(
        "/api/agent/v1/factor-mining/estimate",
        headers=_headers(),
        json={"sourceId": 1, "market": "USStock", "startDate": "2023-01-01",
              "endDate": "2023-12-31", "unknown": True},
    )
    assert response.status_code == 200
    assert stub.calls["estimate"]["payload"]["sourceId"] == 1
    assert "unknown" not in stub.calls["estimate"]["payload"]


def test_submit_requires_an_idempotency_key(client, service):
    service(submit={"jobId": "job-1", "status": "queued"})
    response = client.post(
        "/api/agent/v1/factor-mining/jobs",
        headers=_headers(),
        json={"sourceId": 1, "market": "USStock", "startDate": "2023-01-01",
              "endDate": "2023-12-31"},
    )
    assert response.status_code == 400
    assert "Idempotency-Key" in response.get_json()["message"]


def test_submit_returns_202_and_records_the_agent_token(client, service):
    stub = service(submit={"jobId": "job-1", "status": "queued"})
    response = client.post(
        "/api/agent/v1/factor-mining/jobs",
        headers=_headers("submit-2"),
        json={"sourceId": 1, "market": "USStock", "startDate": "2023-01-01",
              "endDate": "2023-12-31", "unknown": True},
    )
    assert response.status_code == 202
    assert response.get_json()["data"]["jobId"] == "job-1"
    assert stub.calls["submit"]["user_id"] == 31
    assert stub.calls["submit"]["agent_token_id"] == 77
    assert "unknown" not in stub.calls["submit"]["payload"]


def test_submit_surfaces_the_service_key_as_400(client, service):
    service(submit=ValueError("factorMining.sourceRequired"))
    response = client.post(
        "/api/agent/v1/factor-mining/jobs",
        headers=_headers("submit-3"),
        json={"market": "USStock"},
    )
    assert response.status_code == 400
    assert response.get_json()["message"] == "factorMining.sourceRequired"


def test_list_jobs_delegates_to_the_service_sweep_path(client, service):
    stub = service(list_jobs=[{"jobId": "job-1", "status": "succeeded"}])
    response = client.get(
        "/api/agent/v1/factor-mining/jobs?limit=5", headers=_headers(),
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["items"][0]["jobId"] == "job-1"
    assert stub.calls["list_jobs"] == {"user_id": 31, "limit": 5}


def test_get_job_returns_404_when_the_service_has_no_row(client, service):
    service(get_job=None)
    response = client.get(
        "/api/agent/v1/factor-mining/jobs/job-404", headers=_headers(),
    )
    assert response.status_code == 404
    assert response.get_json()["message"] == "factorMining.jobNotFound"


def test_proposals_are_tenant_scoped_and_paged(client, service):
    stub = service(get_job_proposals=[{"id": 1, "round": 0, "ordinal": 0}])
    response = client.get(
        "/api/agent/v1/factor-mining/jobs/job-1/proposals?limit=10&offset=2",
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["items"][0]["id"] == 1
    assert stub.calls["get_job_proposals"] == {
        "job_id": "job-1", "user_id": 31, "limit": 10, "offset": 2,
    }


def test_proposals_return_404_for_an_unknown_job(client, service):
    service(get_job_proposals=None)
    response = client.get(
        "/api/agent/v1/factor-mining/jobs/job-404/proposals", headers=_headers(),
    )
    assert response.status_code == 404
    assert response.get_json()["message"] == "factorMining.jobNotFound"


def test_submit_requires_b_scope(client, service, _authorized):
    stub = service(submit={"jobId": "job-1", "status": "queued"})
    _authorized["scopes"] = "R"
    response = client.post(
        "/api/agent/v1/factor-mining/jobs",
        headers=_headers("submit-1"),
        json={"sourceId": 1, "market": "USStock", "startDate": "2023-01-01",
              "endDate": "2023-12-31"},
    )
    assert response.status_code == 403
    assert "submit" not in stub.calls


def test_cancel_requires_b_scope(client, service, _authorized):
    stub = service(cancel_job={"jobId": "job-1", "status": "cancelled"})
    _authorized["scopes"] = "R"
    response = client.post(
        "/api/agent/v1/factor-mining/jobs/job-1/cancel", headers=_headers("cancel-1"),
    )
    assert response.status_code == 403
    assert "cancel_job" not in stub.calls


def test_candidates_forward_filters_and_reject_a_non_integer_run_id(client, service):
    stub = service(list_candidates=[{"id": 5}])
    response = client.get(
        "/api/agent/v1/factor-mining/candidates?runId=7&status=frozen&market=USStock&limit=10",
        headers=_headers(),
    )
    assert response.status_code == 200
    assert stub.calls["list_candidates"] == {
        "user_id": 31, "run_id": 7, "status": "frozen",
        "market": "USStock", "limit": 10, "offset": 0,
    }
    bad = client.get(
        "/api/agent/v1/factor-mining/candidates?runId=abc", headers=_headers(),
    )
    assert bad.status_code == 400
    assert bad.get_json()["message"] == "factorMining.invalidQuery"


def test_candidate_with_a_non_integer_id_is_404(client, service):
    service(get_candidate={"id": 5})
    response = client.get(
        "/api/agent/v1/factor-mining/candidates/not-an-int", headers=_headers(),
    )
    assert response.status_code == 404
    assert response.get_json()["message"] == "factorMining.candidateNotFound"


def test_run_payload_keeps_the_shared_whitelist_and_drops_unknown_keys(client, service):
    stub = service(submit={"jobId": "job-1", "status": "queued"})
    client.post(
        "/api/agent/v1/factor-mining/jobs",
        headers=_headers("submit-4"),
        json={"sourceId": 1, "market": "USStock", "startDate": "2023-01-01",
              "endDate": "2023-12-31", "searchMode": "evolution",
              "population": 40, "generations": 5, "eliteRatio": 0.2,
              "aiRounds": 2, "aiProposalsPerRound": 10,
              "researchNotes": "note", "replayJobId": "job-0",
              "unknown": True, "__userId": 999},
    )
    assert stub.calls["submit"]["payload"] == {
        "sourceId": 1, "market": "USStock", "startDate": "2023-01-01",
        "endDate": "2023-12-31", "searchMode": "evolution",
        "population": 40, "generations": 5, "eliteRatio": 0.2,
        "aiRounds": 2, "aiProposalsPerRound": 10,
        "researchNotes": "note", "replayJobId": "job-0",
    }
