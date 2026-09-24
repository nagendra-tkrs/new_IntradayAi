import type {
  BacktestResult,
  CancelOrderResult,
  ClosePositionResult,
  DataStatus,
  EditOrderResult,
  FillOrderResult,
  Instrument,
  MarketIndex,
  MarketStatus,
  PaperPosition,
  PaperTrade,
  PendingOrder,
  Performance,
  PlaceOrderResult,
  Portfolio,
  ScannerResponse,
  StockChartResponse,
  StockDetailResponse,
  TradeSetup,
} from "./types";

const API_BASE = "/api";

function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("trading_token");
}

export async function fetchAPI<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options?.headers as Record<string, string> || {}),
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
  });
  if (res.status === 401) {
    localStorage.removeItem("trading_token");
    window.location.href = "/login";
    throw new Error("Unauthorized");
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

export async function getMarketStatus(): Promise<MarketStatus> {
  return fetchAPI("/market/status");
}

export async function getDataStatus(): Promise<DataStatus> {
  return fetchAPI("/market/data-status");
}

export async function getMarketIndex(name: string = "NIFTY50"): Promise<MarketIndex> {
  return fetchAPI(`/market/index/${name}`);
}

export async function getStocks(universe: string = "NIFTY50"): Promise<Instrument[]> {
  return fetchAPI(`/stocks?universe=${universe}`);
}

export async function getStockDetail(symbol: string): Promise<StockDetailResponse> {
  return fetchAPI(`/stocks/${symbol}`);
}

export async function getTradeSetup(symbol: string): Promise<TradeSetup> {
  return fetchAPI(`/stocks/${symbol}/trade-setup`);
}

export async function saveTradeSetup(symbol: string, setup: {
  entry?: number;
  stop_loss?: number;
  target?: number;
  direction?: string;
}): Promise<TradeSetup> {
  return fetchAPI(`/stocks/${symbol}/trade-setup`, {
    method: "PATCH",
    body: JSON.stringify(setup),
  });
}

export async function clearTradeSetup(symbol: string): Promise<TradeSetup> {
  return fetchAPI(`/stocks/${symbol}/trade-setup`, {
    method: "DELETE",
  });
}

export async function getStockChart(symbol: string, days: number = 1, interval: string = "5m"): Promise<StockChartResponse> {
  return fetchAPI(`/stocks/${symbol}/chart?days=${days}&interval=${interval}`);
}

export async function runScanner(universe: string = "NIFTY50"): Promise<ScannerResponse> {
  return fetchAPI(`/scanner?universe=${universe}`);
}

export async function getPortfolio(): Promise<Portfolio> {
  return fetchAPI("/portfolio");
}

export async function placePaperOrder(order: {
  symbol: string;
  direction: string;
  quantity: number;
  entry_price?: number;
  stop_loss?: number;
  target_1?: number;
  target_2?: number;
}): Promise<PlaceOrderResult> {
  return fetchAPI("/paper/orders", {
    method: "POST",
    body: JSON.stringify(order),
  });
}

export async function closePosition(positionId: string, exitPrice?: number): Promise<ClosePositionResult> {
  return fetchAPI("/paper/close", {
    method: "POST",
    body: JSON.stringify({ position_id: positionId, exit_price: exitPrice }),
  });
}

export async function getPositions(): Promise<{ positions: PaperPosition[] }> {
  return fetchAPI("/paper/positions");
}

export async function getTradeHistory(limit: number = 50): Promise<{ trades: PaperTrade[] }> {
  return fetchAPI(`/paper/trades?limit=${limit}`);
}

export async function getPerformance(): Promise<Performance> {
  return fetchAPI("/paper/performance");
}

export async function runBacktest(params: {
  symbol: string;
  strategy?: string;
  days?: number;
  initial_capital?: number;
}): Promise<BacktestResult> {
  return fetchAPI("/backtest", {
    method: "POST",
    body: JSON.stringify(params),
  });
}

export async function getAllUserSetups(): Promise<{
  setups: Record<string, TradeSetup>;
}> {
  return fetchAPI("/trade-setups");
}

export async function getPendingOrders(): Promise<{ orders: PendingOrder[] }> {
  return fetchAPI("/paper/pending");
}

export async function editPendingOrder(orderId: string, payload: {
  entry_price?: number;
  stop_loss?: number;
  target_1?: number;
  target_2?: number;
  quantity?: number;
}): Promise<EditOrderResult> {
  return fetchAPI(`/paper/orders/${orderId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function fillPendingOrder(orderId: string, fillPrice?: number): Promise<FillOrderResult> {
  return fetchAPI(`/paper/orders/${orderId}/fill`, {
    method: "POST",
    body: JSON.stringify({ fill_price: fillPrice }),
  });
}

export async function cancelPendingOrder(orderId: string): Promise<CancelOrderResult> {
  return fetchAPI(`/paper/orders/${orderId}`, {
    method: "DELETE",
  });
}