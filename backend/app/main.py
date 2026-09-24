import json
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from contextlib import asynccontextmanager
from sqlalchemy import select
from app.core.config import settings
from app.core.database import init_db
from app.api.auth import router as auth_router, verify_token
from app.api.market import router as market_router
from app.api.trading import router as trading_router
from app.api.backtest_api import router as backtest_router
from app.api.trade_setup import router as trade_setup_router

# Phase 3 (Universe Manager persistence): import the additive ORM models so
# Base.metadata.create_all() (via init_db() in lifespan) creates the new tables
# on startup. Additive only — no existing table is touched, no behavior change.
import app.models.universe_models  # noqa: F401

PUBLIC_PATHS = {"/api/auth/google/config", "/api/auth/google/callback", "/api/health"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in PUBLIC_PATHS and not path.startswith("/api/auth/"):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return JSONResponse(status_code=401, content={"detail": "Missing token"})
            try:
                verify_token(auth.split(" ", 1)[1])
            except Exception:
                return JSONResponse(status_code=401, content={"detail": "Invalid token"})
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Best-effort: ensure a default user exists so authenticated endpoints work
    # even when Google OAuth is not configured or used.
    try:
        from app.core.database import async_session
        from app.models.models import User
        from sqlalchemy import select
        async with async_session() as db:
            result = await db.execute(select(User).where(User.email == "default@intradayai.local"))
            user = result.scalar_one_or_none()
            if not user:
                user = User(
                    email="default@intradayai.local",
                    name="Default User",
                    auth_provider="system",
                )
                db.add(user)
                await db.commit()
                await db.refresh(user)
    except Exception:
        pass
    # Best-effort: seed/backfill known company sectors into the instruments table
    # so existing companies display a real sector. Never blocks startup, never
    # overwrites a valid existing sector.
    try:
        from app.services.instrument_service import backfill_known_sectors, backfill_missing_sectors
        await backfill_known_sectors()
        await backfill_missing_sectors()
    except Exception:
        pass
    # Background paper-trading SL/Target monitor: marks open positions from live
    # quotes and closes them automatically when stop-loss or target_1 is hit.
    from app.services.paper_monitor import start_monitor, stop_monitor
    start_monitor()
    try:
        yield
    finally:
        stop_monitor()


app = FastAPI(
    title="IntradayAI",
    description="AI-powered intraday stock analysis and paper trading for Indian markets",
    version="1.0.0",
    lifespan=lifespan,
)

try:
    origins = json.loads(settings.CORS_ORIGINS)
except Exception:
    origins = ["http://localhost:3000"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuthMiddleware)

app.include_router(auth_router)
app.include_router(market_router)
app.include_router(trading_router)
app.include_router(backtest_router)
app.include_router(trade_setup_router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "IntradayAI", "version": "1.0.0"}
