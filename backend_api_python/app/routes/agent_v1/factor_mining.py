"""Agent Gateway factor-mining surface: the same service the human API uses."""
from __future__ import annotations

from flask import g, request

from app.routes.factor_mining import RUN_FIELDS
from app.services.factor_mining import dsl, templates
from app.services.factor_mining.service import get_factor_mining_service
from app.utils.agent_auth import SCOPE_B, SCOPE_R, agent_required, current_user_id

from . import agent_v1_bp
from ._helpers import clip_int, envelope, error, get_json_or_400


def _run_payload() -> dict:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return {}
    return {key: value for key, value in body.items() if key in RUN_FIELDS}


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
    try:
        data = get_factor_mining_service().submit(
            user_id=current_user_id(),
            payload=_run_payload(),
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
