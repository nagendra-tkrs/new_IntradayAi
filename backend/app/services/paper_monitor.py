import asyncio
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

_monitor_task = None


async def _run_monitor_cycle(engine, get_quote, finalize, on_fill=None):
    """One monitoring pass over every account.

    For each account it fetches a single quote per unique symbol across its
    pending orders and open positions, marks prices (update_prices), auto-fills
    pending orders whose entry condition has been reached (check_entry_triggers),
    then evaluates SL/Target exits (check_stops). Failed quotes for one symbol
    never block other symbols or accounts. Auto-closes reuse the same close ->
    persist -> risk path as the manual close endpoint; auto-fills reuse the same
    fill lifecycle as the manual fill endpoint.
    """
    for account in list(engine.accounts.values()):
        symbols = {p["symbol"] for p in account.positions.values()}
        symbols |= {o["symbol"] for o in account.pending_orders.values()}
        if not symbols:
            continue
        prices = {}
        for symbol in symbols:
            try:
                quote = await get_quote(symbol)
            except Exception as e:
                logger.warning("Paper monitor: quote failed for %s: %s", symbol, e)
                continue
            try:
                price = float(quote.get("price", 0))
            except (TypeError, ValueError):
                price = 0.0
            if price and price > 0:
                prices[symbol] = price
        if not prices:
            continue
        account.update_prices(prices)
        fills = account.check_entry_triggers(prices)
        for result in fills:
            try:
                if on_fill is not None:
                    await on_fill(result, account.user_id)
            except Exception as e:
                logger.exception("Paper monitor: failed to finalize auto-fill: %s", e)
        if fills:
            account.update_prices(prices)
        exits = account.check_stops(prices)
        for result in exits:
            try:
                await finalize(result, account.user_id)
            except Exception as e:
                logger.exception("Paper monitor: failed to finalize auto-close: %s", e)
        # Write-through the restored/latest open positions + pending orders so
        # marks and auto-executions survive a restart (no-op on non-persistent
        # engines used by tests). Write reduction: price marks alone never set
        # an account dirty, so an idle cycle writes nothing.
        if account._dirty:
            try:
                engine.persist_snapshot(account.user_id)
            except Exception:
                logger.exception("Paper monitor: failed to persist account snapshot")


async def _monitor_loop(engine, get_quote, finalize, on_fill=None):
    while True:
        await asyncio.sleep(settings.PAPER_TRADING_MONITOR_INTERVAL_SECONDS)
        try:
            await _run_monitor_cycle(engine, get_quote, finalize, on_fill=on_fill)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Paper trading monitor cycle failed")


def start_monitor():
    """Start the background paper-trading monitor (entry-trigger auto-fill +
    SL/Target auto-close; idempotent)."""
    global _monitor_task
    if _monitor_task is not None and not _monitor_task.done():
        return _monitor_task
    # Imported lazily: trading.py instantiates the shared engine/provider and
    # defines the close finalizer; the monitor is started from app lifespan.
    from app.api.trading import paper_engine, risk_engine, _finalize_paper_trade, _get_provider

    provider = _get_provider()

    async def get_quote(symbol):
        return await provider.get_quote(symbol)

    async def on_fill(result, user_id):
        risk_engine.state.open_positions = len(paper_engine.get_positions(user_id=user_id))

    _monitor_task = asyncio.create_task(
        _monitor_loop(paper_engine, get_quote, _finalize_paper_trade, on_fill=on_fill)
    )
    logger.info(
        "Paper trading monitor started (interval=%ss)",
        settings.PAPER_TRADING_MONITOR_INTERVAL_SECONDS,
    )
    return _monitor_task


def stop_monitor():
    """Cancel the background paper-trading monitor (idempotent)."""
    global _monitor_task
    task = _monitor_task
    _monitor_task = None
    if task is not None and not task.done():
        task.cancel()