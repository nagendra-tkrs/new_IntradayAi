"use client";

export type PaperDirection = "LONG" | "SHORT" | "BUY" | "SELL";

export interface User {
  id: string;
  email: string;
  name: string;
  picture?: string;
}

export interface PaperPosition {
  id: string;
  user_id: string;
  symbol: string;
  direction: PaperDirection;
  quantity: number;
  entry_price: number;
  current_price: number;
  stop_loss: number;
  target_1: number;
  target_2: number;
  unrealized_pnl: number;
  opened_at: string;
  status: "open";
  filled_at: string | null;
}

export interface PendingOrder {
  id: string;
  user_id: string;
  symbol: string;
  direction: PaperDirection;
  quantity: number;
  entry_price: number;
  current_price: number;
  stop_loss: number;
  target_1: number;
  target_2: number;
  unrealized_pnl: number;
  opened_at: string;
  status: "pending";
  filled_at: null;
}

export interface ClosedTrade {
  id: string;
  user_id: string;
  symbol: string;
  direction: PaperDirection;
  quantity: number;
  entry_price: number;
  exit_price: number;
  entry_time: string;
  exit_time: string;
  pnl: number;
  status: "closed";
}

export interface PortfolioStats {
  count: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_pnl: number;
  avg_pnl: number;
}

export interface Portfolio {
  cash: number;
  total_value: number;
  reserved_margin: number;
  available_cash: number;
  short_liability: number;
  positions_count: number;
  pending_orders_count: number;
  pending_value: number;
  total_pnl: number;
  unrealized_pnl: number;
  realized_pnl: number;
  initial_capital: number;
  positions: PaperPosition[];
}

export interface Performance {
  daily: PortfolioStats;
  weekly: PortfolioStats;
  monthly: PortfolioStats;
  all_time: PortfolioStats;
}

export interface TradeSetup {
  symbol: string;
  entry: number | null;
  stop_loss: number | null;
  target: number | null;
  risk_reward: number | null;
  direction: string | null;
  override_active: boolean;
  user_setup_updated_at: string | null;
}

export interface SignalScore {
  trend_score: number;
  momentum_score: number;
  volume_score: number;
  vwap_score: number;
  price_action_score: number;
  market_context_score: number;
  risk_quality_score: number;
  total: number;
}

export type SignalDirection =
  | "STRONG_LONG"
  | "LONG"
  | "WEAK_LONG"
  | "NO_TRADE"
  | "WEAK_SHORT"
  | "SHORT"
  | "STRONG_SHORT";

export interface SignalSetup {
  entry: number;
  stop_loss: number;
  target_1: number;
  target_2: number;
  risk_per_share: number;
  reward_per_share: number;
  risk_reward_ratio: number;
  trailing_stop: number | null;
}

export interface SignalInfo {
  id: string;
  symbol: string;
  timestamp: string;
  direction: SignalDirection;
  confidence: number;
  signal_score: SignalScore;
  setup: SignalSetup;
  explanation: { reasons: string[]; risks: string[] };
  reasons: string[];
  strategy: string;
  data_source: string;
  indicator_values: Record<string, number | string | null>;
  signal_generated_at: string;
  entry_updated_at: string;
  stop_loss_updated_at: string;
  target_updated_at: string;
  last_updated_at: string;
  market_data_timestamp?: string | null;
}

export interface ScannerResult {
  symbol: string;
  name: string;
  sector: string;
  price: number;
  change_pct: number;
  volume: number;
  data_status: string;
  trend: string;
  signal: string;
  confidence: number;
  rsi: number;
  adx: number;
  relative_volume: number;
  distance_from_vwap: number;
  signal_data: SignalInfo | null;
  data_age_seconds?: number | null;
  data_timestamp?: string | null;
  ema_9?: number | null;
  ema_20?: number | null;
  ema_50?: number | null;
  macd?: number | null;
  atr?: number | null;
  vwap?: number | null;
  error?: string;
  reason?: string;
}

export interface ScannerResponse {
  total_scanned: number;
  signals_found: number;
  data_source: string;
  signals_paused: boolean;
  results: ScannerResult[];
  top_signals: ScannerResult[];
  disclaimer: string;
  data_status?: DataStatus;
}

export interface ScannerStocksResult extends ScannerResult {
  sector: string;
  name: string;
  change_pct: number;
  volume: number;
  data_status: string;
  signal: string;
  direction: string;
  confidence: number;
  signal_data: SignalInfo | null;
}

export interface StockDetail extends ScannerStocksResult {}

export interface SetupRequest {
  entry?: number | null;
  stop_loss?: number | null;
  target?: number | null;
  direction?: string | null;
}

export interface OrderRequest {
  symbol: string;
  direction: string;
  quantity: number;
  entry_price?: number | null;
  stop_loss?: number | null;
  target_1?: number | null;
  target_2?: number | null;
}

export interface ClosePositionRequest {
  position_id: string;
  exit_price?: number | null;
}

export interface ScannerPayload {
  universe?: string;
}

export interface PortfolioPayload {}

// ---- Phase 5: consumer-layer response types (verified from backend) ----

export interface ChartPoint {
  timestamp: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number;
  vwap: number | null;
  ema_9: number | null;
  ema_20: number | null;
  ema_50: number | null;
}

export interface Instrument {
  symbol: string;
  name: string;
  exchange: string;
  sector: string;
  yfinance_symbol: string;
}

export interface Quote {
  symbol: string;
  price: number;
  change: number;
  change_pct: number;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  prev_close: number;
  volume: number;
  timestamp: string | null;
  received_at: string;
  source: string;
  data_age_seconds: number | null;
  data_status?: string;
  error?: string;
  name?: string;
}

export interface MarketIndex extends Quote {
  name: string;
}

export interface DataStatus {
  mode?: string;
  connection_status?: string;
  data_source?: string;
  instruments_loaded?: boolean;
  symbols_cached?: number;
  bars_cached?: number;
  last_tick_time?: string | null;
  data_latency_ms?: number | null;
  signals_paused?: boolean;
  pause_reason?: string | null;
  historical_warmed_up?: boolean;
  symbol_freshness?: Record<string, unknown>;
  disclaimer?: string;
}

export interface MarketStatus extends Partial<DataStatus> {
  session: string;
  is_open: boolean;
  data_source: string;
  timestamp: string | null;
  disclaimer?: string;
}

export interface StockDetailResponse {
  quote: Quote;
  indicators: Record<string, number | null>;
  chart_data: ChartPoint[];
  signal: SignalInfo | null;
  user_trade_setup: TradeSetup;
  data_status: DataStatus;
  data_freshness: {
    source: string;
    status: string;
    data_age_seconds: number | null;
    should_trade: boolean;
    trade_reason: string | null;
  };
}

export interface StockChartResponse {
  data: ChartPoint[];
  data_status: string;
  source?: string;
  interval?: string;
  days?: number;
}

export interface PaperTrade {
  id: string;
  symbol: string;
  direction: string;
  entry_price: number;
  exit_price: number;
  quantity: number;
  stop_loss: number;
  target_1: number;
  target_2: number;
  entry_time: string;
  exit_time: string;
  pnl: number;
  result: string;
  status: string;
}

export interface BacktestTrade {
  direction: string;
  entry: number;
  exit: number;
  quantity: number;
  cost: number;
  net_pnl: number;
}

export interface EquityPoint {
  timestamp: string;
  equity: number;
}

export interface BacktestPerformance {
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_pnl: number;
  final_capital: number;
  initial_capital: number;
  profit_factor: number;
  max_drawdown: number;
  sharpe_ratio: number;
  sortino_ratio: number;
  expectancy: number;
  avg_win: number;
  avg_loss: number;
  avg_r_multiple: number;
  total_bars: number;
  total_slippage_paid: number;
  total_brokerage_paid: number;
  total_stt_paid: number;
}

export interface BacktestResult extends BacktestPerformance {
  id: string;
  symbol: string;
  strategy: string;
  strategy_version: string;
  performance: BacktestPerformance;
  trades: BacktestTrade[];
  equity_curve: EquityPoint[];
  error?: string;
}

export interface PlaceOrderResult {
  order_id: string;
  status: "pending";
  position: PendingOrder;
}

export interface EditOrderResult {
  order_id: string;
  status: "pending";
  position: PendingOrder;
}

export interface FillOrderResult {
  order_id: string;
  status: "filled";
  position: PaperPosition;
}

export interface CancelOrderResult {
  order_id: string;
  status: "cancelled";
}

export interface ClosePositionResult {
  trade: PaperTrade;
  pnl: number;
}
