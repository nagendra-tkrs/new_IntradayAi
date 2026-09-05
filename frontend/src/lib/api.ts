const API_BASE = "/api";

function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("trading_token");
}

export async function fetchAPI(path: string, options?: RequestInit) {
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

export async function getMarketStatus() {
  return fetchAPI("/market/status");
}

export async function getDataStatus() {
  return fetchAPI("/market/data-status");
}

export async function getMarketIndex(name: string = "NIFTY50") {
  return fetchAPI(`/market/index/${name}`);
}

export async function getStocks(universe: string = "NIFTY50") {
  return fetchAPI(`/stocks?universe=${universe}`);
}

export async function getStockDetail(symbol: string) {
  return fetchAPI(`/stocks/${symbol}`);
}

export async function getTradeSetup(symbol: string) {
  return fetchAPI(`/stocks/${symbol}/trade-setup`);
}

export async function saveTradeSetup(symbol: string, setup: {
  entry?: number;
  stop_loss?: number;
  target?: number;
  direction?: string;
}) {
  return fetchAPI(`/stocks/${symbol}/trade-setup`, {
    method: "PATCH",
    body: JSON.stringify(setup),
  });
}

export async function clearTradeSetup(symbol: string) {
  return fetchAPI(`/stocks/${symbol}/trade-setup`, {
    method: "DELETE",
  });
}

export async function getStockChart(symbol: string, days: number = 1, interval: string = "5m") {
  return fetchAPI(`/stocks/${symbol}/chart?days=${days}&interval=${interval}`);
}

export async function runScanner(universe: string = "NIFTY50") {
  return fetchAPI(`/scanner?universe=${universe}`);
}

export async function getPortfolio() {
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
}) {
  return fetchAPI("/paper/orders", {
    method: "POST",
    body: JSON.stringify(order),
  });
}

export async function closePosition(positionId: string, exitPrice?: number) {
  return fetchAPI("/paper/close", {
    method: "POST",
    body: JSON.stringify({ position_id: positionId, exit_price: exitPrice }),
  });
}

export async function getPositions() {
  return fetchAPI("/paper/positions");
}

export async function getTradeHistory(limit: number = 50) {
  return fetchAPI(`/paper/trades?limit=${limit}`);
}

export async function getPerformance() {
  return fetchAPI("/paper/performance");
}

export async function runBacktest(params: {
  symbol: string;
  strategy?: string;
  days?: number;
  initial_capital?: number;
}) {
  return fetchAPI("/backtest", {
    method: "POST",
    body: JSON.stringify(params),
  });
}

export async function getAllUserSetups(): Promise<{
  setups: Record<string, {
    symbol: string;
    entry: number | null;
    stop_loss: number | null;
    target: number | null;
    risk_reward: number | null;
    direction: string | null;
    override_active: boolean;
    user_setup_updated_at: string | null;
  }>;
}> {
  return fetchAPI("/trade-setups");
}

export async function getPendingOrders() {
  return fetchAPI("/paper/pending");
}

export async function editPendingOrder(orderId: string, payload: {
  entry_price?: number;
  stop_loss?: number;
  target_1?: number;
  target_2?: number;
  quantity?: number;
}) {
  return fetchAPI(`/paper/orders/${orderId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function fillPendingOrder(orderId: string, fillPrice?: number) {
  return fetchAPI(`/paper/orders/${orderId}/fill`, {
    method: "POST",
    body: JSON.stringify({ fill_price: fillPrice }),
  });
}

export async function cancelPendingOrder(orderId: string) {
  return fetchAPI(`/paper/orders/${orderId}`, {
    method: "DELETE",
  });
}
