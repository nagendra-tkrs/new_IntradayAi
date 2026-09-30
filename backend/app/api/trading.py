import json
import uuid
from datetime import date, datetime
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import select
from app.services.paper_trading import PaperTradingEngine
from app.services.risk_engine import RiskEngine, RiskConfig
from app.services import signal_store
from app.services.performance_comparison import compare_legacy_vs_profit_capture
from app.services.context_report import build_context_comparison
from app.services.trade_setup import compute_risk_reward
from app.core.config import settings
from app.services.market_data.provider_factory import get_provider
from app.core.market_session import now_ist, is_market_hours, IST
from app.core.database import get_db
from app.models.models import User
from app.api.auth import get_current_user

router = APIRouter(prefix="/api", tags=["trading"])
paper_engine = PaperTradingEngine(persist=True)
risk_engine = RiskEngine()

MAX_DUPLICATE_POSITIONS = 1

_provider = None

def _get_provider():
    global _provider
    if _provider is None:
        _provider = get_provider()
    return _provider


def _parse_dt(value):
    if not value:
        return None
    from datetime import datetime
    return datetime.fromisoformat(value)


class OrderRequest(BaseModel):
    symbol: str
    direction: str
    quantity: int
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target_1: Optional[float] = None
    target_2: Optional[float] = None
    # Optional quality metadata for risk-based sizing. The paper-trading UI does
    # NOT send these today (scanner-only), so legacy manual orders are
    # unaffected; only callers that supply them opt into quality sizing.
    setup_quality: Optional[str] = None
    signal_strength: Optional[str] = None
    atr: Optional[float] = None
    # Traceability: the AI signal (signals.id) this order originates from. The
    # scanner pages send it so Signal → Paper Order → Position → Exit → P&L can
    # be joined. Legacy/manual orders omit it (None) and are unaffected.
    signal_id: Optional[str] = None


class OrderEditRequest(BaseModel):
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target_1: Optional[float] = None
    target_2: Optional[float] = None
    quantity: Optional[int] = None


class FillOrderRequest(BaseModel):
    fill_price: Optional[float] = None


class ClosePositionRequest(BaseModel):
    position_id: str
    exit_price: Optional[float] = None


def _paper_details(trade_dict: dict) -> str:
    """Serialize the additive profit-capture fields of a realized trade row
    into the ``trades.details_json`` column. Informational only — P&L and the
    ledger columns stay the authoritative accounting source."""
    keys = (
        "exit_reason", "position_id", "exit_quantity", "initial_quantity",
        "remaining_quantity", "t1_exit_price", "t1_exit_quantity",
        "t1_realized_pnl", "realized_pnl", "trailing_active", "atr_ref",
        "exit_stage",
    )
    details = {k: trade_dict.get(k) for k in keys if trade_dict.get(k) is not None}
    if not details:
        return ""
    details["result"] = trade_dict.get("result")
    return json.dumps(details, default=str)


async def _finalize_paper_trade(result: dict, user_id: str):
    """Shared close-finalization used by the manual close endpoint AND the
    automatic SL/Target monitor: persists the closed trade to SQLite and updates
    the shared risk engine (daily loss / consecutive-loss / position count).

    A T1 PARTIAL exit persists its own ledger row (unique id, shared
    position_id in details_json) and feeds ``daily_pnl`` but NEVER bumps the
    daily-trade count or loss streak — only full closes call record_trade_result."""
    try:
        from app.core.database import async_session
        from app.models.models import Trade
        trade_dict = result.get("trade", {})
        is_partial = bool(result.get("partial") or trade_dict.get("exit_reason") == "T1_PARTIAL")
        trade_id = trade_dict.get("id", "")
        if is_partial or not trade_id:
            trade_id = uuid.uuid4().hex[:16]
        exec_qty = trade_dict.get("exit_quantity") or trade_dict.get("quantity", 1)
        async with async_session() as db:
            db_trade = Trade(
                id=trade_id,
                user_id=user_id,
                symbol=trade_dict.get("symbol", ""),
                direction=trade_dict.get("direction", ""),
                entry_price=trade_dict.get("entry_price", 0.0),
                exit_price=trade_dict.get("exit_price", 0.0),
                quantity=exec_qty,
                stop_loss=trade_dict.get("stop_loss", 0.0),
                target_1=trade_dict.get("target_1", 0.0),
                target_2=trade_dict.get("target_2", 0.0),
                entry_time=_parse_dt(trade_dict.get("opened_at", "")),
                exit_time=_parse_dt(trade_dict.get("exit_time", "")),
                status=trade_dict.get("status", "closed"),
                pnl=trade_dict.get("pnl", 0.0),
                fees=trade_dict.get("fees", 0.0),
                slippage=trade_dict.get("slippage", 0.0),
                signal_id=trade_dict.get("signal_id"),
                details_json=_paper_details(trade_dict) or None,
            )
            db.add(db_trade)
            await db.commit()
    except Exception as e:
        # Log but do not break the close flow; trade still in closed_trades
        print(f"Failed to persist trade to SQLite: {e}")
    pnl = result.get("pnl", 0)
    # Traceability: mark the linked signals row's outcome (WIN/LOSS/BREAKEVEN,
    # realized P&L, holding minutes) when the closing trade references a
    # signal_id. Never blocks the close flow (wrapped defensively).
    #
    # ATTRIBUTION CONTRACT: `signals.realized_pnl` is the POSITION total, not
    # the exit slice. A Profit-Capture position writes one ledger row per slice
    # (T1_PARTIAL, then T2_FINAL / TRAILING_STOP) and all of them share one
    # signal_id, so passing the slice P&L here meant the signal ended up holding
    # only the LAST close's P&L and understating the position by every prior
    # partial. `close_position` now carries the position-level `realized_pnl`;
    # fall back to the slice only if an older caller omitted it, so a
    # single-close position is still attributed exactly.
    try:
        close_trade = result.get("trade", {})
        signal_id = close_trade.get("signal_id")
        if signal_id:
            holding_min = 0
            opened = _parse_dt(close_trade.get("opened_at"))
            exited = _parse_dt(close_trade.get("exit_time"))
            if opened and exited:
                holding_min = int(max((exited - opened).total_seconds(), 0) // 60)
            position_pnl = close_trade.get("realized_pnl")
            if position_pnl is None:
                position_pnl = result.get("realized_pnl")
            attributed_pnl = pnl if position_pnl is None else position_pnl
            signal_store.mark_signal_outcome(
                signal_id, float(attributed_pnl), holding_min
            )
            # Mirror the same attributed figure into the shadow ledger so a
            # future Profit Selection promotion can weigh the layer's
            # SKIP decisions against what the trade it did not block earned.
            try:
                from app.services.profit_selection_store import (
                    attach_realized_outcome,
                )

                attributed_outcome = (
                    "WIN" if attributed_pnl > 0
                    else ("LOSS" if attributed_pnl < 0 else "BREAKEVEN")
                )
                attach_realized_outcome(
                    signal_id, float(attributed_pnl), attributed_outcome,
                    trade_id=close_trade.get("id"),
                )
            except Exception:
                pass
    except Exception:
        pass
    if is_partial:
        # Partials feed the daily loss limit but never count as a closed trade
        # for the daily-trade cap or the consecutive-loss cooldown.
        risk_engine.state.daily_pnl += pnl
    else:
        risk_engine.record_trade_result(pnl)
    risk_engine.state.open_positions = len(paper_engine.get_positions(user_id=user_id))


@router.get("/portfolio")
async def get_portfolio(user: User = Depends(get_current_user)):
    summary = paper_engine.get_portfolio_summary(user_id=user.id)
    positions = paper_engine.get_positions(user_id=user.id)
    return {**summary, "positions": positions}


@router.post("/paper/orders")
async def place_paper_order(order: OrderRequest, user: User = Depends(get_current_user)):
    open_positions = paper_engine.get_positions(user_id=user.id)
    if len(open_positions) >= risk_engine.config.max_simultaneous_positions:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum simultaneous positions ({risk_engine.config.max_simultaneous_positions}) reached",
        )

    open_symbols = {p["symbol"] for p in open_positions}
    pending_symbols = {p["symbol"] for p in paper_engine.get_pending_orders(user_id=user.id)}
    all_symbols = open_symbols | pending_symbols
    if order.symbol in all_symbols:
        raise HTTPException(
            status_code=400,
            detail=f"You already have an open or pending position in {order.symbol}",
        )

    can, reason = risk_engine.can_trade()
    if not can:
        raise HTTPException(status_code=400, detail=reason)

    entry_price = order.entry_price
    if entry_price is None:
        quote = await _get_provider().get_quote(order.symbol)
        entry_price = quote["price"]

    stop_loss = order.stop_loss or (entry_price * 0.97 if order.direction == "LONG" else entry_price * 1.03)
    target_1 = order.target_1 or (entry_price * 1.04 if order.direction == "LONG" else entry_price * 0.96)

    direction_label = "BUY" if order.direction.upper() in ("LONG", "BUY") else "SELL"
    setup_result = compute_risk_reward(entry_price, stop_loss, target_1, direction=direction_label)
    if not setup_result.valid:
        raise HTTPException(status_code=400, detail="; ".join(setup_result.errors))

    valid, msg = risk_engine.validate_setup(entry_price, stop_loss, target_1, order.direction)
    if not valid:
        raise HTTPException(status_code=400, detail=msg)

    qty = order.quantity
    if qty <= 0:
        qty = risk_engine.calculate_position_size(
            entry_price,
            stop_loss,
            setup_quality=order.setup_quality,
            signal_strength=order.signal_strength,
        )
    if qty <= 0:
        if order.setup_quality:
            raise HTTPException(
                status_code=400,
                detail=f"Order rejected: setup quality '{order.setup_quality}' permits no position"
                      " (REJECTED/WEAK quality never trades by default)",
            )
        raise HTTPException(status_code=400, detail="Position size is zero")

    result = paper_engine.place_order(
        symbol=order.symbol,
        direction=order.direction,
        quantity=qty,
        entry_price=entry_price,
        stop_loss=stop_loss,
        target_1=target_1,
        target_2=order.target_2 or (entry_price * 1.06 if order.direction == "LONG" else entry_price * 0.94),
        user_id=user.id,
        atr=order.atr,
        signal_id=order.signal_id,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    # Record order-usage on the linked signals row (quantity + risk amount +
    # effective risk percent of portfolio value at order time).
    if order.signal_id and "position" in result:
        risk_per_share = abs(
            float(result["position"].get("entry_price", entry_price) or entry_price)
            - float(result["position"].get("stop_loss", stop_loss) or stop_loss)
        )
        total_risk = round(qty * risk_per_share, 2)
        risk_pct = None
        try:
            portfolio = paper_engine.get_portfolio_summary(user_id=user.id)
            total_value = float(portfolio.get("total_value") or 0)
            if total_value > 0:
                risk_pct = round(total_risk / total_value * 100.0, 4)
        except Exception:
            risk_pct = None
        signal_store.update_signal_usage(order.signal_id, qty, total_risk, risk_pct)
    return result


@router.patch("/paper/orders/{order_id}")
async def edit_paper_order(order_id: str, payload: OrderEditRequest, user: User = Depends(get_current_user)):
    order = paper_engine.get_pending_order(order_id, user_id=user.id)
    if not order:
        raise HTTPException(status_code=404, detail="Pending order not found")
    if order.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Cannot edit: order is already filled or closed")
    if order.get("user_id") and order["user_id"] != user.id:
        raise HTTPException(status_code=403, detail="Cannot edit another user's order")

    entry_price = payload.entry_price if payload.entry_price is not None else order["entry_price"]
    stop_loss = payload.stop_loss if payload.stop_loss is not None else order["stop_loss"]
    target_1 = payload.target_1 if payload.target_1 is not None else order["target_1"]
    quantity = payload.quantity if payload.quantity is not None else order["quantity"]

    direction_label = "BUY" if order["direction"].upper() in ("LONG", "BUY") else "SELL"
    setup_result = compute_risk_reward(entry_price, stop_loss, target_1, direction=direction_label)
    if not setup_result.valid:
        raise HTTPException(status_code=400, detail="; ".join(setup_result.errors))

    valid, msg = risk_engine.validate_setup(entry_price, stop_loss, target_1, order["direction"])
    if not valid:
        raise HTTPException(status_code=400, detail=msg)

    if quantity <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be positive")

    result = paper_engine.edit_order(
        order_id, user_id=user.id,
        entry_price=payload.entry_price, stop_loss=payload.stop_loss,
        target_1=payload.target_1, target_2=payload.target_2,
        quantity=payload.quantity,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/paper/orders/{order_id}/fill")
async def fill_paper_order(order_id: str, req: FillOrderRequest = FillOrderRequest(), user: User = Depends(get_current_user)):
    order = paper_engine.get_pending_order(order_id, user_id=user.id)
    if not order:
        raise HTTPException(status_code=404, detail="Pending order not found")
    if order.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Order is already filled or closed")
    if order.get("user_id") and order["user_id"] != user.id:
        raise HTTPException(status_code=403, detail="Cannot fill another user's order")

    fill_price = req.fill_price
    if fill_price is None:
        try:
            quote = await _get_provider().get_quote(order["symbol"])
            fill_price = quote["price"]
        except Exception:
            fill_price = order["entry_price"]

    result = paper_engine.fill_order(order_id, fill_price=fill_price, user_id=user.id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    risk_engine.state.open_positions = len(paper_engine.get_positions(user_id=user.id))
    return result


@router.delete("/paper/orders/{order_id}")
async def cancel_paper_order(order_id: str, user: User = Depends(get_current_user)):
    order = paper_engine.get_pending_order(order_id, user_id=user.id)
    if not order:
        raise HTTPException(status_code=404, detail="Pending order not found")
    if order.get("user_id") and order["user_id"] != user.id:
        raise HTTPException(status_code=403, detail="Cannot cancel another user's order")
    result = paper_engine.cancel_order(order_id, user_id=user.id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/paper/pending")
async def get_pending_orders(user: User = Depends(get_current_user)):
    orders = paper_engine.get_pending_orders(user_id=user.id)
    return {"orders": orders}


@router.post("/paper/close")
async def close_paper_position(req: ClosePositionRequest, user: User = Depends(get_current_user)):
    exit_price = req.exit_price
    if exit_price is None:
        pos = paper_engine.get_position(req.position_id, user_id=user.id)
        if pos:
            quote = await _get_provider().get_quote(pos["symbol"])
            exit_price = quote["price"]
    if exit_price is None:
        raise HTTPException(status_code=400, detail="Cannot determine exit price")
    result = paper_engine.close_position(req.position_id, exit_price, user_id=user.id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    await _finalize_paper_trade(result, user.id)
    return result


@router.get("/paper/positions")
async def get_positions(user: User = Depends(get_current_user)):
    return {"positions": paper_engine.get_positions(user_id=user.id)}


def _trade_rows_to_dicts(db_trades) -> list[dict]:
    """Serialize SQLAlchemy Trade rows to the paper-trade dict shape shared by
    /paper/trades and /paper/performance so both use the same source of truth
    (the persistent SQLite ledger)."""
    trades = []
    for t in db_trades:
        detail = {}
        if getattr(t, "details_json", None):
            try:
                detail = json.loads(t.details_json) or {}
            except (TypeError, ValueError):
                detail = {}
        trades.append({
            "id": t.id,
            "symbol": t.symbol,
            "direction": t.direction,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "quantity": t.quantity,
            "stop_loss": t.stop_loss,
            "target_1": t.target_1,
            "target_2": t.target_2,
            "entry_time": str(t.entry_time) if t.entry_time else "",
            "exit_time": str(t.exit_time) if t.exit_time else "",
            "pnl": t.pnl,
            "result": t.pnl > 0 and "WIN" or (t.pnl < 0 and "LOSS" or "BREAKEVEN"),
            "status": t.status,
            # Traceability: the AI recommendation this trade originated from
            # (NULL for manual orders and pre-fix historical trades).
            "signal_id": getattr(t, "signal_id", None),
            # Execution aliases (AI-vs-actual comparison; additive).
            "actual_entry": t.entry_price,
            "actual_exit": t.exit_price,
            "realized_pnl": t.pnl,
            "unrealized_pnl": 0.0,
            # Profit-capture extras (additive; empty on legacy rows).
            "exit_reason": detail.get("exit_reason"),
            "position_id": detail.get("position_id"),
            "exit_quantity": detail.get("exit_quantity"),
            "initial_quantity": detail.get("initial_quantity"),
            "remaining_quantity": detail.get("remaining_quantity"),
            "t1_exit_price": detail.get("t1_exit_price"),
            "t1_exit_quantity": detail.get("t1_exit_quantity"),
            "t1_realized_pnl": detail.get("t1_realized_pnl"),
        })
    return trades


async def _load_trades_from_db(user_id: str) -> list[dict]:
    """Load a user's closed paper-trade ledger and attach the persisted AI
    recommendation snapshot (when the trade's ``signal_id`` resolves).

    Read-only: the ledger's stored values are the source of truth; anything
    about the original recommendation comes from the joined ``signals`` row —
    never from current market data.
    """
    from app.core.database import async_session
    from app.models.models import Signal, Trade
    async with async_session() as db:
        stmt = select(Trade).where(Trade.user_id == user_id).order_by(Trade.exit_time.desc())
        result = await db.execute(stmt)
        trades = _trade_rows_to_dicts(result.scalars().all())
        signal_ids = {t["signal_id"] for t in trades if t.get("signal_id")}
        signals = {}
        if signal_ids:
            sigs = (
                await db.execute(select(Signal).where(Signal.id.in_(signal_ids)))
            ).scalars().all()
            signals = {s.id: s for s in sigs}
        return _attach_ai_decisions(trades, signals)


def _signal_components(signal) -> dict:
    """Parse the persisted indicator_scores JSON of a signals row into a flat
    dict (empty when absent or malformed)."""
    payload = {}
    raw = getattr(signal, "indicator_scores", None)
    if raw:
        try:
            payload = json.loads(raw) or {}
        except (TypeError, ValueError):
            payload = {}
    return payload


def _attach_ai_decisions(trades: list[dict], signals_by_id: dict) -> list[dict]:
    """Attach the persisted AI-recommendation snapshot to each trade dict.

    Trades whose ``signal_id`` resolves attach the ORIGINAL recommendation
    (direction/score/quality/components/setup) read from the signals row —
    never recomputed from current market data. Manual and pre-fix historical
    trades (no signal_id, or a missing row) stay ``ai_decision =
    NOT_AVAILABLE``. Only additive keys are introduced; legacy contract keys
    are left untouched.
    """
    for t in trades:
        sid = t.get("signal_id")
        sig = signals_by_id.get(sid) if sid else None
        if sig is None:
            t["ai_available"] = False
            t["ai_decision"] = "NOT_AVAILABLE"
            continue
        comp = _signal_components(sig)
        t["ai_available"] = True
        t["ai_decision"] = "AVAILABLE"
        t["ai_signal_id"] = sig.id
        t["ai_signal_timestamp"] = str(sig.timestamp) if sig.timestamp else None
        t["ai_direction"] = sig.direction
        t["ai_score"] = sig.signal_score
        t["signal_quality"] = sig.signal_quality
        t["ai_confidence"] = sig.confidence
        t["ai_strategy"] = sig.strategy
        t["ai_strategy_version"] = sig.strategy_version
        t["ai_data_source"] = sig.data_source
        t["ai_atr"] = sig.atr
        t["ai_trend_score"] = comp.get("trend_score")
        t["ai_momentum_score"] = comp.get("momentum_score")
        t["ai_volume_score"] = comp.get("volume_score")
        t["ai_vwap_score"] = comp.get("vwap_score")
        t["ai_price_action_score"] = comp.get("price_action_score")
        t["ai_market_context_score"] = comp.get("market_context_score")
        t["ai_risk_quality_score"] = comp.get("risk_quality_score")
        t["original_entry"] = sig.entry_price
        t["original_stop_loss"] = sig.stop_loss
        t["original_target_1"] = sig.target_1
        t["original_target_2"] = sig.target_2
        t["original_risk_reward"] = sig.risk_reward
        t["original_quantity"] = sig.quantity
        t["original_risk_amount"] = sig.risk_amount
        t["original_risk_percent"] = sig.risk_percent
        t["ai_evidence"] = {
            "nets": comp.get("nets"),
            "net": comp.get("net"),
            "long_total": comp.get("long_total"),
            "short_total": comp.get("short_total"),
            "conflict": comp.get("conflict"),
            "risk_quality": comp.get("evidence_risk_quality"),
        }
    return trades


def _exit_ist_date(trade: dict) -> Optional[date]:
    """IST calendar date on which a trade was REALIZED (its exit/close date).

    Realized P&L belongs to the date the trade completed, so date-wise history
    groups by ``exit_time`` — never by entry. The ledger stores
    ``now_ist().isoformat()`` wall-clock timestamps and the SQLite DateTime
    column serializes them without an offset (e.g. "2026-09-25 09:35:00"), so
    naive values are interpreted as IST wall-clock — the same convention the
    rest of the app uses for trade dates (see ``_bus_date``). Trades without a
    usable exit timestamp return None: they are never grouped (and never crash).
    """
    raw = trade.get("exit_time") or ""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        # Ledger values are naive IST wall-clock (SQLite strips the offset).
        dt = dt.replace(tzinfo=IST)
    else:
        # Aware values (e.g. a provider/timestamp with an offset) convert to
        # IST so the calendar date never shifts to another Indian trading day.
        dt = dt.astimezone(IST)
    return dt.date()


def _is_realized(trade: dict) -> bool:
    """Ledger rows are completed/closed trades. Legacy rows without a status
    are treated as closed; OPEN positions (which live under /paper/positions)
    are never mixed into realized trade history."""
    return trade.get("status") in (None, "", "closed")


def _aggregate_trades(trades: list[dict]) -> dict:
    """Date-wise P&L aggregates over realized trades.

    Uses the ledger's authoritative ``pnl`` field for every trade — the same
    value the paper account, portfolio, and performance modules read — and
    never recomputes P&L from entry/exit prices. Zero-P&L trades are counted
    in ``total_trades`` but in neither the profitable nor the losing bucket
    (the ledger's ``result`` "BREAKEVEN" is derived from the same rule).
    """
    realized = [t for t in trades if _is_realized(t) and _exit_ist_date(t) is not None]
    profitable = 0
    losing = 0
    total_profit = 0.0
    total_loss = 0.0
    for t in realized:
        pnl = t.get("pnl", 0.0) or 0.0
        if pnl > 0:
            profitable += 1
            total_profit += pnl
        elif pnl < 0:
            losing += 1
            total_loss += -pnl
    total_profit = round(total_profit, 2)
    total_loss = round(total_loss, 2)
    return {
        "total_trades": len(realized),
        "profitable_trade_count": profitable,
        "losing_trade_count": losing,
        "total_profit": total_profit,
        "total_loss": total_loss,
        "net_pnl": round(total_profit - total_loss, 2),
    }


def _build_date_groups(trades: list[dict]) -> list[dict]:
    """Group realized trades by IST exit date, newest date first.

    Each group mirrors ``_aggregate_trades`` (``trade_count`` /
    ``profitable_trade_count`` / ``losing_trade_count`` / ``total_profit`` /
    ``total_loss`` / ``net_pnl``) plus the day's ``trades``, which keep the
    ledger's order (most recent exit first). Trades with no usable exit
    timestamp are excluded so "All Dates" aggregates always equal the sum of
    the date groups.
    """
    by_date: dict[date, list[dict]] = {}
    for t in trades:
        if not _is_realized(t):
            continue
        d = _exit_ist_date(t)
        if d is None:
            continue
        by_date.setdefault(d, []).append(t)
    groups = []
    for d in sorted(by_date.keys(), reverse=True):
        day_trades = by_date[d]
        agg = _aggregate_trades(day_trades)
        groups.append({
            "date": d.isoformat(),
            "trade_count": agg["total_trades"],
            "profitable_trade_count": agg["profitable_trade_count"],
            "losing_trade_count": agg["losing_trade_count"],
            "total_profit": agg["total_profit"],
            "total_loss": agg["total_loss"],
            "net_pnl": agg["net_pnl"],
            "trades": day_trades,
        })
    return groups


def _parse_filter_date(value: str) -> Optional[date]:
    """Parse a YYYY-MM-DD (IST) history filter; None when malformed."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _build_trade_history_response(full: list[dict], limit: int, filter_date: Optional[date] = None) -> dict:
    """Assemble the /paper/trades response.

    ``trades`` keeps its legacy contract: the flat realized ledger, most
    recent first, capped by ``limit`` (when no ``filter_date``). The added
    ``date_groups`` and ``summary`` are read-only reporting fields computed
    from the SAME authoritative ``pnl`` values; open positions never appear.
    """
    if filter_date is None:
        trades = full[:limit]
        source = full
    else:
        trades = [t for t in full if _exit_ist_date(t) == filter_date]
        source = trades
    return {
        "trades": trades,
        "date_groups": _build_date_groups(source),
        "summary": _aggregate_trades(source),
    }


@router.get("/paper/trades")
async def get_trade_history(limit: int = 50, date: Optional[str] = None, user: User = Depends(get_current_user)):
    """Closed paper-trade ledger with date-wise P&L reporting.

    The existing response shape is preserved (``trades`` unchanged when
    ``date`` is omitted). Two read-only fields are added — no stored record,
    position, cash, margin, or order state is modified:

      - ``date_groups`` — realized trades grouped by IST exit (realization)
        date, newest date first; each group carries ``trade_count``,
        ``profitable_trade_count``, ``losing_trade_count``, ``total_profit``,
        ``total_loss``, ``net_pnl`` and the day's ``trades``.
      - ``summary`` — the same aggregates across All Dates.

    ``date=YYYY-MM-DD`` (IST) narrows every field to trades realized on that
    date (a date with no completed trades yields empty groups and a zero
    summary — never a fabricated record).
    """
    full = await _load_trades_from_db(user.id)
    return _build_trade_history_response(full, limit, _parse_filter_date(date) if date else None)


def _ai_execution_view(trade: dict) -> dict:
    """Actual execution / exit facts for one ledger row — never the original
    AI setup (which stays in ``original_setup``)."""
    return {
        "actual_entry": trade.get("actual_entry"),
        "actual_exit": trade.get("actual_exit"),
        "quantity": trade.get("exit_quantity") or trade.get("quantity"),
        "opened_at": trade.get("entry_time"),
        "exit_time": trade.get("exit_time"),
        "realized_pnl": trade.get("realized_pnl"),
        "unrealized_pnl": trade.get("unrealized_pnl"),
        "exit_reason": trade.get("exit_reason"),
        "status": trade.get("status"),
        "result": trade.get("result"),
    }


@router.get("/paper/trades/{trade_id}/ai-decision")
async def get_trade_ai_decision(trade_id: str, user: User = Depends(get_current_user)):
    """Read-only AI Decision detail for one closed paper trade.

    Returns the ORIGINAL persistent recommendation (direction / score /
    quality / component breakdown / setup) that produced this trade, the
    actual execution that happened afterwards, and — for profit-capture flows
    — every ledger row originating from the same position (T1 partial +
    T2-final / trailing), all carrying the same signal_id.

    This endpoint NEVER places, modifies, cancels or closes orders, and it
    NEVER fabricates a recommendation: trades without a resolvable signal_id
    (manual orders, pre-fix historical rows) return ``ai_decision =
    NOT_AVAILABLE`` with an explanatory reason.
    """
    trades = await _load_trades_from_db(user.id)
    trade = next((t for t in trades if t["id"] == trade_id), None)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    if not trade.get("ai_available"):
        return {
            "trade_id": trade_id,
            "ai_decision": "NOT_AVAILABLE",
            "reason": "Signal was not persisted when this trade was created.",
            "execution": _ai_execution_view(trade),
        }
    pos_id = trade.get("position_id") or trade.get("id")
    lifecycle = [
        t for t in trades if (t.get("position_id") or t.get("id")) == pos_id
    ]
    lifecycle = sorted(lifecycle, key=lambda t: t.get("exit_time") or "")
    return {
        "trade_id": trade_id,
        "ai_decision": "AVAILABLE",
        "signal": {
            "id": trade.get("ai_signal_id"),
            "symbol": trade.get("symbol"),
            "timestamp": trade.get("ai_signal_timestamp"),
            "direction": trade.get("ai_direction"),
            "signal_score": trade.get("ai_score"),
            "confidence": trade.get("ai_confidence"),
            "signal_quality": trade.get("signal_quality"),
            "strategy": trade.get("ai_strategy"),
            "strategy_version": trade.get("ai_strategy_version"),
            "data_source": trade.get("ai_data_source"),
        },
        "score_breakdown": {
            "trend_score": trade.get("ai_trend_score"),
            "momentum_score": trade.get("ai_momentum_score"),
            "volume_score": trade.get("ai_volume_score"),
            "vwap_score": trade.get("ai_vwap_score"),
            "price_action_score": trade.get("ai_price_action_score"),
            "market_context_score": trade.get("ai_market_context_score"),
            "risk_quality_score": trade.get("ai_risk_quality_score"),
            "total": trade.get("ai_score"),
        },
        "evidence": trade.get("ai_evidence") or {},
        "original_setup": {
            "entry": trade.get("original_entry"),
            "stop_loss": trade.get("original_stop_loss"),
            "target_1": trade.get("original_target_1"),
            "target_2": trade.get("original_target_2"),
            "risk_reward": trade.get("original_risk_reward"),
            "atr": trade.get("ai_atr"),
            "quantity": trade.get("original_quantity"),
            "risk_amount": trade.get("original_risk_amount"),
            "risk_percent": trade.get("original_risk_percent"),
        },
        "execution": _ai_execution_view(trade),
        "lifecycle": [
            {
                "trade_id": t["id"],
                "symbol": t["symbol"],
                "direction": t["direction"],
                "entry_price": t.get("entry_price"),
                "exit_price": t.get("exit_price"),
                "quantity": t.get("exit_quantity") or t.get("quantity"),
                "exit_time": t.get("exit_time"),
                "pnl": t.get("pnl"),
                "exit_reason": t.get("exit_reason"),
            }
            for t in lifecycle
        ],
        "lifecycle_pnl": round(sum(float(t.get("pnl") or 0) for t in lifecycle), 2),
    }


@router.get("/paper/performance")
async def get_performance(user: User = Depends(get_current_user)):
    trades = await _load_trades_from_db(user.id)
    now = now_ist()
    daily_trades = []
    weekly_trades = []
    monthly_trades = []
    for t in trades:
        exit_time = t.get("exit_time", "")
        if not exit_time:
            continue
        try:
            from datetime import datetime
            trade_dt = datetime.fromisoformat(exit_time)
            if trade_dt.date() == now.date():
                daily_trades.append(t)
            if (now - trade_dt).days < 7:
                weekly_trades.append(t)
            if trade_dt.month == now.month and trade_dt.year == now.year:
                monthly_trades.append(t)
        except Exception:
            pass

    def calc_stats(trade_list):
        if not trade_list:
            return {"count": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0, "avg_pnl": 0}
        wins = sum(1 for t in trade_list if t.get("pnl", 0) > 0)
        losses = sum(1 for t in trade_list if t.get("pnl", 0) < 0)
        total_pnl = sum(t.get("pnl", 0) for t in trade_list)
        return {
            "count": len(trade_list),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(trade_list) * 100, 1) if trade_list else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(trade_list), 2) if trade_list else 0,
        }

    return {
        "daily": calc_stats(daily_trades),
        "weekly": calc_stats(weekly_trades),
        "monthly": calc_stats(monthly_trades),
        "all_time": calc_stats(trades),
    }


@router.get("/paper/performance/comparison")
async def get_performance_comparison(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    user: User = Depends(get_current_user),
):
    """Read-only Legacy vs Profit Capture comparison over the user's REALIZED
    paper-trading ledger.

    For every completed trade the hypothetical legacy result (100% exit at the
    T1 price, or the same stop-loss/manual path when T1 was never reached) is
    computed on the fly from the recorded entry/quantity/T1 — no second order
    is created, no fake legacy row is written, and no stored position or
    accounting value is touched. The recorded Profit Capture rows remain the
    source of truth.

    ``start_date`` / ``end_date`` (YYYY-MM-DD IST, inclusive, both optional)
    filter by the IST realization (final exit) date — the same date axis the
    /paper/trades history uses. Lacking enough data, ``has_data`` is false and
    the UI shows \"Insufficient data for performance comparison.\"
    """
    trades = await _load_trades_from_db(user.id)
    return compare_legacy_vs_profit_capture(
        trades,
        start_date=start_date,
        end_date=end_date,
    )


@router.get("/paper/performance/context-comparison")
async def get_context_comparison(user: User = Depends(get_current_user)):
    """Read-only Current (Mode A) vs 24H-context (Mode B) comparison.

    Decision-space numbers come from the persisted signals ledger and the
    signal_24h_comparisons shadow ledger; realized P&L comes from the user's
    paper-trading trades ledger. The 24H side never executes — its realized
    P&L is reported as pending (never estimated/backfilled).

    Empty decision spaces simply report ``has_data: false`` with caveats — no
    profitability claim is ever made without real recorded data.
    """
    from app.core.database import async_session
    from app.models.models import Signal, Signal24hComparison

    trades = await _load_trades_from_db(user.id)
    current_signals: list[dict] = []
    comparisons: list[dict] = []
    async with async_session() as db:
        # signals rows are global (no user scoping — the recommendation ledger
        # is symbol × decision candle), so the query selects all signal rows.
        rows = (await db.execute(
            select(Signal).order_by(Signal.timestamp.desc()).limit(5000)
        )).scalars().all()
        for s in rows:
            current_signals.append({
                "symbol": s.symbol,
                "candle_ts": str(s.timestamp),
                "direction": s.direction,
                "score": s.signal_score,
                "confidence": s.confidence,
                "quality": s.signal_quality,
                "outcome": s.outcome,
                "realized_pnl": s.realized_pnl,
            })
        comp_rows = (await db.execute(
            select(Signal24hComparison).order_by(Signal24hComparison.candle_ts.desc()).limit(5000)
        )).scalars().all()
        for c in comp_rows:
            comparisons.append({
                "symbol": c.symbol,
                "candle_ts": str(c.candle_ts),
                "current_direction": c.current_direction,
                "ctx24_direction": c.ctx24_direction,
                "current_score": c.current_score,
                "ctx24_score": c.ctx24_score,
                "ctx24_quality": c.ctx24_quality,
            })
    return build_context_comparison(
        current_signals,
        comparisons,
        trades,
        use_24h_context=settings.USE_24H_CONTEXT,
        window_hours=settings.CONTEXT_24H_WINDOW_HOURS,
    )
