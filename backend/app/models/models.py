import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Float, Integer, DateTime, Text, Boolean, ForeignKey, Index
)
from sqlalchemy.orm import relationship
from app.core.database import Base


def gen_id() -> str:
    return uuid.uuid4().hex[:16]


class User(Base):
    __tablename__ = "users"
    id = Column(String(16), primary_key=True, default=gen_id)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    password_hash = Column(String(255), nullable=True)
    picture = Column(String(512), nullable=True)
    auth_provider = Column(String(20), default="google")
    last_login = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Instrument(Base):
    __tablename__ = "instruments"
    id = Column(String(16), primary_key=True, default=gen_id)
    symbol = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(255))
    exchange = Column(String(10), default="NSE")
    sector = Column(String(100))
    is_active = Column(Boolean, default=True)
    universe = Column(String(50), default="NIFTY50")


class MarketData(Base):
    __tablename__ = "market_data"
    id = Column(String(16), primary_key=True, default=gen_id)
    symbol = Column(String(50), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Integer, nullable=False)
    timeframe = Column(String(10), default="5m")

    __table_args__ = (
        Index("ix_market_data_symbol_ts", "symbol", "timestamp"),
    )


class Signal(Base):
    __tablename__ = "signals"
    id = Column(String(16), primary_key=True, default=gen_id)
    symbol = Column(String(50), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False)
    direction = Column(String(20), nullable=False)
    signal_score = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    entry_price = Column(Float)
    stop_loss = Column(Float)
    target_1 = Column(Float)
    target_2 = Column(Float)
    risk_reward = Column(Float)
    strategy = Column(String(100))
    explanation = Column(Text)
    indicator_scores = Column(Text)
    market_context = Column(Text)
    data_source = Column(String(20), default="mock")
    outcome = Column(String(20), default="pending")
    realized_pnl = Column(Float, default=0.0)
    holding_duration = Column(Integer, default=0)


class Trade(Base):
    __tablename__ = "trades"
    id = Column(String(16), primary_key=True, default=gen_id)
    signal_id = Column(String(16), ForeignKey("signals.id"))
    symbol = Column(String(50), nullable=False, index=True)
    direction = Column(String(10), nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float)
    quantity = Column(Integer, nullable=False)
    stop_loss = Column(Float)
    target_1 = Column(Float)
    target_2 = Column(Float)
    entry_time = Column(DateTime, nullable=False)
    exit_time = Column(DateTime)
    status = Column(String(20), default="open")
    pnl = Column(Float, default=0.0)
    fees = Column(Float, default=0.0)
    slippage = Column(Float, default=0.0)


class Portfolio(Base):
    __tablename__ = "portfolio"
    id = Column(String(16), primary_key=True, default=gen_id)
    cash = Column(Float, nullable=False)
    total_value = Column(Float, nullable=False)
    total_pnl = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=datetime.utcnow)


class Position(Base):
    __tablename__ = "positions"
    id = Column(String(16), primary_key=True, default=gen_id)
    symbol = Column(String(50), nullable=False, index=True)
    direction = Column(String(10), nullable=False)
    quantity = Column(Integer, nullable=False)
    entry_price = Column(Float, nullable=False)
    current_price = Column(Float, default=0.0)
    stop_loss = Column(Float)
    target_1 = Column(Float)
    target_2 = Column(Float)
    unrealized_pnl = Column(Float, default=0.0)
    opened_at = Column(DateTime, nullable=False)
    status = Column(String(20), default="open")


class Backtest(Base):
    __tablename__ = "backtests"
    id = Column(String(16), primary_key=True, default=gen_id)
    strategy = Column(String(100), nullable=False)
    symbol = Column(String(50))
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    initial_capital = Column(Float, nullable=False)
    final_capital = Column(Float)
    total_trades = Column(Integer, default=0)
    win_rate = Column(Float, default=0.0)
    profit_factor = Column(Float, default=0.0)
    max_drawdown = Column(Float, default=0.0)
    sharpe_ratio = Column(Float, default=0.0)
    expectancy = Column(Float, default=0.0)
    total_pnl = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    results_json = Column(Text)


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(String(16), primary_key=True, default=gen_id)
    user_id = Column(String(16), ForeignKey("users.id"))
    symbol = Column(String(50))
    condition = Column(String(100))
    message = Column(Text)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
