const API_BASE = "/api";

export async function fetchAPI(path: string, options?: RequestInit) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
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

export async function getStockChart(symbol: string, days: number = 1) {
  return fetchAPI(`/stocks/${symbol}/chart?days=${days}`);
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
