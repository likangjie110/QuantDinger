"""Thread-affine IBKR session ownership.

``ib_insync`` binds a connection to the asyncio event loop of the thread that
created it.  The HTTP API serves requests from a pool of gthread workers, so a
client connected on request thread A cannot be driven from request thread B:
the call runs against a different (idle) loop and blocks until it times out.

:class:`IBKRSession` therefore pins one :class:`~app.services.ibkr_trading.client.IBKRClient`
to a dedicated single-thread executor whose thread owns a permanent event loop.
Every call is marshalled onto that thread, which also serialises access so two
concurrent requests cannot interleave on the same socket.

A process-wide registry keyed by ``(host, port, client_id)`` guarantees the
process never competes with itself for a client id -- TWS/IB Gateway allows one
session per client id and answers a second one with
``Error 326: client id is already in use``.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional, Tuple

from app.services.ibkr_trading.client import IBKRClient, IBKRConfig
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Attributes that only read local state and are safe to serve inline.
_DIRECT_ATTRS = frozenset({"config", "connected", "get_connection_status"})

# Upper bound for a marshalled IBKR call.  IBKRConfig.timeout (20s) covers the
# connect handshake; the rest of the calls are request/response round trips.
_CALL_TIMEOUT_SEC = 60.0

_registry: Dict[Tuple[int, str, int, int, str], "IBKRSession"] = {}
_registry_lock = threading.RLock()


def _session_key(config: IBKRConfig, owner_id: int) -> Tuple[int, str, int, int, str]:
    """Registry key for one *owned* session.

    ``(host, port, client_id)`` alone does not identify a tenant: two users
    pointing at the same TWS/Gateway (the UI path defaults to client id 1) would
    otherwise share one connection, and one of them would trade on the other's
    account. The owner keeps that isolation; the IB account id is part of the
    identity too, so switching accounts on one gateway does not reuse the old
    session.
    """
    return (
        int(owner_id),
        str(config.host).strip(),
        int(config.port),
        int(config.client_id),
        str(getattr(config, "account", "") or "").strip(),
    )


class IBKRSession:
    """An :class:`IBKRClient` pinned to its own thread and event loop.

    Attribute access is proxied to the wrapped client: plain state reads are
    served inline, every method call is submitted to the owning thread.
    """

    def __init__(self, config: IBKRConfig, *, owner_id: int = 0):
        self._config = config
        self._key = _session_key(config, owner_id)
        self._closed = False
        # Set when a call times out: the socket state is then unknown.
        self._wedged = False
        self._client = IBKRClient(config)
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"ibkr-{self._key[1]}-{self._key[2]}-{self._key[3]}",
        )
        self._executor.submit(_install_event_loop).result(timeout=10)

    # -- lifecycle -------------------------------------------------------

    def connect(self) -> bool:
        return self._submit(self._client.connect)

    def disconnect(self) -> None:
        """Tear the session down once; later calls are no-ops.

        A shut-down executor, or a client that raises while disconnecting, must not
        leave the session registered, so cleanup happens either way.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._submit(self._client.disconnect)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("IBKR session disconnect raised: %s", exc)
        finally:
            self._executor.shutdown(wait=False)
            _forget(self._key, self)

    @property
    def connected(self) -> bool:
        # A timed-out call leaves the socket in an unknown state: report the session
        # as dead so the next caller replaces it instead of reusing a wedged one.
        return self._client.connected and not self._wedged

    # -- proxy -----------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not defined on IBKRSession itself.
        # Private names are never proxied, so a half-built instance raises
        # AttributeError instead of recursing on ``self._client``.
        if name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(self._client, name)
        if name in _DIRECT_ATTRS or not callable(attr):
            return attr

        def _marshalled(*args: Any, **kwargs: Any) -> Any:
            return self._submit(lambda: attr(*args, **kwargs))

        _marshalled.__name__ = name
        return _marshalled

    def _submit(self, fn: Callable[[], Any]) -> Any:
        try:
            return self._executor.submit(fn).result(timeout=_CALL_TIMEOUT_SEC)
        except TimeoutError:
            self._wedged = True
            raise


def _install_event_loop() -> None:
    """Give the session thread a permanent event loop for ib_insync."""
    asyncio.set_event_loop(asyncio.new_event_loop())


def _forget(key: Tuple[str, int, int], session: "IBKRSession") -> None:
    with _registry_lock:
        if _registry.get(key) is session:
            _registry.pop(key, None)


def get_or_create_session(config: IBKRConfig, *, owner_id: int) -> IBKRSession:
    """Return the live session owned by ``owner_id`` for ``config``, connecting if needed.

    ``owner_id`` is required: sessions are per authenticated user, so one tenant can
    never be handed another tenant's connection and account. Re-using the live session
    for one owner's ``(host, port, client_id)`` triple is what keeps the process from
    answering its own connection with Error 326. A dead session is replaced.
    """
    key = _session_key(config, owner_id)
    existing = None
    with _registry_lock:
        existing = _registry.get(key)
        if existing is not None:
            if existing.connected:
                return existing
            # Retire the dead session before the network call below: disconnecting
            # under this lock would let one slow socket stall every IBKR request.
            _registry.pop(key, None)

        session = IBKRSession(config, owner_id=owner_id)
        _registry[key] = session

    if existing is not None:
        try:
            existing.disconnect()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Stale IBKR session disconnect raised: %s", exc)

    try:
        connected = session.connect()
    except Exception:
        # A half-open session must not stay registered, or the next caller reuses it.
        session.disconnect()
        raise
    if not connected:
        session.disconnect()
        raise ConnectionError(
            "Failed to connect to IBKR TWS/Gateway at "
            f"{config.host}:{config.port} (clientId={config.client_id})."
        )
    return session


def find_session(config: IBKRConfig, *, owner_id: int) -> Optional[IBKRSession]:
    """Return the live session owned by ``owner_id`` for ``config`` without connecting."""
    with _registry_lock:
        session = _registry.get(_session_key(config, owner_id))
    if session is not None and session.connected:
        return session
    return None


__all__ = ["IBKRSession", "get_or_create_session", "find_session"]
