// Typed shapes for the Edge GEX Lab API, contract v1.
// Times are ISO 8601 UTC strings. GEX values are dollars of delta change per 1% move.

export type FeedKind = "opra" | "indicative" | "cboe_delayed" | "demo";
export type Regime = "positive" | "negative" | "neutral";
export type CaptureStatus = "eligible" | "late" | "invalid" | "missed" | "future";

// GET /api/update: is a newer release out? Checked at most once a day; enabled false in demo mode or when opted out.
export interface UpdateInfo {
  enabled: boolean;
  current: string;
  latest: string | null;
  available: boolean;
  url: string;
  title: string;
  checked_at: string | null;
  error: string | null;
}

export interface Health {
  status: string;
  demo: boolean;
  feeds: { alpaca_options: "opra" | "indicative" | null; stocks: "sip" | "iex" | null; cboe: boolean };
  market_open: boolean;
  last_frozen_capture: { ticker: string; date: string; status: CaptureStatus } | null;
  rate: { used_last_min: number; cap: number };
  // false when no Alpaca keys are set: SPY, QQQ and stocks cannot load, SPX and NDX still do
  keys_configured?: boolean;
  // Newest frozen capture per pinned ticker, with its stored invalid-contract counts by reason.
  latest_captures?: {
    ticker: string;
    date: string;
    status: CaptureStatus;
    reason: string | null;
    // gate that judged the capture: "1.2" reads gex_coverage_pct, "1.1" oi_coverage_pct, "1.0" (older rows) coverage_pct
    gate_version?: string | null;
    oi_coverage_pct?: number | null;
    gex_coverage_pct?: number | null;
    // contract count within 10% of spot: the 1.0 gate number, a diagnostic under 1.1
    coverage_pct: number | null;
    eligible_count: number | null;
    valid_count: number | null;
    invalid_counts: Record<string, number> | null;
  }[];
  calendar?: {
    source: "alpaca" | "alpaca_cache" | "weekday_fallback" | "demo" | string;
    fetched_at: string | null;
    covered: [string, string] | null;
    degraded: boolean;
    last_error: string | null;
  };
}

export interface Tickers {
  pinned: string[];
}

export interface SearchHit {
  symbol: string;
  name: string;
  source: string;
}

export interface Expiration {
  date: string;
  dte: number;
  settlement: "AM" | "PM";
  settlements?: ("AM" | "PM")[];
  kind: "daily" | "weekly" | "monthly" | string;
  /** Last settlement instant of the date (UTC ISO, early closes respected); absent from older payloads. */
  settles_at?: string;
}

export interface Expirations {
  ticker: string;
  window_days: number;
  expirations: Expiration[];
}

export interface StrikeRow {
  strike: number;
  call_gex: number;
  put_gex: number;
  net_gex: number;
  call_oi: number;
  put_oi: number;
}

export interface MapLevels {
  call_wall: number | null;
  put_wall: number | null;
  zero_gamma: number[];
  nearest_zero_gamma: number | null;
  /** null when the map has no usable quotes (`quotes.usable` false). */
  regime: Regime | null;
}

export interface FrozenToday {
  captured_at: string;
  status?: CaptureStatus;
  reason?: string | null;
  call_wall: number | null;
  put_wall: number | null;
  nearest_zero_gamma: number | null;
}

/** Net GEX next to what normally trades. Contract section "Net GEX magnitude". */
/** Net GEX for a typical day: the hedge for a 1-ATR move (linear). SPX/NDX use the ETF's ATR%. */
export interface MagnitudePerAtr {
  atr_pct: number | null;
  atr_usd: number | null;
  atr_symbol: string;
  atr_basis: "own" | "etf" | null;
  atr_pct_basis: "spot" | "etf_close" | "last_close" | null;
  atr_label: string | null;
  atr_note: string | null;
  atr_sessions: number | null;
  atr_window: { first: string | null; last: string | null } | null;
  atr_source: string | null;
  atr_reason: string | null;
  hedge_per_atr_usd: number | null;
  pct_of_adv_per_atr: number | null;
  method: string;
}

/** Gross GEX: abs(call GEX) + abs(put GEX), same expiries. net_to_gross is 0..1. */
export interface MagnitudeGross {
  per_1pct_usd: number;
  pct_of_adv: number | null;
  hedge_per_atr_usd: number | null;
  pct_of_adv_per_atr: number | null;
  net_to_gross: number | null;
  note: string;
}

export interface Magnitude {
  adv_usd: number | null;
  adv_basis: "own" | "none" | null;
  adv_label: string | null;
  adv_sessions: number | null;
  adv_window: { first: string | null; last: string | null } | null;
  adv_source: string | null;
  adv_reason: string | null;
  hedge_per_1pct_usd: number;
  pct_of_adv: number | null;
  hedge_for_move: { move_pct: number; usd: number }[];
  hedge_for_move_method: string;
  percentile: number | null;
  percentile_value: number;
  percentile_basis: string;
  history_n: number;
  history_min: number;
  ticks_pct: number[];
  explainer: string;
  per_atr?: MagnitudePerAtr | null;
  gross?: MagnitudeGross | null;
  index_note?: string;
}

/** Vol Drift: ATM IV of the nearest expiry with three full trading days left, against the session's first reading.
 *  Contract section "Vol Drift and DEX". Display only. */
export interface VolDrift {
  available: boolean;
  reason: string | null;
  expiry: string | null;
  settlement: "AM" | "PM" | null;
  session: string;
  current: { iv_pct: number; ts: string; spot: number; k_lo: number; k_hi: number; iv_lo_pct: number; iv_hi_pct: number; usable: number } | null;
  /** source: recorder (first stored capture of the session), app (first value this app computed), demo (fixture). */
  reference: { iv_pct: number; ts: string; data_ts: string | null; source: "recorder" | "app" | "demo"; kind: "snapshot" | "frozen" | null } | null;
  reference_reason: string | null;
  /** current minus reference, vol points, rounded to 0.1: the number the label reads */
  change_pts: number | null;
  label: "rising" | "falling" | "flat" | null;
  /** half width of the flat band, vol points: max(flat_min_pts, flat_rel x reference IV); null without a reference */
  threshold_pts: number | null;
  flat_min_pts: number;
  flat_rel: number;
  regime: "positive" | "negative" | "neutral" | null;
  pairing: string | null;
  tested: boolean;
  chip: string;
  method: string;
  reference_method: string;
  clock_note: string;
}

/** DEX: delta * OI * 100 * spot over the selected expiries. net = calls minus puts (put delta is negative), plus unsigned sides. */
export interface Dex {
  available: boolean;
  reason: string | null;
  net_usd: number | null;
  call_usd: number | null;
  put_usd: number | null;
  contracts: number;
  oi_date: string | null;
  caveat: string;
  method: string;
}

export interface OiSource {
  kind: "cboe" | "alpaca" | null;
  label: string | null;
  /** inferred: Cboe date inferred as the prior session; matches_alpaca: Cboe numbers equal Alpaca's dated OI; reported: Alpaca. */
  basis: "inferred" | "matches_alpaca" | "reported" | null;
  oi_date: string | null;
  alpaca_oi_date: string | null;
  /** set when Cboe OI was wanted but not used, with the reason */
  fallback_reason: string | null;
  matched: number | null;
  total: number | null;
  differ_frac: number | null;
  payload_ts: string | null;
}

export interface GexMap {
  ticker: string;
  as_of: string;
  model_version: string;
  /** basis: last_trade (in session), regular_close (market closed: official session close), payload (Cboe),
   *  prior_close (Cboe with the market open but the index value from before today's open; ts is the index's own time). */
  spot: { price: number; ts: string; source: string; basis?: "last_trade" | "regular_close" | "payload" | "prior_close" };
  /** The one clock T is measured from. label is set only when the market is closed. */
  valuation?: { ts: string; basis: "quote_time" | "session_close"; label: string | null };
  feed: { kind: FeedKind; label: string; stale: boolean; market_open: boolean; last_success: string | null; data_ts?: string | null };
  oi_date: string | null;
  /** Open interest vendor (model 1.1). kind cboe for SPX/NDX and, unless Cboe failed, for SPY, QQQ and stocks; label is
   *  "OI: Cboe (prior session, inferred)", "OI: Cboe (matches Alpaca, Oct 15)" or "OI: Alpaca (Oct 15)". null in demo. */
  oi_source?: OiSource | null;
  /** eligible/valid/pct: contract count within 10% (diagnostic). oi_pct: share of the OI within oi_band_pct of spot on valid contracts (gate 1.1, diagnostic).
   *  gex_pct: share of the estimated |GEX| within oi_band_pct on valid contracts, the gate 1.2 number; gate_pct: its threshold (98). */
  coverage: {
    eligible: number;
    valid: number;
    pct: number;
    oi_pct?: number | null;
    gex_pct?: number | null;
    oi_band_pct?: number;
    gate_version?: string;
    gate_pct?: number;
  };
  signed: boolean;
  strikes: StrikeRow[];
  by_expiry: { expiry: string; contracts: number; valid?: number; valid_pct: number; net_gex: number }[];
  totals: { call_gex: number; put_gex: number; net_gex: number };
  /** null when the reference could not be built; absent from older payloads. */
  magnitude?: Magnitude | null;
  /** null only when the block could not be built; absent from older payloads. */
  vol_drift?: VolDrift | null;
  dex?: Dex | null;
  /** usable false: no valid contract within 10% of spot, so the levels are unavailable, not neutral or zero.
   *  reason "no_live_quotes": no bid, ask or IV anywhere in the selection (Cboe's first minutes after the open). */
  quotes?: { usable: boolean; reason: "no_live_quotes" | "no_usable_contracts" | null; live: number };
  levels: MapLevels;
  profile_curve: { spot: number; net_gex: number }[];
  frozen_today: FrozenToday | null;
  lead: string;
  warnings: string[];
}

export interface CaptureRow {
  date: string;
  ticker: string;
  status: CaptureStatus;
  captured_at: string | null;
  contracts: number | null;
  // contract count within 10% of spot: the 1.0 gate number, a diagnostic under 1.1
  coverage_pct: number | null;
  // gate that judged the capture ("1.0" for rows before protocol v1.1). Each gate reads its own number:
  // 1.2 gex_coverage_pct (share of estimated |GEX| within 5% held by valid contracts), 1.1 oi_coverage_pct
  // (share of OI within 5%), 1.0 coverage_pct. The others are stored diagnostics.
  gate_version?: string | null;
  oi_coverage_pct?: number | null;
  gex_coverage_pct?: number | null;
  hash: string | null;
  input_digest?: string | null;
  reason?: string | null;
  valid?: number | null;
  // contracts inside the coverage band that were not usable, by reason (stored per capture)
  invalid_counts?: Record<string, number> | null;
  // the after-close processing step (16:20 ET) has run for this session
  processed?: boolean;
  // its outcome for the studies (SPY, QQQ): complete = analyzed AND counted by the cohort, excluded = left out
  // for study_reason (analysis reasons and cohort reasons, comma separated)
  study_status?: "complete" | "excluded" | null;
  study_reason?: string | null;
}

export type LevelFamily =
  | "call_wall"
  | "put_wall"
  | "zero_gamma"
  | "round"
  | "prior_high"
  | "prior_low"
  | "prior_close"
  | "pm_high"
  | "pm_low"
  // display only, never sent by the API: a call wall and a put wall on the same strike, drawn once
  | "call_put_wall";

export interface Bar {
  t: string;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export interface Comparator {
  family: LevelFamily;
  price: number;
  label: string;
}

export interface Touch {
  family: LevelFamily;
  price: number;
  time: string;
  approach: "from_below" | "from_above";
  gap_through: boolean;
  reaction_atr: number | null;
  counted?: boolean;
  excluded_reason?: "gap_through" | "horizon_incomplete" | null;
}

export interface Behavior {
  regime: Regime | null;
  trend_efficiency: number | null;
  range_atr: number | null;
  realized_vol: number | null;
  vwap_crosses: number | null;
  close_location: number | null;
  variance_ratio: number | null;
  atr: number | null;
  baseline_prior_range_atr?: number | null;
  baseline_open_vol?: number | null;
  spot_vs_flip?: "above" | "below" | "at" | "no_flip";
  missing_minutes?: number | null;
}

export type StudyStatus = "complete" | "excluded" | "pending" | "not_applicable";

export interface ReplayStudy {
  status: StudyStatus;
  eligible: boolean;
  reasons: string[];
  capture_status: CaptureStatus | null;
  missing_minutes: number | null;
  bars_through: string | null;
  prepared_at: string | null;
}

export interface ReplaySnapshot {
  time: string;
  spot?: number;
  call_wall: number | null;
  put_wall: number | null;
  nearest_zero_gamma: number | null;
  net_gex: number;
  status?: CaptureStatus;
  status_reason?: string | null;
  spot_ts?: string | null;
  index_ts?: string | null;   // SPX/NDX: the index value's own time (Cboe last_trade_time)
  used?: boolean;
}

export interface ReplayDates {
  ticker: string;
  dates: string[];
  processed?: string[];
}

export interface Replay {
  ticker: string;
  date: string;
  resolution: "1m" | "15m";
  // History audit-fix: GET /api/replay is a pure reader. processed:false means the after-close
  // processing step has not stored this session yet.
  processed?: boolean;
  processed_at?: string | null;
  session?: { date: string; is_session: boolean; open: string; close: string; early_close: boolean; calendar_source: string; completed: boolean } | null;
  window?: { start: string; end: string } | null;
  study?: ReplayStudy | null;
  levels_tested?: { family: LevelFamily; price: number; status: "touched" | "gap_through" | "not_touched" | "excluded_inside_band" | "no_data"; excluded_reason: string | null }[];
  inputs?: Record<string, unknown> | null;
  atr_source?: Record<string, unknown> | null;
  vwap_source?: "provider_vw" | "typical_price_fallback" | null;
  bars: Bar[];
  // every regular minute from 09:30 to the exchange close is present (only then is the last bar the close)
  bars_complete?: boolean;
  vwap: { t: string; v: number }[];
  frozen: {
    captured_at: string;
    status: CaptureStatus;
    spot: number;
    call_wall: number | null;
    put_wall: number | null;
    zero_gamma: number[];
    net_gex: number;
    gross_gex?: number | null;
    regime: Regime | null;
    status_reason?: string | null;
    spot_ts?: string | null;
  } | null;
  comparators: Comparator[];
  touches: Touch[];
  behavior: Behavior | null;
  snapshots: ReplaySnapshot[];
  lead: string;
  notes: string[];
}

// The facts sent to the AI (RECAP_VERSION 2.1). Map and replay keep nearest_zero_gamma; the recap payload says gamma flip.
export interface RecapPayload {
  ticker: string;
  date: string;
  weekday: string;
  date_label: string;
  freeze_time: string;
  measured_from: string;
  measured_to: string;
  window: string;
  early_close: boolean;
  capture_status: CaptureStatus | null;
  eligible: boolean;
  exclusion_reasons: string[];
  regime_at_10: Regime | null;
  spot_at_10: number | null;
  call_wall: number | null;
  put_wall: number | null;
  nearest_gamma_flip: number | null;
  gex_levels: { level: string; price: number; status: string }[];
  gex_levels_not_touched: string[];
  notes: string[];
  [key: string]: unknown;
}

export interface Recap {
  ticker: string;
  date: string;
  // processed:false: nothing stored for this session yet; payload is null and source is "none".
  processed?: boolean;
  eligible?: boolean | null;
  reason?: string | null;
  payload: RecapPayload | null;
  text: string;
  source: "ai" | "template" | "none";
  model: string | null;
  validated: boolean;
  rejected: string[];
}

// Results shapes per the contract's "/api/scorecard results" (null until a gate passes).
export type Interval = [number, number];

export interface Study1FamilyResult {
  family: LevelFamily | string;
  gex: boolean;
  first_touches: number;
  sessions_with_touch: number;
  touch_rate: number | null;
  median_reaction_atr: number | null;
  ci90: Interval | null;
  placebo: { events: number; touch_rate: number | null; median_reaction_atr: number | null; ci90: Interval | null } | null;
}

export interface Study1Results {
  sessions: number;
  lead?: string;
  method?: string;
  families: Study1FamilyResult[];
}

export interface Study2SignResult {
  sessions: number;
  median_trend_efficiency: number | null;
  ci90: Interval | null;
  secondary_medians?: Partial<Record<"range_atr" | "realized_vol" | "vwap_crosses" | "close_location" | "variance_ratio", number | null>>;
}

export interface Study2Results {
  lead?: string;
  by_sign: { positive: Study2SignResult; negative: Study2SignResult };
  oos: {
    target: string;
    method: string;
    n_predictions: number;
    mae_baseline: number;
    mae_with_gex: number;
    mae_change_pct: number;
  } | null;
}

export interface Scorecard {
  /** capture_gate: the gate new captures are judged by; capture_gates: text of each gate by version. */
  protocol: {
    version: string;
    locked_at: string | null;
    capture_gate?: string;
    capture_gates?: Record<string, string>;
    /** model/input version new captures are computed with, and the OI vendor policy (model 1.1: cboe) */
    model_version?: string;
    oi_source?: string;
    oi_source_note?: string;
    /** true when the Study 1 sample mixes Alpaca-OI and Cboe-OI sessions */
    mixed_oi_sources?: boolean;
  };
  study1: {
    ticker: string;
    sessions: number;
    /** eligible sessions by the gate that judged them, e.g. {"1.0": 1, "1.1": 2} */
    sessions_by_gate?: Record<string, number>;
    /** eligible sessions by open interest vendor, e.g. {"alpaca": 1, "cboe": 4}; rows before the switch are read by feed */
    sessions_by_oi_source?: Record<string, number>;
    gate_sessions: number;
    families: { family: LevelFamily | string; gex?: boolean; first_touches: number; sessions_with_touch: number; gate: number | null; gate_sessions?: number | null }[];
    passed: boolean;
    results: Study1Results | null;
  };
  study2: {
    positive: { sessions: number; after_lock: number; gate: number; gate_after_lock?: number; sessions_by_gate?: Record<string, number>; sessions_by_oi_source?: Record<string, number> };
    negative: { sessions: number; after_lock: number; gate: number; gate_after_lock?: number; sessions_by_gate?: Record<string, number>; sessions_by_oi_source?: Record<string, number> };
    excluded_neutral?: number;
    passed: boolean;
    results: Study2Results | null;
  };
  lead: string;
  notes?: string[];
}

// ---------- GET /api/futures: the Futures view conversion (display only; the map is unchanged) ----------
export interface FuturesContract {
  index: "SPX" | "NDX";
  symbol: string; // "/ESZ26"
  also: string[]; // ["/MESZ26"]: same index, same price
  month: string; // "2026-12"
  label: string; // "Dec 2026"
  expiry: string;
  roll_date: string;
  code: string; // "Z26"
  settlement: string;
  next?: FuturesContract;
}
export interface FuturesParity {
  available: boolean;
  reason: string | null;
  /** True while Cboe's index value is the prior close (before about 09:45 ET): no basis is computed. */
  prior_close?: boolean;
  forward: number | null;
  index_spot: number;
  /** median of basis_raw over the payloads of the last few minutes (basis_window) */
  basis: number | null;
  /** this payload's F - index */
  basis_raw: number | null;
  basis_window: { payloads: number; seconds: number } | null;
  rate: number;
  rate_source: string;
  t_years: number | null;
  strikes: { strike: number; call_mid: number; put_mid: number; forward: number }[];
  usable_pairs: number;
  spread: number | null;
  option_root: string;
  option_expiry: string | null;
  settlement: "AM";
  as_of: string | null;
  index_time: string | null;
}
export interface FuturesRatio {
  value: number;
  index_price: number;
  index_time: string;
  etf: string;
  etf_price: number;
  etf_time: string | null;
  lag_s: number | null;
  source: string;
  note: string;
}
export interface FuturesOverride {
  price: number;
  index_value: number;
  index_source: "live_etf_ratio" | "cboe_delayed" | "demo";
  index_time: string | null;
  delayed: boolean;
  basis: number;
  at: string | null;
  label: string;
}
export interface FuturesView {
  ticker: string;
  index: "SPX" | "NDX";
  etf: string | null;
  available: boolean;
  reason: string | null;
  contract: FuturesContract;
  parity: FuturesParity | null;
  ratio: FuturesRatio | null;
  override: FuturesOverride | null;
  check: { diff: number | null; tolerance: number; warning: boolean } | null;
  basis: { value: number; source: "parity" | "override"; label: string } | null;
  /** futures = round to tick (level * ratio + basis). ratio is 1 for SPX and NDX. */
  convert: { ratio: number; basis: number; tick: number } | null;
  tick: number;
  tolerance: number;
  label: string;
  warnings: string[];
  as_of: string;
  demo: boolean;
}
