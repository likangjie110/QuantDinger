"""Agent Gateway security helpers for secret redaction, payload bounds, and allowlists."""
from __future__ import annotations

from typing import Any, Mapping, MutableMapping

from app.utils.agent_auth import current_token, instrument_allowed, market_allowed

# Upper bound for indicator Python source accepted via agent/MCP paths.
MAX_SOURCE_CODE_BYTES = 512 * 1024
MAX_INDICATOR_CODE_BYTES = MAX_SOURCE_CODE_BYTES

# Keys stripped or masked anywhere in agent-facing JSON.
_SECRET_KEYS = frozenset({
    "apikey", "api_key", "secretkey", "secret_key", "passphrase", "secret", "password",
    "privatekey", "private_key", "accesstoken", "access_token", "refreshtoken", "refresh_token", "bottoken", "bot_token",
    "webhooksecret", "webhook_secret", "signingsecret", "signing_secret", "clientsecret", "client_secret",
})


def indicator_code_too_large(code: str) -> bool:
    return len((code or "").encode("utf-8")) > MAX_INDICATOR_CODE_BYTES


def assert_indicator_code_size(code: str) -> None:
    if indicator_code_too_large(code):
        raise ValueError(
            f"Indicator code exceeds {MAX_INDICATOR_CODE_BYTES // 1024} KiB limit"
        )


def assert_strategy_code_size(code: str) -> None:
    if len((code or "").encode("utf-8")) > MAX_SOURCE_CODE_BYTES:
        raise ValueError(
            f"Strategy code exceeds {MAX_SOURCE_CODE_BYTES // 1024} KiB limit"
        )


def token_has_restricted_allowlist(field: str) -> bool:
    raw = str(current_token().get(field) or "*").strip()
    return raw not in {"", "*"}


def manifest_allowlist_error(manifest) -> str | None:
    """The first allowlist violation in a compiled strategy manifest, or None."""
    metadata = manifest.metadata()
    for market in manifest.markets:
        if not market_allowed(market):
            return f"Market not allowed: {market}"
    instruments: list[dict] = list((metadata.get("universe") or {}).get("instruments") or [])
    for subscription in metadata.get("subscriptions") or []:
        instruments.extend(subscription.get("instruments") or [])
    if isinstance(metadata.get("benchmark"), dict):
        instruments.append(metadata["benchmark"])
    seen: set[tuple[str, str]] = set()
    for instrument in instruments:
        market = str(instrument.get("market") or "")
        symbol = str(instrument.get("symbol") or "")
        key = (market, symbol)
        if key in seen:
            continue
        seen.add(key)
        if market and not market_allowed(market):
            return f"Market not allowed: {market}"
        if symbol and not instrument_allowed(symbol):
            return f"Instrument not allowed: {symbol}"
    universe = metadata.get("universe") or {}
    dynamic_references = [str(universe.get("reference") or "").strip()]
    dynamic_references.extend(
        str(item.get("universeReference") or "").strip()
        for item in metadata.get("subscriptions") or []
    )
    if any(dynamic_references) and token_has_restricted_allowlist("instruments"):
        return "Dynamic universes require an unrestricted instrument allowlist"
    return None


def redact_secrets(value: Any, *, depth: int = 0, max_depth: int = 6) -> Any:
    """Return a copy with known credential fields masked."""
    if depth > max_depth:
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if key.replace("-", "_").lower() in _SECRET_KEYS and v not in (None, "", False):
                out[key] = "***"
            elif isinstance(v, Mapping):
                out[key] = redact_secrets(v, depth=depth + 1, max_depth=max_depth)
            elif isinstance(v, list):
                out[key] = [
                    redact_secrets(item, depth=depth + 1, max_depth=max_depth)
                    for item in v
                ]
            else:
                out[key] = v
        return out
    if isinstance(value, list):
        return [redact_secrets(item, depth=depth + 1, max_depth=max_depth) for item in value]
    return value


def redact_strategy_row(row: dict | None) -> dict | None:
    """Mask credential-like fields before returning a strategy to an agent."""
    if not row:
        return row
    out = dict(row)
    for field in ("exchange_config", "trading_config", "notification_config"):
        if isinstance(out.get(field), dict):
            out[field] = redact_secrets(out[field])
    return out
