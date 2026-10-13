"""
Probe a TWS/IB Gateway for the IBKR session-ownership facts that code review cannot settle.

Read-only by design: every connection passes ``readonly=True`` and this script never places,
modifies or cancels an order.

Questions it answers
--------------------
1. Does TWS/Gateway *replace* or *reject* a second connection that reuses one client id?
   - replace: two sessions sharing an id silently knock each other offline, so orders stop
     without an error on either side;
   - reject: the second caller fails loudly with error 326 ("client id already in use").
2. Do two *different* client ids coexist? (Control that makes answer 1 meaningful.)
3. What is ``IB.RequestTimeout``? Left at 0, a wedged request hangs until the caller's own
   ceiling instead of failing fast — the account-snapshot session relies on a 60s timeout.

``--selftest`` answers a fourth question with no gateway at all: whether the session registry in
``app/services/ibkr_trading/session.py`` is process-private (it is), which is why
``GUNICORN_WORKERS>1`` leaves every worker holding its own connection for the same client id.

Usage (from backend_api_python):
  python scripts/ibkr_probe_session_ownership.py --selftest
  python scripts/ibkr_probe_session_ownership.py --host 127.0.0.1 --port 4002 --client-id 99

Pick a client id nothing else is using. The repo already allocates 1 (admin UI), 7 (live
orders), 8 (execution stream) and 9 (account snapshots); the default here is 99.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

DEFAULT_CLIENT_ID = 99
RESERVED_CLIENT_IDS = (1, 7, 8, 9)
DEFAULT_TIMEOUT_SEC = 10.0
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_ib():
    try:
        from ib_insync import IB
    except ImportError:  # pragma: no cover - depends on the environment
        raise SystemExit("ib_insync is required: python -m pip install -r requirements.txt")
    return IB


def _connect(IB, *, host: str, port: int, client_id: int, timeout: float):
    """Connect read-only; return ``(ib_instance, error_text)``."""
    ib = IB()
    try:
        ib.connect(host, port, clientId=int(client_id), timeout=timeout, readonly=True)
    except Exception as exc:  # noqa: BLE001 - the message is the finding
        return ib, f"{type(exc).__name__}: {exc}"
    return ib, ""


def probe(*, host: str, port: int, client_id: int, timeout: float = DEFAULT_TIMEOUT_SEC) -> dict:
    """Run the duplicate-client-id experiment against a live gateway."""
    IB = _load_ib()
    report: dict = {
        "host": host,
        "port": port,
        "client_id": client_id,
        "readonly": True,
    }

    first, first_err = _connect(IB, host=host, port=port, client_id=client_id, timeout=timeout)
    report["first_connected"] = first.isConnected()
    report["first_error"] = first_err
    if not first.isConnected():
        report["verdict"] = "unreachable"
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return report

    server_version = getattr(first.client, "serverVersion", None)
    report["server_version"] = server_version() if callable(server_version) else None
    report["request_timeout"] = float(getattr(first, "RequestTimeout", 0) or 0)
    report["managed_accounts"] = len(first.managedAccounts() or [])

    duplicate, duplicate_err = _connect(IB, host=host, port=port, client_id=client_id, timeout=timeout)
    duplicate_connected = duplicate.isConnected()
    report["duplicate_client_id"] = {
        "second_connected": duplicate_connected,
        "first_still_connected": first.isConnected(),
        "second_error": duplicate_err,
    }
    if duplicate_connected and not first.isConnected():
        report["verdict"] = "replaces"
    elif not duplicate_connected:
        report["verdict"] = "rejects"
    else:
        report["verdict"] = "coexists"

    for ib in (duplicate, first):
        try:
            ib.disconnect()
        except Exception:  # noqa: BLE001 - teardown must not mask the finding
            pass

    # Control: a second, different client id must be able to coexist.
    control_a, control_a_err = _connect(IB, host=host, port=port, client_id=client_id, timeout=timeout)
    control_b, control_b_err = _connect(
        IB, host=host, port=port, client_id=client_id + 1, timeout=timeout
    )
    report["control_distinct_client_ids"] = {
        "first_connected": control_a.isConnected(),
        "second_connected": control_b.isConnected(),
        "second_error": control_b_err or control_a_err,
    }
    for ib in (control_b, control_a):
        try:
            ib.disconnect()
        except Exception:  # noqa: BLE001
            pass

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def selftest_registry_is_process_private() -> dict:
    """Show that a session registered by one process is invisible to another."""
    child_code = (
        "from app.services.ibkr_trading import session as s;"
        "s._registry[(4242, 'gateway.test', 4002, 1, '')] = object();"
        "print(len(s._registry))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = BACKEND_DIR + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", child_code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(
            "child process failed to import the app: "
            f"{completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else 'unknown'}"
        )

    # Running as ``python scripts/...`` puts scripts/ on sys.path, not the backend root.
    if BACKEND_DIR not in sys.path:
        sys.path.insert(0, BACKEND_DIR)
    from app.services.ibkr_trading import session as session_module

    report = {
        "child_entries": int(completed.stdout.strip() or 0),
        "parent_entries": len(session_module._registry),
        "process_private": int(completed.stdout.strip() or 0) == 1
        and len(session_module._registry) == 0,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    parser.add_argument("--host", help="TWS/IB Gateway host")
    parser.add_argument("--port", type=int, help="TWS/IB Gateway port (7497 TWS paper, 4002 Gateway paper)")
    parser.add_argument("--client-id", type=int, default=DEFAULT_CLIENT_ID, help=f"default {DEFAULT_CLIENT_ID}")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--selftest", action="store_true", help="run the process-private registry check only")
    args = parser.parse_args()

    if args.selftest:
        selftest_registry_is_process_private()
        return 0

    if not args.host or not args.port:
        parser.error("--host and --port are required (or use --selftest)")
    if args.client_id in RESERVED_CLIENT_IDS:
        print(
            f"warning: client id {args.client_id} is allocated elsewhere in this repo "
            f"{RESERVED_CLIENT_IDS}; connecting may evict that session.",
            file=sys.stderr,
        )
    probe(host=args.host, port=int(args.port), client_id=int(args.client_id), timeout=float(args.timeout))
    return 0


if __name__ == "__main__":
    sys.exit(main())
