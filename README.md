# IntradayAI - AI Intraday Stock Trading Application

Production-quality intraday stock analysis and paper trading for Indian markets (NSE/BSE).

## Architecture

```
intradayai/
├── backend/           Python FastAPI backend
│   ├── app/
│   │   ├── api/       REST API routes
│   │   ├── core/      Config, database, market session
│   │   ├── models/    SQLAlchemy + Pydantic models
│   │   ├── services/  Business logic
│   │   │   ├── market_data/   Provider abstraction
│   │   │   ├── indicators.py  Technical indicators
│   │   │   ├── signal_engine.py  Multi-factor scoring
│   │   │   ├── scanner.py     Market scanner
│   │   │   ├── risk_engine.py  Risk management
│   │   │   ├── paper_trading.py  Virtual trading
│   │   │   └── backtesting.py   Backtest engine
│   │   └── main.py    FastAPI app entry
│   └── requirements.txt
├── frontend/          Next.js + TypeScript + Tailwind
│   └── src/
│       ├── app/       Pages (Dashboard, Scanner, Signals, etc.)
│       ├── components/  UI components
│       └── lib/api.ts  API client
├── docker-compose.yml
└── .env.example
```

## Features

- **Market Scanner**: Scans 40 NIFTY50 stocks with 15+ technical indicators
- **Signal Engine**: Multi-factor scoring (trend, momentum, volume, VWAP, price action, risk quality)
- **Confidence Scoring**: 0-100 confidence with clear labeling (Very Strong, Strong, Good, Moderate, Avoid)
- **Trade Setups**: ATR-based entry, stop-loss, targets, and risk/reward calculation
- **Risk Management**: Position sizing, daily loss limits, trade count limits, cooldown
- **Paper Trading**: Virtual portfolio with order execution and stop-loss management
- **Backtesting**: Historical testing with transaction costs (brokerage, STT, slippage)
- **Dashboard**: Dark-theme trading terminal with real-time signals

## Setup

### Quick Start (Development)

**Backend:**
```bash
cd backend
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
cp ../.env.example ../.env
python -m uvicorn app.main:app --reload --port 8000
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

The app will be available at http://localhost:3000 (frontend) and http://localhost:8000 (API).

### Docker Compose
```bash
docker-compose up --build
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | /api/health | Health check |
| GET | /api/market/status | Market session status |
| GET | /api/market/index/{name} | Index data (NIFTY50, BANKNIFTY) |
| GET | /api/stocks | List instruments |
| GET | /api/stocks/{symbol} | Stock detail + indicators + signal |
| GET | /api/scanner | Run full market scanner |
| GET | /api/portfolio | Paper trading portfolio |
| POST | /api/paper/orders | Place paper order |
| POST | /api/paper/close | Close paper position |
| GET | /api/paper/positions | Open positions |
| GET | /api/paper/trades | Trade history |
| POST | /api/backtest | Run backtest |

## Signal Scoring Methodology

Each stock is evaluated on 7 independent factors:

| Factor | Max Score | Description |
|--------|-----------|-------------|
| Trend | 20 | EMA 9/20/50 alignment + ADX strength |
| Momentum | 15 | RSI position + MACD histogram + Rate of Change |
| Volume | 15 | Relative volume vs 20-day average |
| VWAP | 15 | Distance from VWAP |
| Price Action | 15 | Opening range breakout + Previous high/low + Bollinger Bands |
| Market Context | 10 | NIFTY trend alignment |
| Risk Quality | 10 | ATR-based volatility assessment |

Total score (0-100) maps to signal direction:
- 78-100: STRONG LONG
- 65-77: LONG  
- 55-64: WEAK LONG
- 45-54: NO TRADE
- 35-44: WEAK SHORT
- 22-34: SHORT
- 0-21: STRONG SHORT

A minimum risk/reward ratio of 1.2 is required. No trade is generated below this threshold.

## Limitations

- **Simulated data only**: The mock provider generates realistic but fake market data. No real market data is used by default.
- **No guaranteed accuracy**: Signal scores are heuristic-based and have NOT been statistically calibrated to predict actual profit probability.
- **No broker integration**: Paper trading is virtual only. No real orders are placed.
- **Experimental ML layer**: The architecture supports ML models but none are deployed in the current version.

## Risk Disclaimer

This application is for educational and research purposes only. It does not constitute financial advice. Trading in stocks involves significant risk of loss. Past performance, whether simulated or backtested, does not guarantee future results. Always consult a qualified financial advisor before making investment decisions.

The signal confidence score (0-100) is a composite metric based on technical indicator confluence. It is NOT a probability of profit and should not be interpreted as such.

## Tech Stack

- **Backend**: Python 3.12+, FastAPI, SQLAlchemy, SQLite, pandas, NumPy
- **Frontend**: Next.js 16, React 19, TypeScript, Tailwind CSS, Recharts
- **Market Data**: Pluggable provider architecture (Mock, Live-ready)
- **Deployment**: Docker Compose, environment variables
