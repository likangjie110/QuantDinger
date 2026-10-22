"""Agent Gateway factor-mining surface: the same service the human API uses."""
from __future__ import annotations

from flask import g, request

from app.routes.factor_mining import RUN_FIELDS
from app.services.factor_mining import dsl, templates
from app.services.factor_mining.service import get_factor_mining_service
from app.services.script_source import get_script_source_service
from app.services.strategy_v2.contract import compile_strategy_v2
from app.utils.agent_auth import (
    SCOPE_B, SCOPE_R, agent_required, current_user_id, market_allowed,
)

from . import agent_v1_bp
from ._helpers import clip_int, envelope, error, get_json_or_400
from ._security import manifest_allowlist_error, token_has_restricted_allowlist


def _run_payload() -> dict:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return {}
    return {key: value for key, value in body.items() if key in RUN_FIELDS}


def _effective_run_payload(service, payload: dict, user_id: int) -> dict:
    """The config a replay actually runs with: the recorded run's own config."""
    replay_id = str(payload.get("replayJobId") or "").strip()
    if not replay_id:
        return payload
    original = service.get_job(replay_id, user_id=user_id)
    config = (original or {}).get("config") or {}
    return dict(config) if config else payload


def _allowlist_rejection(payload: dict, user_id: int):
    """403 when a restricted token asks outside its market/instrument allowlist."""
    if not (
        token_has_restricted_allowlist("markets")
        or token_has_restricted_allowlist("instruments")
    ):
        return None
    market = str(payload.get("market") or "")
    if market and not market_allowed(market):
        return error(403, f"Market not allowed: {market}", http=403)
    try:
        source_id = int(payload.get("sourceId") or 0)
    except (TypeError, ValueError):
        return None
    if source_id <= 0:
        return None
    source = get_script_source_service().get_source(source_id, user_id=user_id)
    code = str((source or {}).get("code") or "")
    if not code:
        return None
    try:
        program = compile_strategy_v2(code)
    except Exception as exc:
        return error(400, str(exc), http=400)
    reason = manifest_allowlist_error(program.manifest)
    if reason:
        return error(403, reason, http=403)
    return None


@agent_v1_bp.route("/factor-mining/catalog", methods=["GET"])
@agent_required(SCOPE_R)
def get_factor_mining_catalog():
    return envelope({
        "fields": list(dsl.fields_for(str(request.args.get("market") or ""))),
        "operators": list(dsl.OPERATOR_SPECS),
        "templates": templates.template_catalog(),
    })


@agent_v1_bp.route("/factor-mining/estimate", methods=["POST"])
@agent_required(SCOPE_R)
def estimate_factor_mining():
    _body, err = get_json_or_400()
    if err:
        return err
    try:
        return envelope(get_factor_mining_service().estimate(_run_payload()))
    except ValueError as exc:
        return error(400, str(exc))


@agent_v1_bp.route("/factor-mining/jobs", methods=["POST"])
@agent_required(SCOPE_B)
def submit_factor_mining_job():
    body, err = get_json_or_400()
    if err:
        return err
    payload = {key: value for key, value in body.items() if key in RUN_FIELDS}
    service = get_factor_mining_service()
    user_id = current_user_id()
    rejection = None
    if (
        token_has_restricted_allowlist("markets")
        or token_has_restricted_allowlist("instruments")
    ):
        rejection = _allowlist_rejection(
            _effective_run_payload(service, payload, user_id), user_id,
        )
    if rejection:
        return rejection
    try:
        data = service.submit(
            user_id=user_id,
            payload=payload,
            agent_token_id=int(g.agent_token["id"]),
        )
    except ValueError as exc:
        return error(400, str(exc))
    return envelope(data, status=202)


@agent_v1_bp.route("/factor-mining/jobs", methods=["GET"])
@agent_required(SCOPE_R)
def list_factor_mining_jobs():
    limit = clip_int(request.args.get("limit"), default=20, lo=1, hi=50)
    return envelope({"items": get_factor_mining_service().list_jobs(
        user_id=current_user_id(), limit=limit,
    )})


@agent_v1_bp.route("/factor-mining/jobs/<job_id>", methods=["GET"])
@agent_required(SCOPE_R)
def get_factor_mining_job(job_id: str):
    job = get_factor_mining_service().get_job(job_id, user_id=current_user_id())
    if job is None:
        return error(404, "factorMining.jobNotFound", http=404)
    return envelope(job)


@agent_v1_bp.route("/factor-mining/jobs/<job_id>/cancel", methods=["POST"])
@agent_required(SCOPE_B)
def cancel_factor_mining_job(job_id: str):
    job = get_factor_mining_service().cancel_job(job_id, user_id=current_user_id())
    if job is None:
        return error(404, "factorMining.jobNotFound", http=404)
    if job.get("status") != "cancelled":
        return error(409, f"Job is already {job.get('status')}", http=409)
    return envelope(job, message="cancelled")


@agent_v1_bp.route("/factor-mining/jobs/<job_id>/proposals", methods=["GET"])
@agent_required(SCOPE_R)
def list_factor_mining_proposals(job_id: str):
    items = get_factor_mining_service().get_job_proposals(
        job_id,
        user_id=current_user_id(),
        limit=clip_int(request.args.get("limit"), default=50, lo=1, hi=200),
        offset=clip_int(request.args.get("offset"), default=0, lo=0, hi=1_000_000),
    )
    if items is None:
        return error(404, "factorMining.jobNotFound", http=404)
    return envelope({"items": items})


@agent_v1_bp.route("/factor-mining/candidates", methods=["GET"])
@agent_required(SCOPE_R)
def list_factor_mining_candidates():
    raw_run_id = request.args.get("runId")
    run_id = None
    if raw_run_id not in (None, ""):
        try:
            run_id = int(raw_run_id)
        except (TypeError, ValueError):
            return error(400, "factorMining.invalidQuery")
    items = get_factor_mining_service().list_candidates(
        user_id=current_user_id(),
        run_id=run_id,
        status=str(request.args.get("status") or ""),
        market=str(request.args.get("market") or ""),
        limit=clip_int(request.args.get("limit"), default=50, lo=1, hi=200),
        offset=clip_int(request.args.get("offset"), default=0, lo=0, hi=1_000_000),
    )
    return envelope({"items": items})


@agent_v1_bp.route("/factor-mining/candidates/<candidate_id>", methods=["GET"])
@agent_required(SCOPE_R)
def get_factor_mining_candidate(candidate_id: str):
    try:
        identifier = int(candidate_id)
    except (TypeError, ValueError):
        return error(404, "factorMining.candidateNotFound", http=404)
    candidate = get_factor_mining_service().get_candidate(
        identifier, user_id=current_user_id(),
    )
    if not candidate:
        return error(404, "factorMining.candidateNotFound", http=404)
    return envelope(candidate)
