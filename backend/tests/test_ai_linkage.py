"""AI Recommendation → Paper Order → Position → Exit → P&L linkage tests.

Contract under test:

  * ``signal_store.persist_signal`` keeps the complete decision-time evidence:
    the six directional component scores, the signed nets / long / short / net
    totals / conflict flag, ATR, strategy version, quality and order usage
    (quantity, risk amount, effective risk %).
  * Profit-capture flows keep the SAME ``signal_id`` on every resulting ledger
    row (T1 partial + T2 final / trailing) and the same ``position_id``.
  * ``/api/paper/trades`` exposes the AI snapshot per trade (additive keys
    only; legacy contract untouched) and marks manual / historical rows
    ``ai_decision = NOT_AVAILABLE`` without fabricating a recommendation.
  * ``GET /api/paper/trades/{trade_id}/ai-decision`` is read-only and returns
    the full recommendation-vs-execution breakdown, including the position
    lifecycle for profit-capture flows (404 / NOT_AVAILABLE otherwise).
"""

import json
import sqlite3
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session as SyncSession

from app.api.auth import create_token
from app.core.database import Base, get_db
from app.main import app as _app
from app.models.models import Signal, Trade, User
from app.services import signal_store
from app.services.paper_trading import PaperTradingEngine
from app.services.profit_capture import ProfitCaptureConfig

USER = "aaaaaaaaaaaaaaaa"
EMAIL = "ai-link@test.local"
# Exactly 16 chars: signal_store truncates ids to signals.id VARCHAR(16).
SID = "siglink123456789"


# ────────────────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────────────────

def _fake_signal(sig_id=SID, direction="LONG", atr=1.45, strategy_version="v1"):
    return {
        "id": sig_id,
        "symbol": "TEST",
        "timestamp": "2026-09-29 13:50:00",
        "direction": direction,
        "confidence": 0.82,
        "signal_score": {
            "total": 72.0, "trend_score": 18.0, "momentum_score": 12.0,
            "volume_score": 11.0, "vwap_score": 11.0, "price_action_score": 10.0,
            "market_context_score": 5.0, "risk_quality_score": 8.0,
        },
        "setup": {"entry": 100.0, "stop_loss": 98.0, "target_1": 105.0,
                  "target_2": 107.0, "risk_reward_ratio": 2.5,
                  "risk_per_share": 2.0},
        "explanation": {"reasons": ["Trend up"], "risks": ["Volatile"]},
        "strategy": "multi_factor",
        "strategy_version": strategy_version,
        "data_source": "yfinance",
        "signal_generated_at": "2026-09-29 13:51:20",
        "indicator_values": {"atr_14": atr},
        "direction_evidence": {
            "nets": {"trend": 0.2, "momentum": 0.1, "volume": 0.0,
                     "vwap": 0.3, "price_action": 0.2, "market_context": 0.4},
            "net": 12.0, "long_total": 40.0, "short_total": 10.0,
            "conflict": False, "risk_quality": "OK",
        },
        "context_24h": None,
    }


# ────────────────────────────────────────────────────────────────────────────
# signal_store — full evidence persistence
# ────────────────────────────────────────────────────────────────────────────

def test_schema_migration_adds_evidence_columns(tmp_path):
    db = str(tmp_path / "migrate.db")
    # Old-shape table (as it existed pre-feature) → migration must be additive.
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE signals (id VARCHAR(16) PRIMARY KEY, symbol VARCHAR(50), "
                "timestamp DATETIME, direction VARCHAR(20), signal_score FLOAT)")
    con.execute("CREATE TABLE paper_positions (id VARCHAR(16) PRIMARY KEY, symbol VARCHAR(50))")
    con.commit()
    con.close()
    signal_store.ensure_signal_schema(db)
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(signals)").fetchall()}
    con.close()
    assert {"atr", "strategy_version", "risk_percent"} <= cols


def test_persist_signal_stores_atr_version_and_full_evidence(tmp_path):
    db = str(tmp_path / "evidence.db")
    signal_store.persist_signal(_fake_signal(), candle_ts="2026-09-29 13:50:00",
                                market_ctx={"nifty_trend": "UP"}, quality="QUALIFIED",
                                db_path=db)
    signal_store.update_signal_usage(SID, 10, 40.0, risk_percent=2.0, db_path=db)
    con = sqlite3.connect(db)
    row = con.execute("SELECT atr, strategy_version, risk_percent, quantity, risk_amount, "
                      "signal_quality FROM signals WHERE id = ?", (SID,)).fetchone()
    ind = json.loads(con.execute(
        "SELECT indicator_scores FROM signals WHERE id = ?", (SID,)).fetchone()[0])
    con.close()
    assert row[0] == pytest.approx(1.45)          # atr_14 at decision time
    assert row[1] == "v1"                          # strategy version
    assert row[2] == pytest.approx(2.0)            # risk % (effective)
    assert row[3] == 10 and row[4] == pytest.approx(40.0)
    assert row[5] == "QUALIFIED"
    # Complete decision-time evidence is preserved in the JSON payload.
    assert ind["nets"]["trend"] == 0.2
    assert ind["net"] == 12.0
    assert ind["long_total"] == 40.0
    assert ind["short_total"] == 10.0
    assert ind["conflict"] is False
    assert ind["evidence_risk_quality"] == "OK"
    assert ind["atr"] == pytest.approx(1.45)
    assert ind["trend_score"] == 18.0


def test_update_usage_records_risk_percent(tmp_path):
    db = str(tmp_path / "usage.db")
    signal_store.persist_signal(_fake_signal(), candle_ts="2026-09-29 13:50:00", db_path=db)
    signal_store.update_signal_usage(SID, 12, 48.0, risk_percent=1.92, db_path=db)
    con = sqlite3.connect(db)
    row = con.execute("SELECT quantity, risk_amount, risk_percent FROM signals "
                      "WHERE id = ?", (SID,)).fetchone()
    con.close()
    assert row == (12, 48.0, pytest.approx(1.92))


# ────────────────────────────────────────────────────────────────────────────
# paper-engine level — signal_id across the full profit-capture lifecycle
# ────────────────────────────────────────────────────────────────────────────

def test_profit_capture_rows_keep_signal_id_and_position_id(tmp_path):
    db = str(tmp_path / "pc.db")
    engine = PaperTradingEngine(
        db_path=db, persist=False,
        profit_config=ProfitCaptureConfig(t1_exit_percent=50.0),
    )
    r = engine.place_order("TEST", "LONG", 10, 100.0, 98.0, 105.0, 107.0,
                           user_id=USER, signal_id=SID)
    assert "error" not in r
    pos_id = r["position"]["id"]
    assert r["position"]["signal_id"] == SID

    fill = engine.fill_order(pos_id, fill_price=100.0, user_id=USER)
    assert fill["status"] == "filled"

    # T1 reached → 50% partial exit (one row, position stays open).
    exits1 = engine.check_stops({"TEST": 105.0}, user_id=USER)
    assert len(exits1) == 1
    assert exits1[0]["partial"] is True
    assert exits1[0]["exit_reason"] == "T1_PARTIAL"
    t1_row = exits1[0]["trade"]
    assert t1_row["signal_id"] == SID
    assert t1_row["position_id"] == pos_id
    assert t1_row["exit_quantity"] == pytest.approx(5.0)
    assert t1_row["remaining_quantity"] == pytest.approx(5.0)

    # T2 reached on the same tick the remaining position is re-evaluated →
    # final close under the same signal_id (T2_FINAL).
    exits2 = engine.check_stops({"TEST": 107.0}, user_id=USER)
    assert len(exits2) == 1
    assert exits2[0]["exit_reason"] == "T2_FINAL"
    t2_row = exits2[0]["trade"]
    assert t2_row["signal_id"] == SID
    assert t2_row["position_id"] == pos_id
    assert t2_row["exit_quantity"] == pytest.approx(5.0)

    closed = engine.get_closed_trades(user_id=USER)
    assert len(closed) == 2
    assert {t["exit_reason"] for t in closed} == {"T1_PARTIAL", "T2_FINAL"}
    assert all(t["signal_id"] == SID for t in closed)
    assert all(t["position_id"] == pos_id for t in closed)
    # Both rows are distinct ledger records of the same position (unique ids
    # are assigned when the rows are finalized through _finalize_paper_trade).


def test_manual_order_has_no_signal_id(tmp_path):
    db = str(tmp_path / "manual.db")
    engine = PaperTradingEngine(db_path=db, persist=False)
    r = engine.place_order("MANUAL", "LONG", 5, 100.0, 98.0, 105.0, 107.0,
                           user_id=USER)
    assert r["position"].get("signal_id") is None
    fill = engine.fill_order(r["position"]["id"], fill_price=100.0, user_id=USER)
    assert fill["status"] == "filled"
    exits = engine.check_stops({"MANUAL": 96.0}, user_id=USER)
    assert len(exits) == 1
    assert exits[0]["exit_reason"] == "STOP_LOSS"
    assert exits[0]["trade"].get("signal_id") is None


# ────────────────────────────────────────────────────────────────────────────
# _attach_ai_decisions — enrichment contract (pure unit)
# ────────────────────────────────────────────────────────────────────────────

def _sig_ns(sig_id=SID, **kw):
    defaults = dict(
        id=sig_id, symbol="TEST",
        timestamp=__import__("datetime").datetime(2026, 9, 29, 13, 50, 0),
        direction="LONG", signal_score=72.0, signal_quality="QUALIFIED",
        confidence=0.82, strategy="multi_factor", strategy_version=None,
        data_source="yfinance", atr=1.45, entry_price=100.0, stop_loss=98.0,
        target_1=105.0, target_2=107.0, risk_reward=2.5, quantity=10,
        risk_amount=40.0, risk_percent=2.0,
        indicator_scores=json.dumps({
            "trend_score": 18.0, "momentum_score": 12.0, "volume_score": 11.0,
            "vwap_score": 11.0, "price_action_score": 10.0,
            "market_context_score": 5.0, "risk_quality_score": 8.0,
            "nets": {"trend": 0.2}, "net": 12.0, "long_total": 40.0,
            "short_total": 10.0, "conflict": False, "evidence_risk_quality": "OK",
        }),
    )
    defaults.update(kw)
    return NS(**defaults)


def _trade_ns(tid="trade11", signal_id=SID, **kw):
    d = dict(id=tid, symbol="TEST", direction="LONG", entry_price=100.0,
             exit_price=107.0, quantity=10, stop_loss=98.0, target_1=105.0,
             target_2=107.0, entry_time="2026-09-29 13:55:00",
             exit_time="2026-09-29 14:10:00", pnl=35.0, result="WIN",
             status="closed", signal_id=signal_id, actual_entry=100.0,
             actual_exit=107.0, realized_pnl=35.0, unrealized_pnl=0.0,
             exit_reason="T2_FINAL", position_id="pos111", exit_quantity=10.0)
    d.update(kw)
    return NS(**d)


def test_attach_ai_decisions_enriches_resolvable_trades():
    from app.api.trading import _attach_ai_decisions
    trades = [
        _trade_ns(tid="t1"),                       # signal linked
        _trade_ns(tid="t2", signal_id=None),       # manual order
        _trade_ns(tid="t3", signal_id="missingid"),  # signal row deleted
    ]
    out = _attach_ai_decisions([{k: v for k, v in t.__dict__.items()} for t in trades],
                               {SID: _sig_ns()})
    t1 = next(t for t in out if t["id"] == "t1")
    assert t1["ai_decision"] == "AVAILABLE"
    assert t1["ai_direction"] == "LONG"
    assert t1["ai_score"] == 72.0
    assert t1["signal_quality"] == "QUALIFIED"
    assert t1["ai_trend_score"] == 18.0
    assert t1["ai_risk_quality_score"] == 8.0
    assert t1["original_entry"] == 100.0
    assert t1["original_stop_loss"] == 98.0
    assert t1["original_target_1"] == 105.0
    assert t1["original_target_2"] == 107.0
    assert t1["original_risk_reward"] == 2.5
    assert t1["original_quantity"] == 10
    assert t1["original_risk_amount"] == 40.0
    assert t1["original_risk_percent"] == 2.0
    assert t1["ai_atr"] == 1.45
    assert t1["ai_evidence"]["net"] == 12.0
    # Execution aliases must remain the ACTUAL values, not the AI setup.
    assert t1["actual_entry"] == 100.0 and t1["actual_exit"] == 107.0
    for tid in ("t2", "t3"):
        row = next(t for t in out if t["id"] == tid)
        assert row["ai_decision"] == "NOT_AVAILABLE"
        assert row["ai_available"] is False


# ────────────────────────────────────────────────────────────────────────────
# API level — /api/paper/trades/{trade_id}/ai-decision (TestClient, temp DB)
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def client_env(tmp_path, monkeypatch):
    import app.core.database as database_mod

    db_file = tmp_path / "test_ai_api.db"
    sync_engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(sync_engine)
    with SyncSession(sync_engine) as s:
        s.add(User(id=USER, email=EMAIL, name="AI Link"))
        s.commit()
    sync_engine.dispose()

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    Session = async_sessionmaker(async_engine, expire_on_commit=False)

    async def _override_get_db():
        async with Session() as session:
            yield session

    _app.dependency_overrides[get_db] = _override_get_db
    monkeypatch.setattr(database_mod, "async_session", Session)  # trading/pkg reads

    client = TestClient(_app)
    yield {"client": client, "engine": async_engine}
    _app.dependency_overrides.clear()


def _auth():
    return {"Authorization": f"Bearer {create_token(USER, EMAIL)}"}


def _insert_signal(s, sig_id=SID, **kw):
    row = Signal(
        id=sig_id, symbol="TEST", timestamp=__import__("datetime").datetime(
            2026, 9, 29, 13, 50, 0),
        direction="LONG", signal_score=72.0, confidence=0.82,
        entry_price=100.0, stop_loss=98.0, target_1=105.0, target_2=107.0,
        risk_reward=2.5, strategy="multi_factor", strategy_version="v1",
        data_source="yfinance", signal_quality="QUALIFIED", quantity=10,
        risk_amount=40.0, risk_percent=2.0, atr=1.45,
        indicator_scores=json.dumps({
            "trend_score": 18.0, "momentum_score": 12.0, "volume_score": 11.0,
            "vwap_score": 11.0, "price_action_score": 10.0,
            "market_context_score": 5.0, "risk_quality_score": 8.0,
            "nets": {"trend": 0.2}, "net": 12.0, "long_total": 40.0,
            "short_total": 10.0, "conflict": False, "evidence_risk_quality": "OK",
        }),
    )
    for k, v in kw.items():
        setattr(row, k, v)
    s.add(row)


def _insert_trade(s, tid, signal_id=SID, exit_reason="T2_FINAL",
                  position_id="pos111", pnl=35.0, **kw):
    import datetime
    row = Trade(
        id=tid, user_id=USER, signal_id=signal_id, symbol="TEST",
        direction="LONG", entry_price=100.0, exit_price=107.0, quantity=10,
        stop_loss=98.0, target_1=105.0, target_2=107.0,
        entry_time=datetime.datetime(2026, 9, 29, 13, 55, 0),
        exit_time=datetime.datetime(2026, 9, 29, 14, 10, 0),
        status="closed", pnl=pnl,
        details_json=json.dumps({"exit_reason": exit_reason,
                                 "position_id": position_id}),
    )
    for k, v in kw.items():
        setattr(row, k, v)
    s.add(row)


def test_ai_decision_endpoint_available_with_full_breakdown(client_env):
    from sqlalchemy import create_engine as _ce
    # Seed Signal + a two-row profit-capture lifecycle (T1 partial, T2 final).
    import datetime
    db_path = client_env["engine"].url.database
    sync = _ce(f"sqlite:///{db_path}")
    with SyncSession(sync) as s:
        _insert_signal(s)
        _insert_trade(s, "tradet1", exit_reason="T1_PARTIAL",
                      position_id="pos111", pnl=25.0,
                      exit_time=datetime.datetime(2026, 9, 29, 14, 5, 0),
                      exit_price=105.0)
        _insert_trade(s, "tradet2", exit_reason="T2_FINAL",
                      position_id="pos111", pnl=35.0)
        s.commit()
    sync.dispose()

    client = client_env["client"]
    resp = client.get(f"/api/paper/trades/tradet2/ai-decision", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ai_decision"] == "AVAILABLE"
    assert body["signal"]["id"] == SID
    assert body["signal"]["direction"] == "LONG"
    assert body["signal"]["signal_score"] == 72.0
    assert body["score_breakdown"]["trend_score"] == 18.0
    assert body["score_breakdown"]["total"] == 72.0
    assert body["evidence"]["net"] == 12.0
    assert body["original_setup"]["entry"] == 100.0
    assert body["original_setup"]["stop_loss"] == 98.0
    assert body["original_setup"]["target_1"] == 105.0
    assert body["original_setup"]["target_2"] == 107.0
    assert body["original_setup"]["risk_reward"] == 2.5
    assert body["original_setup"]["quantity"] == 10
    assert body["original_setup"]["risk_amount"] == 40.0
    assert body["original_setup"]["atr"] == 1.45
    # Execution facts are the ACTUAL values.
    assert body["execution"]["actual_entry"] == 100.0
    assert body["execution"]["actual_exit"] == 107.0
    assert body["execution"]["realized_pnl"] == 35.0
    assert body["execution"]["exit_reason"] == "T2_FINAL"
    # Profit-capture lifecycle: both rows share the same position and signal.
    assert len(body["lifecycle"]) == 2
    assert [r["exit_reason"] for r in body["lifecycle"]] == ["T1_PARTIAL", "T2_FINAL"]
    assert all(r["pnl"] is not None for r in body["lifecycle"])
    assert body["lifecycle_pnl"] == pytest.approx(60.0)


def test_ai_decision_endpoint_not_available_for_manual_trade(client_env):
    db_path = client_env["engine"].url.database
    sync = create_engine(f"sqlite:///{db_path}")
    with SyncSession(sync) as s:
        _insert_trade(s, "manual1", signal_id=None, exit_reason="MANUAL_CLOSE")
        s.commit()
    sync.dispose()
    client = client_env["client"]
    resp = client.get("/api/paper/trades/manual1/ai-decision", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ai_decision"] == "NOT_AVAILABLE"
    assert "not persisted" in body["reason"].lower()
    assert body["execution"]["exit_reason"] == "MANUAL_CLOSE"


def test_ai_decision_endpoint_404_for_unknown_trade(client_env):
    client = client_env["client"]
    resp = client.get("/api/paper/trades/nosuchtrade/ai-decision", headers=_auth())
    assert resp.status_code == 404


def test_trade_history_exposes_signal_id_and_ai_fields(client_env):
    db_path = client_env["engine"].url.database
    sync = create_engine(f"sqlite:///{db_path}")
    with SyncSession(sync) as s:
        _insert_signal(s)
        _insert_trade(s, "hist1")
        s.commit()
    sync.dispose()
    client = client_env["client"]
    resp = client.get("/api/paper/trades", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    trades = body["trades"]
    assert len(trades) >= 1
    row = next(t for t in trades if t["id"] == "hist1")
    assert row["signal_id"] == SID
    assert row["ai_decision"] == "AVAILABLE"
    assert row["ai_score"] == 72.0
    assert row["original_entry"] == 100.0
    assert row["actual_exit"] == 107.0


def _capture_usage(real_fn, recorded, db_override=None):
    """Wrapper forwarding update_signal_usage to the real implementation with an
    explicit db_path (so the pytest guard can't block the temp-DB write)."""
    recorded["called"] = True

    def _fn(signal_id, quantity, risk_amount, risk_percent=None, db_path=None):
        recorded["args"] = (signal_id, quantity, risk_amount, risk_percent)
        return real_fn(signal_id, quantity, risk_amount, risk_percent,
                       db_path=db_path or db_override)

    return _fn


def test_http_order_to_close_carries_signal_id_and_ai_decision(client_env, monkeypatch):
    """Full HTTP lifecycle in a temp DB: Signal → Order → Fill → Close →
    persisted ledger → /paper/trades enrichment → /paper/trades/{id}/ai-decision.
    Pure read/write sandbox: the live ledger and signals table are untouched."""
    import app.api.trading as trading_mod
    from app.services.paper_trading import PaperTradingEngine

    db_path = client_env["engine"].url.database
    sqlite_path = str(db_path)
    # Seed the signal row in the temp DB.
    sync = create_engine(f"sqlite:///{sqlite_path}")
    with SyncSession(sync) as s:
        _insert_signal(s)
        s.commit()
    sync.dispose()

    # Point the API's module-level engine + usage-recorder at the temp DB.
    engine = PaperTradingEngine(db_path=sqlite_path, persist=True)
    monkeypatch.setattr(trading_mod, "paper_engine", engine)
    recorded: dict = {}
    monkeypatch.setattr(
        signal_store, "update_signal_usage",
        _capture_usage(signal_store.update_signal_usage, recorded, sqlite_path),
    )

    client = client_env["client"]
    headers = _auth()

    # 1) Place the AI-recommended order (signal_id links Signal → Order).
    order_resp = client.post("/api/paper/orders", json={
        "symbol": "TESTAI", "direction": "LONG", "quantity": 10,
        "entry_price": 100.0, "stop_loss": 98.0,
        "target_1": 105.0, "target_2": 107.0, "signal_id": SID,
    }, headers=headers)
    assert order_resp.status_code == 200, order_resp.text
    order_body = order_resp.json()
    assert "position" in order_body
    pos = order_body["position"]
    assert pos["signal_id"] == SID
    assert recorded.get("called") is True
    sid, qty, total_risk, risk_pct = recorded["args"]
    assert sid == SID and qty == 10
    assert total_risk == pytest.approx(20.0)          # 10 × |100 − 98| = 20
    portfolio = engine.get_portfolio_summary(user_id=USER)
    assert risk_pct == pytest.approx(round(total_risk / float(portfolio["total_value"]) * 100.0, 4))
    # The usage write persisted quantity / risk_amount / risk_percent to the
    # temp signals row (via the explicit db_path forwarding).
    con = sqlite3.connect(sqlite_path)
    usage = con.execute(
        "SELECT quantity, risk_amount, risk_percent FROM signals WHERE id = ?",
        (SID,)).fetchone()
    con.close()
    assert usage == (10, pytest.approx(total_risk), pytest.approx(risk_pct))

    # 2) Fill the position.
    fill = client.post(f"/api/paper/orders/{pos['id']}/fill",
                       json={"fill_price": 100.0}, headers=headers)
    assert fill.status_code == 200, fill.text

    # 3) Close at Target 2 — _finalize_paper_trade persists the ledger row
    #    carrying the SAME signal_id.
    close = client.post("/api/paper/close", json={
        "position_id": pos["id"], "exit_price": 107.0,
    }, headers=headers)
    assert close.status_code == 200, close.text
    assert close.json()["trade"]["signal_id"] == SID

    # 4) The persisted ledger row appears in /paper/trades with AI enrichment.
    hist = client.get("/api/paper/trades?limit=50", headers=headers)
    assert hist.status_code == 200
    row = next(t for t in hist.json()["trades"] if t["symbol"] == "TESTAI")
    assert row["signal_id"] == SID
    assert row["ai_decision"] == "AVAILABLE"
    assert row["ai_direction"] == "LONG"
    assert row["ai_score"] == 72.0
    assert row["original_entry"] == 100.0
    assert row["original_risk_percent"] == pytest.approx(risk_pct)
    assert row["actual_exit"] == 107.0
    assert row["realized_pnl"] == pytest.approx(70.0)

    # 5) The ai-decision endpoint returns the full recommendation + execution.
    ai = client.get(f"/api/paper/trades/{row['id']}/ai-decision", headers=headers)
    assert ai.status_code == 200
    body = ai.json()
    assert body["ai_decision"] == "AVAILABLE"
    assert body["signal"]["signal_score"] == 72.0
    assert body["original_setup"]["entry"] == 100.0
    assert body["original_setup"]["risk_percent"] == pytest.approx(risk_pct)
    assert body["execution"]["actual_exit"] == 107.0
    assert body["execution"]["realized_pnl"] == pytest.approx(70.0)
    assert len(body["lifecycle"]) == 1
    assert body["lifecycle_pnl"] == pytest.approx(70.0)