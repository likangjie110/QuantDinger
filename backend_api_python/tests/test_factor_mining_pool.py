"""Factor mining candidate pool: scoping, self-exclusion, the cap, and the panel rebuild.

Nothing here connects to a database. The library is an in-memory table behind a fake connection
that applies the ``column = ?`` conditions the statement binds, so a row from another tenant,
market, timeframe, or status is really kept out of the pool instead of merely asserted to be. The
fake honours the page bound but hands rows back in ascending id order -- deliberately not the
store's newest-first order -- because the pool has to pin its own order rather than trust the
driver's.

Every panel expectation is derived outside the module under test: against pandas directly, or
against the ``ExpressionEvaluator`` the engine scores candidates with.
"""

import json
import re
from contextlib import contextmanager

import pandas as pd
import pytest

from app.services.factor_mining import engine, pool, storage
from app.services.factor_mining.dsl import normalize_expression
from app.services.factor_mining.engine import _field_panels
from app.services.factor_mining.evaluator import ExpressionEvaluator

USER_ID = 4
MARKET = "USStock"
TIMEFRAME = "1d"
MOMENTUM = {"op": "ts_pct_change", "args": ["close"], "params": {"n": 20}}
VOLUME_MEAN = {"op": "ts_mean", "args": ["volume"], "params": {"n": 5}}
LAG = {"op": "lag", "args": ["close"], "params": {"n": 3}}


class _LibraryCursor:
    """A cursor over an in-memory candidate table, filtered and paged by what the statement binds."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.statements: list[tuple] = []
        self._matched: list[dict] = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        # ``column = ?`` are the conditions, so ``LIMIT ?`` -- the page bound -- is the value right
        # after them. The store pages its newest rows, as ``ORDER BY id DESC LIMIT`` says, and the
        # rows then come back in ascending id order: not the order the pool may rely on.
        filters = dict(zip(re.findall(r"(\w+) = \?", sql), params or ()))
        matched = [
            row for row in self._rows
            if all(row.get(column) == value for column, value in filters.items())
        ]
        if "LIMIT ?" in sql:
            matched = sorted(matched, key=lambda row: row["id"], reverse=True)[:params[len(filters)]]
        self._matched = sorted(matched, key=lambda row: row["id"])

    def fetchall(self):
        return list(self._matched)

    def fetchone(self):
        return None

    def close(self):
        pass


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass


def _library(monkeypatch, rows) -> _LibraryCursor:
    """Point the repository at an in-memory library and return the cursor that served it."""
    cursor = _LibraryCursor(rows)
    connection = _Connection(cursor)

    @contextmanager
    def _connect():
        yield connection

    monkeypatch.setattr(storage, "get_db_connection", _connect)
    return cursor


def _row(candidate_id, expression, *, user_id=USER_ID, market=MARKET, timeframe=TIMEFRAME,
         status="frozen", expression_hash="") -> dict:
    """One ``qd_factor_candidates`` row as the driver hands it back, every column included.

    ``expression_hash`` is the fingerprint the library stored. It is derived from the expression
    unless the test stores something no longer fingerprintable -- the stored column is what the
    row's identity is, whatever the expression in it says.
    """
    return {
        "id": candidate_id,
        "user_id": user_id,
        "run_id": 9,
        "expression_json": json.dumps(expression),
        "expression_hash": expression_hash or _hash(expression),
        "origin": "template",
        "template_id": "momentum",
        "market": market,
        "timeframe": timeframe,
        "status": status,
        "reject_reason": "",
        "metrics_json": "{}",
        "out_of_sample_json": "{}",
        "created_at": None,
        "updated_at": None,
    }


def _hash(expression) -> str:
    """The fingerprint the library stores for ``expression``: its identity across runs."""
    return normalize_expression(expression).fingerprint


def _frames(periods=60) -> dict:
    """One frame per symbol, shaped the way the mining provider hands them to the engine."""
    index = pd.date_range("2025-01-01", periods=periods, freq="B")
    frames = {}
    for offset, symbol in enumerate(("A", "B", "C")):
        close = [100 + offset * 5 + day for day in range(periods)]
        frames[f"USStock:{symbol}"] = pd.DataFrame(
            {"open": close, "high": close, "low": close, "close": close,
             "volume": [1000 + day for day in range(periods)]},
            index=index,
        )
    return frames


def test_pool_is_scoped_to_one_user_market_and_timeframe(monkeypatch):
    """Only the caller's own frozen (market, timeframe) candidates are pool members.

    Factor values are cross-sectional signals: a candidate mined on another market or another
    timeframe is not comparable to this one at all, another tenant's row is not the caller's to
    read, and a candidate still in ``search`` has not been through the out-of-sample review that
    makes it a pool member -- so every one of those four conditions has to hold.
    """
    cursor = _library(monkeypatch, [
        _row(11, MOMENTUM),
        _row(12, MOMENTUM, market="Crypto"),
        _row(13, MOMENTUM, timeframe="1h"),
        _row(14, MOMENTUM, user_id=7),
        _row(15, MOMENTUM, status="search"),
    ])

    members = pool.load_pool(USER_ID, MARKET, TIMEFRAME)

    assert [(member.candidate_id, member.fingerprint) for member in members] == [
        (11, _hash(MOMENTUM)),
    ]
    sql, params = cursor.statements[0]
    for clause in ("user_id = ?", "status = ?", "market = ?", "timeframe = ?"):
        assert clause in sql
    assert params[:4] == (USER_ID, "frozen", MARKET, TIMEFRAME)


def test_pool_panels_are_rebuilt_from_the_stored_expression(monkeypatch):
    """A member's panel is recomputed from its stored expression, on the caller's field panels.

    Rebuilding is why the library stores an expression and not a copy of a panel: the member's
    factor has to be evaluated on the data this candidate is being scored over, or the correlation
    between them measures two different things.
    """
    fields = _field_panels(_frames())
    _library(monkeypatch, [_row(11, MOMENTUM)])

    members = pool.load_pool(USER_ID, MARKET, TIMEFRAME)
    panels, skipped = pool.pool_panels(members, fields)

    assert list(panels) == [_hash(MOMENTUM)]
    assert skipped == []
    pd.testing.assert_frame_equal(panels[_hash(MOMENTUM)], fields["close"].pct_change(20, fill_method=None))
    # The 20-bar warmup stays NaN rather than producing a shorter, differently dated panel.
    assert panels[_hash(MOMENTUM)].iloc[:20].isna().all().all()
    # Evaluated on the very field panels the engine scores candidates with, so a member is
    # aligned to the same dates and symbols as the candidate it is compared against.
    pd.testing.assert_frame_equal(panels[_hash(MOMENTUM)], ExpressionEvaluator(fields).evaluate(MOMENTUM))


def test_pool_excludes_the_candidate_under_evaluation(monkeypatch):
    """The candidate being scored is never part of its own comparison set.

    Dropped by expression hash, because that is how the caller knows what it is evaluating, and
    dropped before the cap: a pool trimmed first would come back one member short and silently
    weaken every gate that reads it. A fingerprint that is not in the library -- a candidate that
    has not been frozen yet -- changes nothing.
    """
    _library(monkeypatch, [
        _row(1, MOMENTUM),
        _row(2, VOLUME_MEAN),
        _row(3, LAG),
        _row(4, {"op": "ts_std", "args": ["close"], "params": {"n": 10}}),
    ])

    members = pool.load_pool(USER_ID, MARKET, TIMEFRAME, exclude_hash=_hash({
        "op": "ts_std", "args": ["close"], "params": {"n": 10},
    }), limit=2)

    assert [member.candidate_id for member in members] == [3, 2]
    elsewhere = pool.load_pool(USER_ID, MARKET, TIMEFRAME, exclude_hash="f" * 64, limit=2)
    assert [member.candidate_id for member in elsewhere] == [4, 3]


def test_pool_caps_the_number_of_members(monkeypatch):
    """The pool is capped, and which members survive the cap does not depend on the row order.

    The cap is the library's newest-first order -- what was frozen most recently is what a new
    candidate is compared against -- applied to the pool's own sorting rather than to whatever
    order the driver returned. A library smaller than the cap is returned whole.
    """
    _library(monkeypatch, [
        _row(candidate_id, {"op": "lag", "args": ["close"], "params": {"n": candidate_id}})
        for candidate_id in range(1, 6)
    ])

    members = pool.load_pool(USER_ID, MARKET, TIMEFRAME, limit=2)

    assert [member.candidate_id for member in members] == [5, 4]
    assert [m.candidate_id for m in pool.load_pool(USER_ID, MARKET, TIMEFRAME, limit=9)] == [5, 4, 3, 2, 1]


def test_pool_panels_report_a_member_whose_stored_expression_no_longer_evaluates(monkeypatch):
    """A member that cannot be scored is reported as skipped, never silently dropped.

    The library outlives the code that wrote it: an expression the DSL no longer accepts, or a row
    whose JSON does not parse, cannot be turned into a panel. Dropping one without a word would
    leave a caller unable to tell "this member is unrelated to the candidate" from "this member was
    never measured" -- and the gates that read the pool have to treat the second as a missing
    measurement, not as clearance.
    """
    fields = _field_panels(_frames())
    _library(monkeypatch, [
        _row(11, MOMENTUM),
        _row(12, VOLUME_MEAN),
        _row(13, {"op": "not_an_operator", "args": ["close"], "params": {}},
             expression_hash="e" * 64),
        {**_row(14, LAG), "expression_json": "{not json"},
    ])

    panels, skipped = pool.pool_panels(pool.load_pool(USER_ID, MARKET, TIMEFRAME), fields)

    assert sorted(panels) == sorted([_hash(MOMENTUM), _hash(VOLUME_MEAN)])
    # Newest member first, the pool's own order, both identities named.
    assert [item["fingerprint"] for item in skipped] == [_hash(LAG), "e" * 64]
    assert {item["reason"] for item in skipped} == {"factorMining.reject.invalid"}
    pd.testing.assert_frame_equal(panels[_hash(VOLUME_MEAN)], ExpressionEvaluator(fields).evaluate(VOLUME_MEAN))


def test_pool_refuses_an_absent_market_or_timeframe(monkeypatch):
    """An empty market or timeframe is refused, not quietly read as "every market".

    The store drops an empty filter, so a missing one passed through would hand back a pool
    spanning markets and timeframes whose factor values are not comparable at all -- every gate
    reading that pool would then be comparing across markets without saying so.
    """
    _library(monkeypatch, [_row(11, MOMENTUM), _row(12, MOMENTUM, market="Crypto")])

    with pytest.raises(ValueError) as raised:
        pool.load_pool(USER_ID, "", TIMEFRAME)
    assert str(raised.value) == "factorMining.invalidParameter"
    with pytest.raises(ValueError):
        pool.load_pool(USER_ID, MARKET, "")


def test_an_empty_library_reads_as_an_empty_pool(monkeypatch):
    """No frozen candidate for this market and timeframe is an empty pool, not an error."""
    _library(monkeypatch, [])

    members = pool.load_pool(USER_ID, MARKET, TIMEFRAME)

    assert members == []
    assert pool.pool_panels(members, _field_panels(_frames())) == ({}, [])


def test_the_pool_and_the_engine_report_a_member_the_same_way():
    """The two spellings of the invalid-expression reason must stay one key.

    They are written in two modules on purpose -- the engine imports the pool once the pool
    feeds its correlations, so importing the engine's constant here would close the cycle -- and
    that is exactly why the equality needs pinning instead of reviewing: the pool's ``skipped``
    rows and the engine's rejected trials land in columns a reader compares, and a rename on
    either side would file the same failure under two different reasons with no test to notice.
    """
    assert pool.INVALID_EXPRESSION_REASON == engine.INVALID_EXPRESSION_REASON
