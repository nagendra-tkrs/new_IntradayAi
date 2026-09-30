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
  PendingOrder,
  Performance,
  PlaceOrderResult,
  Portfolio,
  OrderPreviewRequest,
  RiskPreview,
  ScannerResponse,
  StockChartResponse,
  StockDetailResponse,
  TradeHistoryResponse,
  TradeSetup,
  LegacyProfitCompareResponse,
  ContextComparison,
  AiDecisionResponse,
  SizingMode,
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
    if (err && typeof err === "object" && "status" in err && err.status === "ACTIVE_UNIVERSE_UNAVAILABLE") {
      const reason = typeof err.reason === "string" && err.reason
        ? err.reason
        : "The active universe is unavailable (ACTIVE_UNIVERSE_UNAVAILABLE).";
      const e = new Error(reason);
      (e as { payload?: unknown }).payload = err;
      throw e;
    }
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

export async function getStocks(universe: string = "ACTIVE"): Promise<Instrument[]> {
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

export async function runScanner(universe: string = "ACTIVE"): Promise<ScannerResponse> {
  return fetchAPI(`/scanner?universe=${universe}`);
}

export async function getPortfolio(): Promise<Portfolio> {
  return fetchAPI("/portfolio");
}

export async function placePaperOrder(order: {
  symbol: string;
  direction: string;
  /**
   * Omit for risk-engine sizing. There is intentionally no default of 1: a
   * quantity the user never entered must not be sent as if they had.
   */
  quantity?: number | null;
  entry_price?: number;
  stop_loss?: number;
  target_1?: number;
  target_2?: number;
  setup_quality?: string;
  signal_strength?: string;
  atr?: number;
  signal_id?: string;
  sizing_mode?: SizingMode | null;
}): Promise<PlaceOrderResult> {
  return fetchAPI("/paper/orders", {
    method: "POST",
    body: JSON.stringify(order),
  });
}

/**
 * Risk-sizing preview for an order that has not been placed yet.
 *
 * Read-only. Returns the whole calculation - risk budget, risk per share, both
 * constraints, allowed quantity, expected initial risk and budget utilization -
 * so the user can see exactly what each sizing path would do before choosing.
 */
export async function previewPaperOrder(
  payload: OrderPreviewRequest,
): Promise<RiskPreview> {
  return fetchAPI("/paper/orders/preview", {
    method: "POST",
    body: JSON.stringify(payload),
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

/**
 * Realized paper-trade ledger with date-wise reporting.
 *
 * `date` (optional, IST `YYYY-MM-DD`) narrows the response to trades realized
 * on that day; without it the flat `trades` list keeps its legacy cap of
 * `limit` while `date_groups` / `summary` cover all closed trades.
 */
export async function getTradeHistory(limit: number = 50, date?: string): Promise<TradeHistoryResponse> {
  const dateParam = date ? `&date=${encodeURIComponent(date)}` : "";
  return fetchAPI(`/paper/trades?limit=${limit}${dateParam}`);
}

/** Read-only AI Decision detail for one closed paper trade (read-only). */
export async function getAiDecision(tradeId: string): Promise<AiDecisionResponse> {
  return fetchAPI(`/paper/trades/${encodeURIComponent(tradeId)}/ai-decision`);
}

export async function getPerformance(): Promise<Performance> {
  return fetchAPI("/paper/performance");
}

/** Read-only Legacy vs Profit Capture comparison (optional YYYY-MM-DD IST range). */
export async function getPerformanceComparison(startDate?: string, endDate?: string): Promise<LegacyProfitCompareResponse> {
  const params = new URLSearchParams();
  if (startDate) params.set("start_date", startDate);
  if (endDate) params.set("end_date", endDate);
  const qs = params.toString();
  return fetchAPI(`/paper/performance/comparison${qs ? `?${qs}` : ""}`);
}

/** Read-only Current (Mode A) vs 24H-context shadow (Mode B) comparison. */
export async function getContextComparison(): Promise<ContextComparison> {
  return fetchAPI("/paper/performance/context-comparison");
}

export async function runBacktest(params: {
  symbol: string;
  strategy?: string;
  strategy_version?: string;
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