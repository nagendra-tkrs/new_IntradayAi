import json
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from app.core.config import settings
from app.core.database import init_db
from app.api.market import router as market_router
from app.api.trading import router as trading_router
from app.api.backtest_api import router as backtest_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


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

app.include_router(market_router)
app.include_router(trading_router)
app.include_router(backtest_router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "IntradayAI", "version": "1.0.0"}
