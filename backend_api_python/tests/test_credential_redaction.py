"""Credential redaction for provider errors that reach logs and API responses.

Providers embed the request URL inside exception messages, and some carry the key
as a query parameter, so anything derived from an exception must be scrubbed
before it is logged or returned to a client.
"""

import requests
import pytest

from app.utils.redaction import redact_secrets


def test_redact_secrets_scrubs_query_credentials():
    text = (
        "403 Client Error: Forbidden for url: https://generativelanguage.googleapis.com"
        "/v1beta/models/gemini-2.0-flash:generateContent?key=AIzaSyLEAKED123"
    )
    out = redact_secrets(text)
    assert "AIzaSyLEAKED123" not in out
    assert "key=<redacted>" in out


def test_redact_secrets_scrubs_token_apikey_and_bearer():
    assert "c1234567890" not in redact_secrets(
        "https://finnhub.io/api/v1/quote?symbol=AAPL&token=c1234567890"
    )
    assert "abcd1234" not in redact_secrets(
        "https://api.twelvedata.com/time_series?apikey=abcd1234&interval=1day"
    )
    assert "zzz" not in redact_secrets("https://x/y?api_key=zzz&page=2")
    assert "sk-abcdefghijklmnopqrst" not in redact_secrets("Authorization: Bearer sk-abcdefghijklmnopqrst")


def test_redact_secrets_keeps_harmless_text():
    text = "Model gpt-4o-mini returned 400 for model=x, retrying with fallback"
    assert redact_secrets(text) == text
    assert redact_secrets(None) == ""


def test_gemini_key_travels_in_header_not_url(monkeypatch):
    from app.services.llm import LLMService

    captured = {}

    def fake_post(self, url, *, headers, json_payload, timeout, stream=False):
        captured["url"] = url
        captured["headers"] = headers
        raise requests.exceptions.HTTPError("stop before any network call")

    monkeypatch.setattr(LLMService, "_llm_post", fake_post)
    service = LLMService(provider="google")

    with pytest.raises(Exception):
        service._call_google_gemini(
            [{"role": "user", "content": "hi"}],
            "gemini-1.5-flash",
            0.2,
            "SECRET-GEMINI-KEY",
            "https://generativelanguage.googleapis.com/v1beta",
            5,
        )

    assert "SECRET-GEMINI-KEY" not in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == "SECRET-GEMINI-KEY"


def test_llm_failure_message_is_redacted(monkeypatch):
    """A provider error carrying a key in its URL must not reach the caller verbatim."""
    from app.services.llm import LLMService

    def fake_post(self, url, *, headers, json_payload, timeout, stream=False):
        raise requests.exceptions.HTTPError(
            "403 Client Error for url: https://gateway.internal/relay?key=SECRET-GEMINI-KEY"
        )

    monkeypatch.setattr(LLMService, "_llm_post", fake_post)
    monkeypatch.setattr(LLMService, "get_api_key", lambda self, provider=None: "SECRET-GEMINI-KEY")
    service = LLMService(provider="google")

    with pytest.raises(Exception) as excinfo:
        service.call_llm_api(
            [{"role": "user", "content": "hi"}],
            model="gemini-1.5-flash",
            use_fallback=False,
            try_alternative_providers=False,
        )

    assert "SECRET-GEMINI-KEY" not in str(excinfo.value)


def test_settings_connection_test_scrubs_credential(client, monkeypatch):
    """The Finnhub probe puts the key in the URL; the API response must not echo it."""
    import app.utils.auth as auth_mod

    monkeypatch.setattr(auth_mod, "verify_token", lambda token: {
        "sub": "admin",
        "user_id": 1,
        "role": "admin",
        "_verified_username": "admin",
        "_verified_user_role": "admin",
    })

    def boom(url, timeout=None):
        raise requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='finnhub.io', port=443): Max retries exceeded with url: "
            "/api/v1/quote?symbol=AAPL&token=SECRET-FINNHUB-TOKEN"
        )

    # the route imports requests inside the handler, so patch the module itself
    monkeypatch.setattr("requests.get", boom)

    resp = client.post(
        "/api/settings/test-connection",
        json={"service": "finnhub", "api_key": "SECRET-FINNHUB-TOKEN"},
        headers={"Authorization": "Bearer token"},
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["code"] == 0
    assert "SECRET-FINNHUB-TOKEN" not in payload["msg"]
    assert "<redacted>" in payload["msg"]
