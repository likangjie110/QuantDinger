"""Admin LLM connection probe (``POST /api/settings/test-connection``).

The OpenRouter button previously called ``LLMService.test_connection()``, which
did not exist: the endpoint answered ``code: 0`` with an AttributeError for every
key, so an operator could not tell a bad key from a broken endpoint.
"""

from app.services.llm import LLMService


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _fake_admin(monkeypatch):
    import app.utils.auth as auth_mod

    monkeypatch.setattr(auth_mod, "verify_token", lambda token: {
        "sub": "admin",
        "user_id": 1,
        "role": "admin",
        "_verified_username": "admin",
        "_verified_user_role": "admin",
    })


def _capture_probe(monkeypatch, response):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return response

    monkeypatch.setattr("app.services.llm.requests.get", fake_get)
    return captured


def _post_probe(client):
    return client.post(
        "/api/settings/test-connection",
        json={"service": "openrouter"},
        headers={"Authorization": "Bearer token"},
    )


def test_openrouter_probe_reports_success(client, monkeypatch):
    _fake_admin(monkeypatch)
    monkeypatch.setattr(LLMService, "get_api_key", lambda self, provider=None: "or-key")
    monkeypatch.setattr(LLMService, "get_base_url", lambda self, provider=None: "https://openrouter.ai/api/v1")
    captured = _capture_probe(monkeypatch, _FakeResponse(200, payload={"data": {"label": "sk-or"}}))

    payload = _post_probe(client).get_json()

    assert payload["code"] == 1
    # OpenRouter's /models is public, so only /auth/key can validate the credential.
    assert captured["url"] == "https://openrouter.ai/api/v1/auth/key"
    assert captured["headers"]["Authorization"] == "Bearer or-key"


def test_openrouter_probe_surfaces_provider_failure(client, monkeypatch):
    _fake_admin(monkeypatch)
    monkeypatch.setattr(LLMService, "get_api_key", lambda self, provider=None: "or-key")
    monkeypatch.setattr(LLMService, "get_base_url", lambda self, provider=None: "https://openrouter.ai/api/v1")
    _capture_probe(monkeypatch, _FakeResponse(401, text="No auth credentials found"))

    payload = _post_probe(client).get_json()

    assert payload["code"] == 0
    assert "401" in payload["msg"]
    assert "has no attribute" not in payload["msg"]


def test_openrouter_probe_ignores_the_active_provider(client, monkeypatch):
    """The button must probe OpenRouter even when another provider is configured."""
    _fake_admin(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setattr(LLMService, "get_api_key", lambda self, provider=None: "some-key")

    def fake_base_url(self, provider=None):
        return {
            "openrouter": "https://openrouter.ai/api/v1",
            "deepseek": "https://api.deepseek.com/v1",
        }.get(getattr(provider, "value", ""), "https://api.deepseek.com/v1")

    monkeypatch.setattr(LLMService, "get_base_url", fake_base_url)
    captured = _capture_probe(monkeypatch, _FakeResponse(200, payload={"data": {}}))

    _post_probe(client)

    assert "openrouter.ai" in captured["url"]
