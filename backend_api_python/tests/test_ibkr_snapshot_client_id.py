"""The IBKR account snapshot must not borrow another session's client id.

Reusing the credential's ``ibkr_client_id`` (live orders, default 7) let the
broker-accounts page evict the order session; TWS keeps one session per client id.
"""

import app.services.live_trading.account_snapshot as snap


class _FakeClient:
    def __init__(self):
        self.disconnected = False

    def get_positions(self):
        return []

    def get_open_orders(self):
        return []

    def disconnect(self):
        self.disconnected = True


def _capture(monkeypatch):
    captured = {}

    def fake_create_client(config, market_type=None):
        captured["config"] = config
        captured["market_type"] = market_type
        return _FakeClient()

    monkeypatch.setattr(snap, "create_client", fake_create_client)
    return captured


def test_snapshot_uses_a_client_id_that_is_not_the_order_session(monkeypatch):
    monkeypatch.delenv("IBKR_SNAPSHOT_CLIENT_ID", raising=False)
    captured = _capture(monkeypatch)

    snap._fetch_ibkr_snapshot(
        {"ibkr_host": "gateway.test", "ibkr_port": 4002, "ibkr_client_id": 7},
        [],
    )

    assert captured["config"]["ibkr_client_id"] == 9
    # 8 is the execution stream, 7 the order session, 1 the UI.
    assert captured["config"]["ibkr_client_id"] not in (1, 7, 8)
    assert captured["config"]["ibkr_host"] == "gateway.test"
    assert captured["config"]["ibkr_port"] == 4002


def test_snapshot_client_id_is_configurable(monkeypatch):
    monkeypatch.setenv("IBKR_SNAPSHOT_CLIENT_ID", "11")
    captured = _capture(monkeypatch)

    snap._fetch_ibkr_snapshot({"ibkr_client_id": 7}, [])

    assert captured["config"]["ibkr_client_id"] == 11


def test_snapshot_falls_back_when_the_setting_is_not_a_number(monkeypatch):
    monkeypatch.setenv("IBKR_SNAPSHOT_CLIENT_ID", "not-a-number")
    captured = _capture(monkeypatch)

    snap._fetch_ibkr_snapshot({"ibkr_client_id": 7}, [])

    assert captured["config"]["ibkr_client_id"] == 9
