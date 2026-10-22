"""Human-facing factor mining API: submit a search, poll its job, and browse the library.

A facade. Every decision -- which payload is legal, what a search costs, whose job or candidate a
row is -- belongs to the service layer, and a rejection arrives here as the closed i18n key that
layer raised. What stays at this boundary is the two things a facade owes a JSON client: the
request body is cut to the keys a run config reads, so a client's own key is dropped rather than
persisted into the job store's request, and a stored timestamp leaves as ISO-8601, because the
job columns are naive UTC and ``jsonify`` would otherwise render them as RFC-822 next to the ISO
date strings inside the same response.
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import g, jsonify, request

from app.openapi.blueprint import HumanBlueprint as Blueprint
from app.services.factor_mining import dsl, templates
from app.services.factor_mining.service import get_factor_mining_service
from app.utils.auth import login_required
from app.utils.logger import get_logger


logger = get_logger(__name__)
factor_mining_blp = Blueprint("factor_mining", __name__)

# The keys ``MiningConfig.from_payload`` reads, and no others. Deliberately a second whitelist
# next to the service's own ``__`` strip: that one guards where the mapping enters the service,
# this one guards where the untrusted body enters the process.
_RUN_FIELDS = frozenset({
    "sourceId", "sourceName", "market", "timeframe", "startDate", "endDate", "freezeRatio", "holdoutRatio",
    "budget", "seed", "templateIds", "groups", "holdingPeriod", "commission", "slippage",
    "neutralizeIndustry", "thresholds", "searchMode", "population", "generations", "eliteRatio",
    "aiRounds", "aiProposalsPerRound", "replayJobId", "researchNotes",
})

_RUN_REQUEST_SCHEMA = {
    "type": "object",
    "properties": {
        **{key: {} for key in sorted(_RUN_FIELDS)},
        "holdoutRatio": {
            "type": "number", "minimum": 0, "maximum": 0.3, "default": 0,
            "description": "Reserve the tail without evaluating it; new product UI defaults to 0.10.",
        },
        "searchMode": {"type": "string", "enum": ["enumeration", "evolution", "ai"],
                       "default": "enumeration"},
        "replayJobId": {"type": "string", "description":
                        "An owned terminal AI run. Submit only searchMode=ai and replayJobId to replay its recorded configuration."},
    },
}
_RUN_REQUEST_BODY = {"required": True, "content": {
    "application/json": {"schema": _RUN_REQUEST_SCHEMA},
}}


def _run_payload() -> dict:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return {}
    return {key: value for key, value in body.items() if key in _RUN_FIELDS}


def _query_int(name: str) -> int | None:
    """An optional integer query argument. An unreadable one is the caller's error, not a 500."""
    raw = request.args.get(name)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("factorMining.invalidQuery") from exc


def _iso_timestamps(payload: dict | None) -> dict | None:
    """A stored row as the API spells it: every top-level ``datetime`` becomes ISO-8601.

    Shape-neutral on purpose: the job columns and the candidate columns are both naive UTC, so
    a receipt, a listed job, and a library row all have to leave through here -- one column
    rendered as RFC-822 next to another rendered as ISO-8601 is the same API speaking twice.
    """
    if not payload:
        return payload
    return {key: _iso(value) for key, value in payload.items()}


def _iso(value):
    if not isinstance(value, datetime):
        return value
    # ``qd_agent_jobs`` timestamps are written by ``datetime.utcnow()`` into a plain TIMESTAMP
    # column, so a naive value is UTC -- the same reading ``agent_jobs`` itself applies when it
    # spells its SSE events.
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


@factor_mining_blp.route("/catalog", methods=["GET"])
@login_required
def factor_mining_catalog():
    """The DSL's own vocabulary: searchable fields, operators, and hypothesis templates."""
    # ``fields`` is the *market's* set, not the price-volume group: a client that is about to
    # submit a run already holds the market (``/run`` requires it), and a Crypto run's editor
    # otherwise offers only the price-volume group among the fields its candidates may name. The optional ``market``
    # query argument is read through the same ``fields_for`` the validator uses, so what this
    # advertises and what L1 admits cannot drift apart. Absent and unrecognised both take
    # ``fields_for``'s own fallback -- the price-volume group, which refuses rather than admits.
    # The docstring stays one paragraph on purpose: it becomes the route's OpenAPI description.
    return jsonify({
        "code": 1,
        "msg": "common.success",
        "data": {
            "fields": list(dsl.fields_for(str(request.args.get("market") or ""))),
            "operators": list(dsl.OPERATOR_SPECS),
            "templates": templates.template_catalog(),
        },
    })


@factor_mining_blp.route("/estimate", methods=["POST"])
@factor_mining_blp.doc(requestBody=_RUN_REQUEST_BODY)
@login_required
def estimate_factor_mining():
    """Price a search before submitting it: no source read, no panel, no job."""
    try:
        data = get_factor_mining_service().estimate(_run_payload())
        return jsonify({"code": 1, "msg": "common.success", "data": data})
    except ValueError as exc:
        return jsonify({"code": 0, "msg": str(exc), "data": None}), 400
    except Exception:
        # The same envelope ``/run`` answers an unexpected failure with. Without this arm an
        # unhandled exception leaves as a generic 500 with no key for the caller to read.
        logger.exception("Factor mining estimate failed")
        return jsonify({"code": 0, "msg": "factorMining.estimateFailed", "data": None}), 500


@factor_mining_blp.route("/run", methods=["POST"])
@factor_mining_blp.doc(requestBody=_RUN_REQUEST_BODY)
@login_required
def run_factor_mining():
    """Queue one mining run. 202: the receipt is the job, not its result."""
    try:
        data = get_factor_mining_service().submit(user_id=int(g.user_id), payload=_run_payload())
        return jsonify({"code": 1, "msg": "common.success", "data": _iso_timestamps(data)}), 202
    except ValueError as exc:
        return jsonify({"code": 0, "msg": str(exc), "data": None}), 400
    except Exception:
        logger.exception("Factor mining run failed")
        return jsonify({"code": 0, "msg": "factorMining.runFailed", "data": None}), 500


@factor_mining_blp.route("/jobs", methods=["GET"])
@login_required
def list_factor_mining_jobs():
    """The caller's mining jobs, newest first, each with the progress its summary lacks."""
    try:
        limit = max(1, min(_query_int("limit") or 20, 50))
        items = get_factor_mining_service().list_jobs(user_id=int(g.user_id), limit=limit)
        return jsonify({"code": 1, "msg": "common.success", "data": {
            "items": [_iso_timestamps(item) for item in items],
        }})
    except ValueError as exc:
        return jsonify({"code": 0, "msg": str(exc), "data": None}), 400


@factor_mining_blp.route("/jobs/<job_id>", methods=["GET"])
@login_required
def get_factor_mining_job(job_id: str):
    """One job of the caller's, including cumulative research evidence.

    result.summary.research contains experimentId, contextHash, runTrials, cumulativeTrials,
    freezeVersion, freezeSnapshotId and holdout (reserved, startDate, endDate, evaluated=false).
    Holdout is reserved only; the data fingerprint does not imply a persisted market-data snapshot.
    Another tenant's and another feature's jobs are both not found.
    """
    job = get_factor_mining_service().get_job(job_id, user_id=int(g.user_id))
    if not job:
        return jsonify({"code": 0, "msg": "factorMining.jobNotFound", "data": None}), 404
    return jsonify({"code": 1, "msg": "common.success", "data": _iso_timestamps(job)})


@factor_mining_blp.route("/jobs/<job_id>/cancel", methods=["POST"])
@login_required
def cancel_factor_mining_job(job_id: str):
    """Stop one of the caller's jobs; the runner halts at its next progress report."""
    job = get_factor_mining_service().cancel_job(job_id, user_id=int(g.user_id))
    if not job:
        return jsonify({"code": 0, "msg": "factorMining.jobNotFound", "data": None}), 404
    return jsonify({"code": 1, "msg": "common.success", "data": _iso_timestamps(job)})


@factor_mining_blp.route("/jobs/<job_id>/proposals", methods=["GET"])
@login_required
def list_factor_mining_proposals(job_id: str):
    """The proposals one AI job's run recorded, in generation order.

    A running job answers what it has written so far -- possibly nothing -- so a caller can
    watch the rounds arrive; an unknown job, another tenant's, and a job that never opened a
    run are all not found.
    """
    try:
        items = get_factor_mining_service().get_job_proposals(
            job_id,
            user_id=int(g.user_id),
            limit=max(1, min(_query_int("limit") or 50, 200)),
            offset=max(0, _query_int("offset") or 0),
        )
    except ValueError as exc:
        return jsonify({"code": 0, "msg": str(exc), "data": None}), 400
    if items is None:
        response = jsonify({"code": 0, "msg": "factorMining.jobNotFound", "data": None})
        response.status_code = 404
        return response
    return jsonify({"code": 1, "msg": "common.success", "data": {
        "items": [_iso_timestamps(item) for item in items],
    }})


@factor_mining_blp.route("/candidates", methods=["GET"])
@login_required
def list_factor_mining_candidates():
    """The caller's candidate library. Filters and the page window are the repository's own."""
    try:
        items = get_factor_mining_service().list_candidates(
            user_id=int(g.user_id),
            run_id=_query_int("runId"),
            status=str(request.args.get("status") or ""),
            market=str(request.args.get("market") or ""),
            limit=max(1, min(_query_int("limit") or 50, 200)),
            offset=max(0, _query_int("offset") or 0),
        )
        return jsonify({"code": 1, "msg": "common.success", "data": {
            "items": [_iso_timestamps(item) for item in items],
        }})
    except ValueError as exc:
        return jsonify({"code": 0, "msg": str(exc), "data": None}), 400


@factor_mining_blp.route("/candidates/<candidate_id>", methods=["GET"])
@login_required
def get_candidate(candidate_id: str):
    """One candidate with the metrics and out-of-sample block a list row leaves behind."""
    try:
        identifier = int(candidate_id)
    except (TypeError, ValueError):
        # An id that cannot name a row is the same answer as an id that names no row.
        return jsonify({"code": 0, "msg": "factorMining.candidateNotFound", "data": None}), 404
    candidate = get_factor_mining_service().get_candidate(identifier, user_id=int(g.user_id))
    if not candidate:
        return jsonify({"code": 0, "msg": "factorMining.candidateNotFound", "data": None}), 404
    return jsonify({"code": 1, "msg": "common.success", "data": _iso_timestamps(candidate)})
