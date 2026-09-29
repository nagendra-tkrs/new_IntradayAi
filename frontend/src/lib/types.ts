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
  /** REMAINING quantity after any T1 partial exit (equal to the original size before one). */
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
  // ---- Profit Capture layer (additive, optional) ----
  initial_quantity?: number;
  remaining_quantity?: number;
  exit_stage?: "ACTIVE" | "T1_EXECUTED";
  trailing_active?: boolean;
  realized_pnl?: number;
  t1_exit_price?: number;
  t1_exit_quantity?: number;
  t1_realized_pnl?: number;
  atr_ref?: number;
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
  atr_ref?: number;
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
  /** Live universe classification: "CORE" | "ROTATION" (carried from the active universe). */
  classification?: string;
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
  // ---- Top Signal Quality Layer (purely additive: base signal is untouched) ----
  /** Quality classification: PREMIUM | QUALIFIED | NORMAL | WEAK | REJECTED (null when not applicable). */
  setup_quality?: "PREMIUM" | "QUALIFIED" | "NORMAL" | "WEAK" | "REJECTED" | null;
  /** Total setup quality score (category budgets sum to 100). */
  setup_quality_score?: number | null;
  /** Number of confirmation categories marked "confirmed" (of confirmation_total). */
  confirmation_count?: number | null;
  /** Total confirmation categories considered (8: trend, momentum, vwap, volume, price action, market context, risk/reward, conflict). */
  confirmation_total?: number | null;
  /** True when setup qualifies as a Top Signal (PREMIUM/QUALIFIED and RR gate met). */
  top_signal_eligible?: boolean;
  /** Deterministic rank among top signals (1..n), null when not eligible. */
  top_signal_rank?: number | null;
  /** Risk/reward ratio measured from the actual setup levels. */
  risk_reward_ratio?: number | null;
  /** Per-category evidence + why/limitations, all derived from real calculated values. */
  quality_detail?: {
    categories: Record<
      string,
      { status: string; points: number; reason: string }
    >;
    why_qualified: string[];
    quality_limitations: string[];
  } | null;
}

export interface ScannerResponse {
  total_scanned: number;
  /** Universe key that was actually scanned ("rotation_80" when active). */
  universe?: string;
  /** Active universe version label (e.g. draft-20261001-925c84), or null. */
  active_universe_version?: string | null;
  signals_found: number;
  data_source: string;
  signals_paused: boolean;
  results: ScannerResult[];
  top_signals: ScannerResult[];
  /** Count of results per setup_quality classification (PREMIUM/QUALIFIED/NORMAL/WEAK/REJECTED). */
  setup_quality_distribution?: Record<string, number>;
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
  /** Optional quality/ATR metadata for risk-based sizing (scanner-fed only). */
  setup_quality?: string | null;
  signal_strength?: string | null;
  atr?: number | null;
  /** Traceability: the AI signal (SignalInfo.id) this order originates from. */
  signal_id?: string | null;
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
  /** Live universe classification: "CORE" or "ROTATION". */
  classification?: string;
  /** Rank within the member's category. */
  rank?: number | null;
  /** Total rotation score from the active universe version, when available. */
  rotation_score?: number | null;
  /** Backend universe key this instrument belongs to ("rotation_80"). */
  universe?: string;
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
  // ---- Profit Capture layer (additive, optional) ----
  exit_reason?: "T1_PARTIAL" | "T2_FINAL" | "TRAILING_STOP" | "STOP_LOSS" | "MANUAL_CLOSE" | "TARGET_1" | string | null;
  position_id?: string | null;
  exit_quantity?: number | null;
  initial_quantity?: number | null;
  remaining_quantity?: number | null;
  t1_exit_price?: number | null;
  t1_exit_quantity?: number | null;
  t1_realized_pnl?: number | null;
  // ---- AI Recommendation linkage (additive, optional) ----
  // The persisted AI decision this trade originated from. Manual orders and
  // pre-fix historical trades have signal_id = null and ai_decision
  // NOT_AVAILABLE — the UI must never fabricate a recommendation.
  signal_id?: string | null;
  ai_available?: boolean;
  ai_decision?: "AVAILABLE" | "NOT_AVAILABLE" | string;
  ai_signal_id?: string | null;
  ai_signal_timestamp?: string | null;
  ai_direction?: string | null;
  ai_score?: number | null;
  ai_confidence?: number | null;
  signal_quality?: string | null;
  ai_strategy?: string | null;
  ai_strategy_version?: string | null;
  ai_data_source?: string | null;
  ai_atr?: number | null;
  ai_trend_score?: number | null;
  ai_momentum_score?: number | null;
  ai_volume_score?: number | null;
  ai_vwap_score?: number | null;
  ai_price_action_score?: number | null;
  ai_market_context_score?: number | null;
  ai_risk_quality_score?: number | null;
  ai_evidence?: {
    nets?: Record<string, number> | null;
    net?: number | null;
    long_total?: number | null;
    short_total?: number | null;
    conflict?: boolean | null;
    risk_quality?: string | null;
  } | null;
  // Original setup captured at recommendation time (never overwritten by the
  // executed SL/T1/T2/trailing lifecycle).
  original_entry?: number | null;
  original_stop_loss?: number | null;
  original_target_1?: number | null;
  original_target_2?: number | null;
  original_risk_reward?: number | null;
  original_quantity?: number | null;
  original_risk_amount?: number | null;
  original_risk_percent?: number | null;
  // Actual execution aliases (AI-vs-actual comparison).
  actual_entry?: number | null;
  actual_exit?: number | null;
  realized_pnl?: number | null;
  unrealized_pnl?: number | null;
}

/** Realized P&L aggregates for a set of closed paper trades (or one day). */
export interface TradeHistorySummary {
  total_trades: number;
  profitable_trade_count: number;
  losing_trade_count: number;
  total_profit: number;
  total_loss: number;
  net_pnl: number;
}

/** Closed paper trades grouped by IST realization (exit) date. */
export interface TradeHistoryGroup {
  /** IST calendar date, YYYY-MM-DD (the date the trade was realized). */
  date: string;
  trade_count: number;
  profitable_trade_count: number;
  losing_trade_count: number;
  total_profit: number;
  total_loss: number;
  net_pnl: number;
  trades: PaperTrade[];
}

export interface TradeHistoryResponse {
  /** Flat realized ledger, most recent first (limited by `limit` when no date filter). */
  trades: PaperTrade[];
  /** Date-wise groups, newest date first. */
  date_groups: TradeHistoryGroup[];
  /** Aggregates across all returned realized trades. */
  summary: TradeHistorySummary;
}

/** One ledger row inside an AI Decision lifecycle (profit-capture trail). */
export interface AiDecisionLifecycleRow {
  trade_id: string;
  symbol: string;
  direction: string;
  entry_price: number | null;
  exit_price: number | null;
  quantity: number | null;
  exit_time: string;
  pnl: number | null;
  exit_reason: string | null;
}

/** Read-only `GET /paper/trades/{trade_id}/ai-decision` response. */
export interface AiDecisionResponse {
  trade_id: string;
  ai_decision: "AVAILABLE" | "NOT_AVAILABLE";
  reason?: string;
  signal?: {
    id: string;
    symbol: string;
    timestamp: string | null;
    direction: string | null;
    signal_score: number | null;
    confidence: number | null;
    signal_quality: string | null;
    strategy: string | null;
    strategy_version: string | null;
    data_source: string | null;
  } | null;
  score_breakdown?: Record<string, number | null> | null;
  evidence?: Record<string, unknown> | null;
  original_setup?: {
    entry: number | null;
    stop_loss: number | null;
    target_1: number | null;
    target_2: number | null;
    risk_reward: number | null;
    atr: number | null;
    quantity: number | null;
    risk_amount: number | null;
    risk_percent: number | null;
  } | null;
  execution?: Record<string, unknown> | null;
  lifecycle?: AiDecisionLifecycleRow[] | null;
  lifecycle_pnl?: number | null;
}

/** Trade/risk metrics for ONE model (legacy or profit capture) over a set of completed trades. */
export interface LegacyProfitCompareStats {
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  total_pnl: number;
  average_pnl: number;
  average_win: number;
  average_loss: number;
  /** Positive magnitude of the losing trades (same convention as the paper-trading summary). */
  total_loss: number;
  largest_win: number;
  /** Most negative single-trade P&L. */
  largest_loss: number;
  /** Null when there were no losing trades. */
  profit_factor: number | null;
  /** Positive peak-to-trough drawdown magnitude over the equity curve. */
  max_drawdown: number;
  max_consecutive_losses: number;
}

/** Profit Capture minus Legacy, per metric. */
export interface LegacyProfitCompareDifference {
  pnl: number;
  average_pnl: number;
  win_rate: number;
  profit_factor: number | null;
  max_drawdown: number;
}

/** Exit-path metrics for the Profit Capture (recorded) side. */
export interface LegacyProfitCompareExitMetrics {
  total_trades: number;
  /** Trades whose position reached the T1 price. */
  t1_hit_count: number;
  /** Trades that executed a T1 partial exit. */
  t1_partial_count: number;
  t1_partial_percent: number;
  t2_final_count: number;
  trailing_stop_count: number;
  stop_loss_count: number;
  manual_close_count: number;
  /** Legacy-era trades fully closed at T1 (100% at T1). */
  t1_full_close_count: number;
  better_than_legacy_count: number;
  worse_than_legacy_count: number;
  equal_to_legacy_count: number;
}

/** Read-only comparison split by direction. */
export type LegacyProfitCompareSplit = "all" | "long" | "short";

export interface LegacyProfitCompareResponse {
  period: { start: string | null; end: string | null };
  /** Always "paper_trading" — every number derives from the recorded ledger. */
  data_source: string;
  /** False when there is not enough real data → UI shows "Insufficient data". */
  has_data: boolean;
  models: { legacy: string; profit_capture: string };
  legacy: Record<LegacyProfitCompareSplit, LegacyProfitCompareStats>;
  profit_capture: Record<LegacyProfitCompareSplit, LegacyProfitCompareStats>;
  difference: Record<LegacyProfitCompareSplit, LegacyProfitCompareDifference>;
  exit_metrics: LegacyProfitCompareExitMetrics;
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

// ─── 24H market-context (Mode A vs Mode B) comparison types ────────────────

export interface ContextSignalStats {
  total: number;
  long: number;
  short: number;
  no_trade: number;
  avg_score: number | null;
  avg_score_long: number | null;
  avg_score_short: number | null;
  quality_distribution: Record<string, number>;
  has_data: boolean;
}

export interface ContextRealizedSide {
  total_trades: number;
  realized_pnl: number;
  win_rate: number | null;
  avg_pnl: number | null;
}

export interface ContextRealizedStats extends ContextRealizedSide {
  avg_win: number | null;
  avg_loss: number | null;
  profit_factor: number | null;
  avg_holding_seconds: number | null;
  max_drawdown: number | null;
  exit_breakdown: Record<string, number>;
  long: ContextRealizedSide;
  short: ContextRealizedSide;
  has_data: boolean;
}

export interface ContextComparison {
  data_source: string;
  use_24h_context: boolean;
  window_hours: number;
  has_data: boolean;
  current: {
    signal_stats: ContextSignalStats;
    realized_stats: ContextRealizedStats;
    realized_pending: boolean;
  };
  ctx24: {
    signal_stats: ContextSignalStats;
    realized_stats: ContextRealizedStats | null;
    realized_pending: boolean;
    pending_note: string;
  };
  agreement: {
    total: number;
    agree_count: number;
    agreement_rate: number | null;
  };
  caveats: string[];
}

export interface ClosePositionResult {
  trade: PaperTrade;
  pnl: number;
  exit_reason?: string;
  /** True when the close was a partial exit (position still open). */
  partial?: boolean;
  remaining?: number;
  exit_quantity?: number;
}
