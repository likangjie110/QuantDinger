"""Canonical identities for cumulative research and compatible AI feedback."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any


def _date_text(value: Any) -> str:
    return value.date().isoformat() if isinstance(value, datetime) else str(value or "")[:10]


def research_context(config: Any) -> dict[str, Any]:
    """Group repeated searches on the same source and data split, regardless of tuning."""
    def read(key: str, default: Any = None) -> Any:
        return config.get(key, default) if isinstance(config, Mapping) else getattr(config, key, default)

    return {
        "version": 1,
        "sourceId": int(read("source_id", 0)),
        "market": str(read("market", "")),
        "timeframe": str(read("timeframe", "")).upper(),
        "startDate": _date_text(read("start_date")),
        "endDate": _date_text(read("end_date")),
        "freezeRatio": float(read("freeze_ratio", .3)),
        "holdoutRatio": float(read("holdout_ratio", 0.0)),
    }


def feedback_context(config: Any, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Feedback additionally pins holding period and the source's asset-pool semantics."""
    holding = config.get("holding_period", 5) if isinstance(config, Mapping) else config.holding_period
    return {**research_context(config), "holdingPeriod": int(holding),
            "universe": dict(manifest.get("universe") or {})}


def context_hash(context: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(context), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
