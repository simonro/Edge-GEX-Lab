// Synthetic fixtures in the API contract v1 shapes. Used only when VITE_MOCK=1.
// Every number here is generated (a toy option chain run through the same BSM gamma formula),
// so screens built from it carry the "Synthetic fixture" badge. Nothing here is market data.

import type {
  Bar,
  CaptureRow,
  CaptureStatus,
  Comparator,
  Dex,
  Expirations,
  GexMap,
  Health,
  Magnitude,
  Recap,
  RecapPayload,
  Replay,
  Scorecard,
  SearchHit,
  Touch,
  VolDrift,
} from "../api/types";

const SESSION = "2026-10-19"; // synthetic "today" (a Monday)
const PRIOR = "2026-10-16";

interface TickerCfg {
  spot: number;
  step: number;
  round: number;
  feed: "opra" | "cboe_delayed";
  oiScale: number;
  name: string;
}

const CFG: Record<string, TickerCfg> = {
  SPY: { spot: 758.42, step: 1, round: 5, feed: "opra", oiScale: 1, name: "SPDR S&P 500 ETF" },
  QQQ: { spot: 612.36, step: 1, round: 5, feed: "opra", oiScale: 0.55, name: "Invesco QQQ Trust" },
  SPX: { spot: 7604.1, step: 5, round: 25, feed: "cboe_delayed", oiScale: 0.08, name: "S&P 500 Index" },
  NDX: { spot: 26141.5, step: 25, round: 100, feed: "cboe_delayed", oiScale: 0.006, name: "Nasdaq 100 Index" },
  NVDA: { spot: 188.64, step: 2.5, round: 10, feed: "opra", oiScale: 0.7, name: "NVIDIA Corp" },
};

const UNIVERSE: SearchHit[] = [
  { symbol: "NVDA", name: "NVIDIA Corp", source: "alpaca" },
  { symbol: "NFLX", name: "Netflix Inc", source: "alpaca" },
  { symbol: "NVO", name: "Novo Nordisk A/S", source: "alpaca" },
  { symbol: "NKE", name: "Nike Inc", source: "alpaca" },
  { symbol: "AAPL", name: "Apple Inc", source: "alpaca" },
  { symbol: "AMD", name: "Advanced Micro Devices", source: "alpaca" },
  { symbol: "AMZN", name: "Amazon.com Inc", source: "alpaca" },
  { symbol: "TSLA", name: "Tesla Inc", source: "alpaca" },
  { symbol: "META", name: "Meta Platforms Inc", source: "alpaca" },
  { symbol: "MSFT", name: "Microsoft Corp", source: "alpaca" },
  { symbol: "IWM", name: "iShares Russell 2000 ETF", source: "alpaca" },
  { symbol: "SMH", name: "VanEck Semiconductor ETF", source: "alpaca" },
];

function cfg(t: string): TickerCfg {
  if (CFG[t]) return CFG[t];
  let h = 0;
  for (const c of t) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const spot = 40 + (h % 400) + 0.37;
  return { spot, step: spot > 200 ? 5 : 2.5, round: 10, feed: "opra", oiScale: 0.3, name: t };
}

function rng(seed: number) {
  let s = seed >>> 0 || 1;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

function seedOf(...parts: string[]): number {
  let h = 2166136261;
  for (const p of parts) for (const c of p) h = Math.imul(h ^ c.charCodeAt(0), 16777619) >>> 0;
  return h;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// ---------- calendar ----------
function addDays(day: string, n: number): string {
  const [y, m, d] = day.split("-").map(Number);
  const t = new Date(Date.UTC(y, m - 1, d + n));
  return t.toISOString().slice(0, 10);
}
function dow(day: string): number {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).getUTCDay();
}
function isTradingDay(day: string): boolean {
  const w = dow(day);
  return w !== 0 && w !== 6;
}
function tradingDaysBack(end: string, n: number): string[] {
  const out: string[] = [];
  let d = end;
  while (out.length < n) {
    if (isTradingDay(d)) out.push(d);
    d = addDays(d, -1);
  }
  return out.reverse();
}
/** New York wall clock on a given day to a UTC ISO string (EDT, UTC-4, for these October dates). */
function nyIso(day: string, hh: number, mm: number, ss = 0): string {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d, hh + 4, mm, ss)).toISOString();
}

// ---------- BSM gamma, r = q = 0 ----------
const SQ2PI = Math.sqrt(2 * Math.PI);
function gamma(S: number, K: number, iv: number, T: number): number {
  if (T <= 0 || iv <= 0) return 0;
  const sd = iv * Math.sqrt(T);
  const d1 = (Math.log(S / K) + 0.5 * iv * iv * T) / sd;
  return Math.exp(-0.5 * d1 * d1) / SQ2PI / (S * sd);
}

interface Contract {
  K: number;
  expiry: string;
  T: number;
  iv: number;
  callOI: number;
  putOI: number;
}

const g = (x: number, w: number) => Math.exp(-((x / w) ** 2));

function expirationsFor(ticker: string): Expirations {
  const days = [0, 1, 2, 3, 4, 7];
  const exps = days.map((dte) => {
    const date = addDays(SESSION, dte);
    return {
      date,
      dte,
      settlement: "PM" as const,
      settlements: ["PM" as const],
      kind: (dow(date) === 5 ? "weekly" : "daily") as "daily" | "weekly",
      settles_at: nyIso(date, 16, 0),
    };
  });
  if (ticker === "NVDA") return { ticker, window_days: 7, expirations: exps.filter((e) => dow(e.date) === 5 || e.dte === 0) };
  return { ticker, window_days: 7, expirations: exps.filter((e) => e.dte !== 3 || ticker !== "QQQ") };
}

function chain(ticker: string, expiries: string[]): { contracts: Contract[]; strikes: number[] } {
  const c = cfg(ticker);
  const S = c.spot;
  const lo = Math.ceil((S * 0.9) / c.step) * c.step;
  const hi = Math.floor((S * 1.1) / c.step) * c.step;
  const strikes: number[] = [];
  for (let k = lo; k <= hi + 1e-9; k += c.step) strikes.push(Math.round(k * 100) / 100);
  const exps = expirationsFor(ticker).expirations.filter((e) => expiries.length === 0 || expiries.includes(e.date));
  const contracts: Contract[] = [];
  for (const e of exps) {
    const r = rng(seedOf(ticker, e.date));
    const hoursLeft = 6 + e.dte * 24;
    const T = hoursLeft / (365 * 24);
    const w = [0.55, 0.62, 0.52, 0.34, 0.6, 0.75][Math.min(5, [0, 1, 2, 3, 4, 7].indexOf(e.dte))] ?? 0.4;
    for (const K of strikes) {
      const u = ((K - S) / S) * 100;
      const roundBoost = Math.abs(K / c.round - Math.round(K / c.round)) < 1e-9 ? 1.7 : 1;
      const noise = 0.75 + r() * 0.5;
      const callShape = 0.22 + 1.5 * g(u - 0.87, 0.3) + 0.55 * g(u - 1.53, 0.4) + 0.35 * g(u - 0.21, 0.18) + 0.25 * g(u - 0.3, 1.2) + 0.15 * g(u - 3, 1.6);
      const putShape = 0.14 + 1.6 * g(u + 1.11, 0.3) + 0.6 * g(u + 1.77, 0.45) + 0.3 * g(u + 0.43, 0.2) + 0.35 * g(u + 2.5, 1.8);
      const base = 9000 * c.oiScale * w * roundBoost * noise;
      const iv = Math.max(0.07, 0.13 + 0.9 * Math.max(0, -u / 100) + 0.25 * Math.max(0, u / 100) + 0.02 * (e.dte === 0 ? 1 : 0));
      contracts.push({ K, expiry: e.date, T, iv, callOI: Math.round(base * callShape), putOI: Math.round(base * putShape) });
    }
  }
  return { contracts, strikes };
}

function gexAt(S: number, ct: Contract, signed: boolean): { call: number; put: number } {
  const gm = gamma(S, ct.K, ct.iv, ct.T);
  const mult = 100 * S * S * 0.01;
  const call = gm * ct.callOI * mult;
  const put = gm * ct.putOI * mult * (signed ? -1 : 1);
  return { call, put };
}

function monthDay(day: string): string {
  const M = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const [, m, d] = day.split("-").map(Number);
  return `${M[m - 1]} ${d}`;
}

function fmtStrike(v: number): string {
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}

function buildMap(ticker: string, expiries: string[], signed: boolean): GexMap {
  const c = cfg(ticker);
  const S = c.spot;
  const { contracts, strikes } = chain(ticker, expiries);
  const rows = strikes.map((K) => ({ strike: K, call_gex: 0, put_gex: 0, net_gex: 0, call_oi: 0, put_oi: 0 }));
  const idx = new Map(strikes.map((k, i) => [k, i]));
  const byExp = new Map<string, { contracts: number; net: number }>();
  for (const ct of contracts) {
    const v = gexAt(S, ct, signed);
    const row = rows[idx.get(ct.K)!];
    row.call_gex += v.call;
    row.put_gex += v.put;
    row.call_oi += ct.callOI;
    row.put_oi += ct.putOI;
    const be = byExp.get(ct.expiry) ?? { contracts: 0, net: 0 };
    be.contracts += 2;
    be.net += v.call + (signed ? v.put : -v.put);
    byExp.set(ct.expiry, be);
  }
  for (const r of rows) r.net_gex = signed ? r.call_gex + r.put_gex : r.call_gex + r.put_gex;

  const callWall = rows.reduce((a, b) => (b.call_gex > a.call_gex ? b : a)).strike;
  const putWall = rows.reduce((a, b) => (Math.abs(b.put_gex) > Math.abs(a.put_gex) ? b : a)).strike;

  // profile curve: hypothetical spot sweep +-10%, about 200 points
  const curve: { spot: number; net_gex: number }[] = [];
  const n = 200;
  for (let i = 0; i <= n; i++) {
    const s = S * (0.9 + (0.2 * i) / n);
    let tot = 0;
    for (const ct of contracts) {
      const v = gexAt(s, ct, true);
      tot += signed ? v.call + v.put : v.call - v.put;
    }
    curve.push({ spot: Math.round(s * 100) / 100, net_gex: tot });
  }
  const zeros: number[] = [];
  if (signed) {
    for (let i = 1; i < curve.length; i++) {
      const a = curve[i - 1];
      const b = curve[i];
      if ((a.net_gex < 0 && b.net_gex >= 0) || (a.net_gex > 0 && b.net_gex <= 0)) {
        const t = a.net_gex / (a.net_gex - b.net_gex);
        zeros.push(Math.round((a.spot + t * (b.spot - a.spot)) * 100) / 100);
      }
    }
  }
  const nearest = zeros.length ? zeros.reduce((a, b) => (Math.abs(b - S) < Math.abs(a - S) ? b : a)) : null;
  const totals = rows.reduce(
    (t, r) => ({ call_gex: t.call_gex + r.call_gex, put_gex: t.put_gex + r.put_gex, net_gex: t.net_gex + r.net_gex }),
    { call_gex: 0, put_gex: 0, net_gex: 0 },
  );
  const regime = !signed ? "neutral" : totals.net_gex > 0 ? "positive" : totals.net_gex < 0 ? "negative" : "neutral";
  const eligible = contracts.length * 2;
  const valid = Math.round(eligible * 0.986);
  const pctv = Math.round((valid / eligible) * 1000) / 10;
  const now = new Date();
  const stamp = new Date(now.getTime() - 4000).toISOString();

  const rel = (lvl: number | null, word: string) =>
    lvl === null ? "" : S >= lvl ? `above ${word} (${word === "the flip" ? lvl.toFixed(2) : fmtStrike(lvl)})` : `below ${word} (${word === "the flip" ? lvl.toFixed(2) : fmtStrike(lvl)})`;
  let lead: string;
  if (signed) {
    const parts = [rel(nearest, "the flip"), rel(callWall, "the call wall")].filter(Boolean);
    lead = `${ticker} at ${S.toFixed(2)} sits ${parts.join(" and ")}. Built from ${pctv}% of the selected chain.`;
  } else {
    lead = `Unsigned view: ${ticker} at ${S.toFixed(2)}, largest gamma at ${fmtStrike(callWall)}. Built from ${pctv}% of the selected chain.`;
  }
  const isIndex = c.feed === "cboe_delayed";
  const oiDate = PRIOR;
  const warnings = [`OI is from ${monthDay(oiDate)} (one session behind).`];
  if (isIndex) warnings.push("Index quotes are delayed about 15 minutes. Spot is the delayed index level.");
  if (!signed) warnings.push("Unsigned view counts every contract by size, so there is no gamma flip.");

  const frozen =
    ticker === "SPY" || ticker === "QQQ" || ticker === "SPX" || ticker === "NDX"
      ? {
          captured_at: nyIso(SESSION, 10, 0, 41),
          call_wall: callWall,
          put_wall: putWall,
          nearest_zero_gamma: nearest === null ? null : Math.round((nearest - c.step * 0.6) * 100) / 100,
        }
      : null;

  return {
    ticker,
    as_of: stamp,
    model_version: "1.1",
    spot: { price: S, ts: stamp, source: isIndex ? "cboe" : "alpaca_sip" },
    feed: {
      kind: isIndex ? "cboe_delayed" : "opra",
      label: isIndex ? "Cboe delayed quotes (about 15 min)" : "OPRA quotes, refreshed 60s",
      stale: false,
      market_open: true,
      last_success: stamp,
    },
    oi_date: oiDate,
    oi_source: {
      kind: "cboe",
      label: "OI: Cboe (prior session, inferred)",
      basis: "inferred",
      oi_date: oiDate,
      alpaca_oi_date: isIndex ? null : oiDate,
      fallback_reason: null,
      matched: isIndex ? null : 2412,
      total: isIndex ? null : 2412,
      differ_frac: isIndex ? null : 0.84,
      payload_ts: stamp,
    },
    coverage: { eligible, valid, pct: pctv, oi_pct: 96.84, gex_pct: 99.62, oi_band_pct: 5, gate_version: "1.2", gate_pct: 98 },
    signed,
    strikes: rows,
    by_expiry: [...byExp.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([expiry, v], i) => ({ expiry, contracts: v.contracts, valid_pct: Math.round((99.2 - i * 0.3) * 10) / 10, net_gex: v.net })),
    totals,
    magnitude: mockMagnitude(ticker, totals.call_gex + totals.put_gex * (signed ? 1 : -1), S, totals.call_gex, totals.put_gex),
    dex: mockDex(ticker, S, Math.abs(totals.call_gex), Math.abs(totals.put_gex), eligible),
    vol_drift: mockVolDrift(ticker, S, c.step, regime),
    levels: { call_wall: callWall, put_wall: putWall, zero_gamma: zeros, nearest_zero_gamma: nearest, regime },
    profile_curve: curve,
    frozen_today: frozen,
    lead,
    warnings,
  };
}

// ---------- Vol Drift and DEX (synthetic; contract section "Vol Drift and DEX") ----------
const MOCK_IV: Record<string, [number, number]> = { SPY: [17.4, 16.1], QQQ: [19.4, 21.0], SPX: [15.5, 15.9], NDX: [20.6, 21.7] };
const PAIR: Record<string, string> = {
  "negative rising": "Negative gamma and rising IV: the conditions where moves tend to get bigger.",
  "positive falling": "Positive gamma and falling IV: the conditions where moves tend to stay smaller.",
  "positive flat": "Positive gamma with flat IV: no change in the size of moves the options market prices.",
};
function mockVolDrift(ticker: string, S: number, step: number, regime: string): VolDrift {
  const [ref, cur] = MOCK_IV[ticker] ?? [41.2, 41.8];
  const chg = Math.round((cur - ref) * 10) / 10;
  const band = Math.round(Math.max(0.5, 0.05 * ref) * 100) / 100;
  const label = chg >= band ? "rising" : chg <= -band ? "falling" : "flat";
  const kLo = Math.floor(S / step) * step;
  return {
    available: true, reason: null, expiry: ticker === "NVDA" ? "2026-10-23" : "2026-10-22", settlement: "PM", session: SESSION,
    current: { iv_pct: cur, ts: nyIso(SESSION, 10, 0, 41), spot: S, k_lo: kLo, k_hi: kLo + step, iv_lo_pct: cur - 0.05, iv_hi_pct: cur + 0.05, usable: 120 },
    reference: { iv_pct: ref, ts: nyIso(SESSION, 9, 45, 0), data_ts: nyIso(SESSION, 9, 45, 0), source: "demo", kind: "snapshot" },
    reference_reason: null, change_pts: chg, label, threshold_pts: band, flat_min_pts: 0.5, flat_rel: 0.05,
    regime: regime as VolDrift["regime"], pairing: PAIR[`${regime} ${label}`] ?? null, tested: false,
    chip: "Untested: no study tests this pairing",
    method: "At-the-money implied volatility of the nearest expiry with at least three full trading days left (synthetic fixture).",
    reference_method: "Reference: the first measurement of the session (synthetic fixture).",
    clock_note: "Time to expiry is calendar time, so part of any change is the clock itself.",
  };
}
function mockDex(_ticker: string, _spot: number, callGex: number, putGex: number, contracts: number): Dex {
  // synthetic sizes in the same order as the GEX totals; not a model output
  const call = callGex * 40;
  const put = putGex * 32;
  return {
    available: true, reason: null, net_usd: call - put, call_usd: call, put_usd: put, contracts, oi_date: PRIOR,
    caveat: "Built from the previous session's open interest, so it sizes yesterday's positioning at today's price and IV. It is not a live flow scoreboard.",
    method: "DEX per contract = delta x OI x 100 x spot, net = calls minus puts (synthetic fixture).",
  };
}

// ---------- net GEX magnitude (synthetic ADV; no volume reference for the indexes) ----------
const MOCK_ADV: Record<string, number> = { SPY: 3.0e10, QQQ: 1.8e10, NVDA: 3.5e10 };
const NO_REF = new Set(["SPX", "NDX"]);
const MOCK_ATR_PCT: Record<string, number> = { SPY: 1.1, QQQ: 1.41, NVDA: 3.4 };
const ATR_ETF: Record<string, string> = { SPX: "SPY", NDX: "QQQ" };
function mockMagnitude(ticker: string, netSigned: number, _spot: number, callGex: number, putGex: number): Magnitude {
  const per = Math.abs(netSigned);
  const none = NO_REF.has(ticker);
  const adv = none ? null : MOCK_ADV[ticker] ?? 2.0e9;
  const etf = ATR_ETF[ticker];
  const atrPct = MOCK_ATR_PCT[etf ?? ticker] ?? 2.0;
  const pctAdv = adv ? (per / adv) * 100 : null;
  const gross = Math.abs(callGex) + Math.abs(putGex);
  const grossPct = adv ? (gross / adv) * 100 : null;
  return {
    adv_usd: adv,
    adv_basis: none ? "none" : "own",
    adv_label: none ? "No free volume reference: index hedging trades mostly in ES and NQ futures" : `${ticker}'s average daily dollar volume`,
    adv_sessions: none ? null : 20,
    adv_window: none ? null : { first: "2026-09-17", last: "2026-10-15" },
    adv_source: none ? null : "demo",
    adv_reason: null,
    hedge_per_1pct_usd: per,
    pct_of_adv: adv ? (per / adv) * 100 : null,
    hedge_for_move: [0.5, 1, 2].map((m) => ({ move_pct: m, usd: per * m })),
    hedge_for_move_method: "Linear: the hedge per 1% times the move. Gamma itself changes as price moves, so the larger the move, the rougher the estimate.",
    per_atr: {
      atr_pct: atrPct,
      atr_usd: null,
      atr_symbol: etf ?? ticker,
      atr_basis: etf ? "etf" : "own",
      atr_pct_basis: etf ? "etf_close" : "spot",
      atr_label: etf ? `ATR% from ${etf}` : `${ticker}'s 14-session ATR`,
      atr_note: etf ? `Alpaca has no index bars, so ${ticker} uses ${etf}'s 14-session ATR as a percent of ${etf}'s last close. A percentage carries over from the ETF to the index; dollar volume does not.` : null,
      atr_sessions: 14,
      atr_window: { first: "2026-09-25", last: "2026-10-15" },
      atr_source: "demo",
      atr_reason: null,
      hedge_per_atr_usd: per * atrPct,
      pct_of_adv_per_atr: pctAdv !== null ? pctAdv * atrPct : null,
      method: "Linear: the hedge per 1% times the ATR in percent. ATR is the simple mean true range of the last 14 completed sessions, divided by spot (SPX and NDX: the ETF's last close). Gamma changes as price moves, so this is a rough size for a typical day, not a forecast.",
    },
    gross: {
      per_1pct_usd: gross,
      pct_of_adv: grossPct,
      hedge_per_atr_usd: gross * atrPct,
      pct_of_adv_per_atr: grossPct !== null ? grossPct * atrPct : null,
      net_to_gross: gross > 0 ? per / gross : null,
      note: "Gross adds call GEX and put GEX without signs, same expiries. Net to gross near 100% means one side dominates. A low ratio means calls and puts largely offset each other: check the Unsigned view to see how much exposure sits on each side.",
    },
    index_note:
      "Index products read larger than single stocks for four reasons: daily expiries keep a lot of short dated gamma open, index positioning is put heavy so less of it nets out, implied volatility is lower so each contract carries more gamma, and a 1% move is a large day for an index but a small one for a volatile stock. The per typical day view (1 ATR) puts them on a more equal footing.",
    percentile: null,
    percentile_value: netSigned,
    percentile_basis: "Signed net GEX over every expiry in the 0 to 7 day window, against this ticker's eligible 10:00 frozen captures before today.",
    history_n: 3,
    history_min: 20,
    ticks_pct: [1, 2, 5, 10],
    explainer:
      "Net GEX estimates how many dollars of the underlying dealers would have to trade, under the sign assumption, for each 1% move. Compare it to how much normally trades: a few percent of daily volume is noticeable; a fraction of a percent is usually lost in normal flow. The size is a model estimate, not a measured flow.",
  };
}

// ---------- captures ----------
function captures(ticker: string, days: number): CaptureRow[] {
  const past = tradingDaysBack(SESSION, days);
  const future: string[] = [];
  let d = addDays(SESSION, 1);
  while (future.length < 4) {
    if (isTradingDay(d)) future.push(d);
    d = addDays(d, 1);
  }
  const r = rng(seedOf("cap", ticker));
  const special: Record<string, CaptureStatus> = {};
  const lastIdx = past.length - 1;
  special[past[lastIdx - 3]] = "late";
  special[past[lastIdx - 7]] = "missed";
  special[past[lastIdx - 15]] = "invalid";
  special[past[lastIdx - 21]] = "late";
  // synthetic: the last 2 sessions were judged by gate 1.2 (|GEX| share), the one before by 1.1 (OI share),
  // earlier rows by the 1.0 contract count
  const gate12 = new Set(past.slice(-2));
  const gate11 = new Set(past.slice(-3, -2));
  const isIndex = ticker === "SPX" || ticker === "NDX";
  const rows: CaptureRow[] = past.map((day) => {
    const st = special[day] ?? "eligible";
    const contracts = isIndex ? 3800 + Math.round(r() * 300) : 1480 + Math.round(r() * 90);
    const cov = st === "invalid" ? 91.4 : 97.2 + r() * 2.3;
    const hash = Math.floor(r() * 0xffffffff).toString(16).padStart(8, "0") + Math.floor(r() * 0xffff).toString(16).padStart(4, "0");
    const gate = gate12.has(day) ? "1.2" : gate11.has(day) ? "1.1" : "1.0";
    if (st === "missed") return { date: day, ticker, status: st, captured_at: null, contracts: null, coverage_pct: null, gate_version: null, oi_coverage_pct: null, gex_coverage_pct: null, hash: null };
    const sec = st === "late" ? 6 * 60 + Math.round(r() * 200) : 20 + Math.round(r() * 40);
    const valid = Math.round((contracts * cov) / 100);
    const bad = contracts - valid;
    const oiCov = gate === "1.0" ? null : Math.round(((gate === "1.1" ? 95.2 : 94.2) + r() * 4.6) * 100) / 100;
    const processed = day !== SESSION;
    return {
      date: day,
      ticker,
      status: st,
      captured_at: nyIso(day, 10, Math.floor(sec / 60), sec % 60),
      contracts,
      valid,
      coverage_pct: Math.round(cov * 10) / 10,
      gate_version: gate,
      oi_coverage_pct: oiCov,
      gex_coverage_pct: oiCov === null ? null : Math.round((98.3 + r() * 1.65) * 100) / 100,
      hash,
      reason: st === "invalid" ? `coverage ${cov.toFixed(2)}% below 95.0%` : st === "late" ? "finished after 10:05 ET" : null,
      invalid_counts: (bad > 0 ? (st === "invalid" ? { iv_stale: Math.round(bad * 0.6), mid_unusable: bad - Math.round(bad * 0.6) } : { mid_unusable: bad }) : null) as Record<string, number> | null,
      processed,
      study_status: processed && !isIndex && st === "eligible" ? "complete" : null,
      study_reason: null,
    };
  });
  for (const day of future) rows.push({ date: day, ticker, status: "future", captured_at: null, contracts: null, coverage_pct: null, gate_version: null, oi_coverage_pct: null, gex_coverage_pct: null, hash: null });
  return rows;
}

// ---------- replay ----------
// The synthetic "today" has a capture but no after-close step yet: replay and recap answer processed:false
// and POST /api/recap/generate answers 409, as the real API does.
const UNPROCESSED = new Set([SESSION]);

function unprocessedReplay(ticker: string, date: string): Replay {
  const full = replay(ticker, PRIOR);
  return {
    ticker,
    date,
    resolution: full.resolution,
    processed: false,
    processed_at: null,
    session: null,
    window: null,
    study: { status: "pending", eligible: false, reasons: ["not_processed"], capture_status: "eligible", missing_minutes: null, bars_through: null, prepared_at: null },
    bars: [],
    vwap: [],
    frozen: full.frozen ? { ...full.frozen, captured_at: nyIso(date, 10, 0, 41) } : null,
    comparators: [],
    touches: [],
    behavior: null,
    levels_tested: [],
    snapshots: full.snapshots.slice(0, 3).map((x, i) => ({ ...x, time: nyIso(date, 10, i * 15), used: true })),
    lead: `${ticker} on ${monthDay(date)} is not processed yet.`,
    notes: ["Synthetic fixture", "Not processed yet: run the after-close processing step."],
  };
}

function replay(ticker: string, date: string): Replay {
  const c = cfg(ticker);
  const r = rng(seedOf("replay", ticker, date));
  const flip = seedOf(date) % 3 === 0 ? -1 : 1;
  const ref = c.spot * (1 - 0.0011 * flip);
  const P = (pctOff: number) => ref * (1 + pctOff / 100);
  // shape of the day in percent of the open, minute index from 09:30
  const keys: [number, number][] = [
    [0, 0],
    [36, 0.105],
    [88, 0.343],
    [110, 0.594],
    [132, 0.97],
    [137, 0.93],
    [162, 0.645],
    [200, 0.45],
    [260, 0.2],
    [330, 0.4],
    [389, 0.3],
  ].map(([i, p]) => [i, flip * p] as [number, number]);
  const atr = c.spot * 0.0104;
  const isIndex = ticker === "SPX" || ticker === "NDX";
  const closes: number[] = [];
  for (let i = 0; i < 390; i++) {
    let a = keys[0];
    let b = keys[keys.length - 1];
    for (let j = 0; j < keys.length - 1; j++)
      if (i >= keys[j][0] && i <= keys[j + 1][0]) {
        a = keys[j];
        b = keys[j + 1];
        break;
      }
    const t = (i - a[0]) / Math.max(1, b[0] - a[0]);
    closes.push(P(a[1] + (b[1] - a[1]) * t) + (r() - 0.5) * atr * 0.022);
  }
  let bars: Bar[] = closes.map((cl, i) => {
    const o = i === 0 ? cl - atr * 0.01 : closes[i - 1];
    const wig = atr * (0.012 + r() * 0.03);
    const minute = 30 + i;
    return {
      t: nyIso(date, 9 + Math.floor(minute / 60), minute % 60),
      o,
      h: Math.max(o, cl) + wig * r(),
      l: Math.min(o, cl) - wig * r(),
      c: cl,
      v: Math.round((i < 30 || i > 360 ? 9000 : 4000) * (0.5 + r()) * (isIndex ? 0 : 1) + (isIndex ? 1 : 0)),
    };
  });
  // levels (frozen 10:00 + free comparators)
  const roundTo = (v: number, s: number) => Math.round(v / s) * s;
  const callWall = roundTo(P(flip > 0 ? 0.97 : 1.6), c.step);
  const putWall = roundTo(P(flip > 0 ? -1.0 : -0.95), c.step);
  const zero = Math.round(P(flip > 0 ? -0.33 : 0.42) * 100) / 100;
  const priorHigh = Math.round(P(flip * 0.594) * 100) / 100;
  const priorLow = Math.round(P(-0.55) * 100) / 100;
  const priorClose = Math.round(P(-0.09) * 100) / 100;
  const comps: Comparator[] = [
    { family: "call_wall", price: callWall, label: `Call wall ${fmtStrike(callWall)}` },
    { family: "put_wall", price: putWall, label: `Put wall ${fmtStrike(putWall)}` },
    { family: "zero_gamma", price: zero, label: `Zero gamma ${zero.toFixed(2)}` },
    { family: "prior_high", price: priorHigh, label: `Prior high ${priorHigh.toFixed(2)}` },
    { family: "prior_low", price: priorLow, label: `Prior low ${priorLow.toFixed(2)}` },
    { family: "prior_close", price: priorClose, label: `Prior close ${priorClose.toFixed(2)}` },
  ];
  if (!isIndex) {
    const pmH = Math.round(P(0.21) * 100) / 100;
    const pmL = Math.round(P(-0.22) * 100) / 100;
    comps.push({ family: "pm_high", price: pmH, label: `Premarket high ${pmH.toFixed(2)}` });
    comps.push({ family: "pm_low", price: pmL, label: `Premarket low ${pmL.toFixed(2)}` });
  }
  for (let k = Math.ceil((ref - 2 * atr) / c.round) * c.round; k <= ref + 2 * atr; k += c.round) {
    if (Math.abs(k - callWall) < 1e-9 || Math.abs(k - putWall) < 1e-9) continue;
    comps.push({ family: "round", price: k, label: `Round ${fmtStrike(k)}` });
  }

  // touches from 10:06 on, band 0.05 ATR, reaction 30 min later in ATR toward the approach side
  const band = 0.05 * atr;
  const start = 36;
  const touches: Touch[] = [];
  for (const cmp of comps) {
    const at = bars[start];
    if (at.l - band <= cmp.price && at.h + band >= cmp.price) continue; // inside the band at 10:06
    for (let i = start; i < bars.length; i++) {
      const b = bars[i];
      if (b.l - band <= cmp.price && b.h + band >= cmp.price) {
        const approach = bars[i - 1].c < cmp.price ? "from_below" : "from_above";
        const j = i + 30;
        const reaction = j < bars.length ? ((approach === "from_below" ? cmp.price - bars[j].c : bars[j].c - cmp.price) / atr) : null;
        touches.push({
          family: cmp.family,
          price: cmp.price,
          time: b.t,
          approach,
          gap_through: false,
          reaction_atr: reaction === null ? null : Math.round(reaction * 100) / 100,
          counted: reaction !== null,
          excluded_reason: reaction === null ? "horizon_incomplete" : null,
        });
        break;
      }
    }
  }
  touches.sort((a, b) => a.time.localeCompare(b.time));

  // VWAP and behavior
  let pv = 0;
  let vv = 0;
  const vwap = bars.map((b) => {
    const vol = Math.max(1, b.v);
    pv += ((b.h + b.l + b.c) / 3) * vol;
    vv += vol;
    return { t: b.t, v: pv / vv };
  });
  const after = closes.slice(start);
  const path = after.slice(1).reduce((s, v, i) => s + Math.abs(v - after[i]), 0);
  const te = Math.abs(after[after.length - 1] - after[0]) / path;
  const hi = Math.max(...bars.slice(start).map((b) => b.h));
  const lo = Math.min(...bars.slice(start).map((b) => b.l));
  const rets = after.slice(1).map((v, i) => Math.log(v / after[i]));
  const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
  const sd = Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (rets.length - 1));
  let crosses = 0;
  for (let i = start + 1; i < bars.length; i++) if ((bars[i - 1].c - vwap[i - 1].v) * (bars[i].c - vwap[i].v) < 0) crosses++;
  const regime = flip > 0 ? "positive" : "negative";
  const behavior = {
    regime: regime as "positive" | "negative",
    trend_efficiency: Math.round(te * 100) / 100,
    range_atr: Math.round(((hi - lo) / atr) * 100) / 100,
    realized_vol: Math.round(sd * Math.sqrt(390) * 10000) / 10000,
    vwap_crosses: crosses,
    close_location: Math.round(((after[after.length - 1] - lo) / (hi - lo)) * 100) / 100,
    variance_ratio: 0.83,
    atr: Math.round(atr * 100) / 100,
  };

  let vwapOut = vwap;
  if (isIndex) {
    // SPX/NDX replay comes from 15-minute snapshots, lower resolution
    const agg: Bar[] = [];
    for (let i = 0; i < bars.length; i += 15) {
      const chunk = bars.slice(i, i + 15);
      agg.push({
        t: chunk[0].t,
        o: chunk[0].o,
        h: Math.max(...chunk.map((b) => b.h)),
        l: Math.min(...chunk.map((b) => b.l)),
        c: chunk[chunk.length - 1].c,
        v: 0,
      });
    }
    bars = agg;
    vwapOut = [];
  }

  const snapshots = [];
  for (let m = 15; m <= 390; m += 15) {
    const minute = 30 + m;
    snapshots.push({
      time: nyIso(date, 9 + Math.floor(minute / 60), minute % 60),
      call_wall: callWall,
      put_wall: m > 240 ? putWall + c.step : putWall,
      nearest_zero_gamma: Math.round((zero + (m - 30) * 0.004 * c.step) * 100) / 100,
      net_gex: (flip > 0 ? 1 : -1) * (2.1e9 - m * 2e6) * (c.oiScale * 1.5),
    });
  }
  const firstGex = touches.find((t) => t.family === "call_wall" || t.family === "put_wall" || t.family === "zero_gamma");
  const fam = (f: string) => (f === "call_wall" ? "call wall" : f === "put_wall" ? "put wall" : "gamma flip");
  let lead = `On ${monthDay(date)}, ${ticker} was in ${regime} gamma at 10:00.`;
  if (firstGex) {
    const tt = new Date(firstGex.time);
    const hh = String(tt.getUTCHours() - 4).padStart(2, "0");
    const mm = String(tt.getUTCMinutes()).padStart(2, "0");
    lead += firstGex.reaction_atr === null
      ? ` Price touched the ${fam(firstGex.family)} at ${hh}:${mm}.`
      : ` Price touched the ${fam(firstGex.family)} at ${hh}:${mm} and was ${Math.abs(firstGex.reaction_atr).toFixed(2)} ATR ${firstGex.reaction_atr >= 0 ? "back from" : "past"} it 30 minutes later.`;
  } else lead += " Price did not reach the frozen GEX levels after 10:06.";

  return {
    ticker,
    date,
    resolution: isIndex ? "15m" : "1m",
    processed: true,
    processed_at: nyIso(date, 16, 20, 4),
    session: { date, is_session: true, open: "09:30", close: "16:00", early_close: false, calendar_source: "synthetic", completed: true },
    window: { start: "10:06", end: "16:00" },
    study: {
      status: isIndex ? "not_applicable" : "complete",
      eligible: !isIndex,
      reasons: isIndex ? ["index_low_resolution"] : [],
      capture_status: "eligible",
      missing_minutes: 0,
      bars_through: nyIso(date, 16, 0),
      prepared_at: nyIso(date, 16, 20, 4),
    },
    bars,
    vwap: vwapOut,
    frozen: {
      captured_at: nyIso(date, 10, 0, 38),
      status: "eligible",
      spot: closes[30],
      call_wall: callWall,
      put_wall: putWall,
      zero_gamma: [zero],
      net_gex: (flip > 0 ? 1 : -1) * 2.04e9 * c.oiScale,
      regime,
    },
    comparators: comps,
    touches,
    // every GEX level's tested result, as the real replay sends it (the UI never infers "not touched")
    levels_tested: isIndex
      ? []
      : ([["call_wall", callWall], ["put_wall", putWall], ["zero_gamma", zero]] as const).map(([family, p]) => ({
          family,
          price: p,
          status: touches.some((t) => t.family === family) ? ("touched" as const) : ("not_touched" as const),
          excluded_reason: null,
        })),
    bars_complete: !isIndex,
    behavior,
    snapshots: snapshots.map((x) => ({ ...x, status: "eligible" as const, used: true })),
    lead,
    notes: ["Synthetic fixture", ...(isIndex ? ["Index replay uses 15-minute snapshots, lower resolution."] : [])],
  };
}

// ---------- recap ----------
const recapCache = new Map<string, Recap>();

// Mock mode never runs the Python validator, so a mock recap is never marked as validated (model "mock",
// validated false, and the card says so). The wording still follows the real template: typed numbers
// copied as given, "From 10:06 to 16:00", no percent conversions, no clock words for durations, no claim that a level
// was untouched (that needs levels_tested).
const MOCK_MODEL = "mock";
function mockTouchSentence(rp: ReturnType<typeof replay>): string {
  const t = rp.touches.find((x) => x.family === "call_wall" || x.family === "put_wall" || x.family === "zero_gamma");
  if (!t) return "";
  const word = t.family === "zero_gamma" ? "gamma flip" : t.family.replace("_", " ");
  const hhmm = new Date(t.time).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "America/New_York" });
  const from = t.approach === "from_below" ? "from below" : "from above";
  return ` Price touched the ${word} (${t.price}) at ${hhmm} ${from}.`;
}

function recapTemplate(ticker: string, date: string): Recap {
  const rp = replay(ticker, date);
  const b = rp.behavior!;
  const weekday = new Date(`${date}T12:00:00Z`).toLocaleDateString("en-US", { weekday: "long", timeZone: "UTC" });
  const text =
    `On ${weekday}, ${monthDay(date)}, ${ticker} was in ${b.regime} gamma at the 10:00 capture. ` +
    `From 10:06 to 16:00, trend efficiency was ${b.trend_efficiency?.toFixed(2)} and the range was ${b.range_atr?.toFixed(2)} ATR. ` +
    `Price crossed VWAP ${b.vwap_crosses} times and the close location was ${b.close_location?.toFixed(2)}.` +
    mockTouchSentence(rp);
  const f = rp.frozen;
  const gexLevels = (["call_wall", "put_wall", "zero_gamma"] as const).map((fam) => {
    const lvl = fam === "zero_gamma" ? f?.zero_gamma[0] : f?.[fam];
    const hit = rp.touches.some((x) => x.family === fam);
    return { level: fam === "zero_gamma" ? "gamma flip" : fam.replace("_", " "), price: lvl ?? 0, status: hit ? "touched" : "not_touched" };
  });
  const payload: RecapPayload = {
    ticker,
    date,
    weekday: new Date(`${date}T12:00:00Z`).toLocaleDateString("en-US", { weekday: "long", timeZone: "UTC" }),
    date_label: monthDay(date),
    freeze_time: "10:00",
    measured_from: "10:06",
    measured_to: "16:00",
    window: "10:06 to 16:00",
    early_close: false,
    capture_status: "eligible",
    eligible: true,
    exclusion_reasons: [],
    regime_at_10: b.regime,
    spot_at_10: f?.spot ?? null,
    call_wall: f?.call_wall ?? null,
    put_wall: f?.put_wall ?? null,
    nearest_gamma_flip: f?.zero_gamma[0] ?? null,
    gex_levels: gexLevels,
    gex_levels_not_touched: gexLevels.filter((g) => g.status === "not_touched").map((g) => g.level),
    notes: ["Synthetic fixture: seeded prices and made-up levels, not market data."],
  };
  return {
    ticker,
    date,
    processed: true,
    eligible: true,
    reason: null,
    payload,
    text,
    source: "template",
    model: MOCK_MODEL,
    validated: false,
    rejected: [],
  };
}

function recapGet(ticker: string, date: string): Recap {
  if (UNPROCESSED.has(date))
    return {
      ticker,
      date,
      processed: false,
      eligible: false,
      reason: "not processed yet",
      payload: null,
      text: "No recap: not processed yet. Recaps exist only for sessions processed after the close.",
      source: "none",
      model: null,
      validated: false,
      rejected: [],
    };
  return recapCache.get(`${ticker}|${date}`) ?? recapTemplate(ticker, date);
}

async function recapGenerate(ticker: string, date: string): Promise<Recap> {
  await sleep(UNPROCESSED.has(date) ? 300 : 5200);
  if (UNPROCESSED.has(date)) {
    const { ApiError } = await import("../api/client");
    throw new ApiError(409, `No recap for ${ticker} ${date}: not processed yet. Run the after-close processing step first.`);
  }
  const base = recapTemplate(ticker, date);
  const rp = replay(ticker, date);
  const b = rp.behavior!;
  let out: Recap;
  if (ticker === "QQQ") {
    out = {
      ...base,
      rejected: ['Number 0.47 is not in the payload.', 'Advice word in sentence 2, from the banned list.'],
    };
  } else {
    out = {
      ...base,
      text:
        `On ${monthDay(date)}, ${ticker} was in a ${b.regime} gamma regime at 10:00. ` +
        `From 10:06 to 16:00, the range was ${b.range_atr?.toFixed(2)} ATR and trend efficiency was ${b.trend_efficiency?.toFixed(2)}. ` +
        `${ticker} crossed VWAP ${b.vwap_crosses} times and the close location in range was ${b.close_location?.toFixed(2)}.` +
        mockTouchSentence(rp),
      source: "ai",
      model: MOCK_MODEL,
      validated: false,
      rejected: [],
    };
  }
  recapCache.set(`${ticker}|${date}`, out);
  return out;
}

// ---------- scorecard ----------

const MOCK_PROTOCOL = {
  version: "1.0",
  locked_at: "2026-10-19",
  capture_gate: "1.2",
  model_version: "1.1",
  oi_source: "cboe",
  mixed_oi_sources: true,
  capture_gates: {
    "1.0": "usable OI and IV on >= 95% of contracts within 10% of spot (contract count)",
    "1.1": "valid contracts hold >= 95% of the open interest within 5% of spot",
    "1.2": "valid contracts hold >= 98% of the estimated absolute GEX within 5% of spot",
  },
};

function scorecard(): Scorecard {
  const withResults = /[?&]results=1/.test(window.location.hash) || /[?&]results=1/.test(window.location.search);
  const isG = (f: string) => f === "call_wall" || f === "put_wall" || f === "zero_gamma";
  const families = [
    ["call_wall", 9],
    ["put_wall", 6],
    ["zero_gamma", 11],
    ["round", 21],
    ["prior_high", 7],
    ["prior_low", 5],
    ["prior_close", 8],
    ["pm_high", 6],
    ["pm_low", 4],
  ] as const;
  const fam = (family: string, n: number, s: number) => ({
    family,
    gex: isG(family),
    first_touches: n,
    sessions_with_touch: s,
    gate: isG(family) ? 30 : null,
    gate_sessions: isG(family) ? 20 : null,
  });
  if (!withResults) {
    return {
      protocol: MOCK_PROTOCOL,
      study1: {
        ticker: "SPY",
        sessions: 12,
        sessions_by_gate: { "1.0": 10, "1.1": 1, "1.2": 1 },
        sessions_by_oi_source: { alpaca: 11, cboe: 1 },
        gate_sessions: 40,
        families: families.map(([f, n]) => fam(f, n, Math.min(12, n))),
        passed: false,
        results: null,
      },
      study2: {
        positive: { sessions: 9, after_lock: 9, gate: 40, gate_after_lock: 20, sessions_by_gate: { "1.0": 7, "1.1": 1, "1.2": 1 } },
        negative: { sessions: 3, after_lock: 3, gate: 40, gate_after_lock: 20, sessions_by_gate: { "1.0": 3 } },
        excluded_neutral: 0,
        passed: false,
        results: null,
      },
      lead: "Still collecting: 12 of 40 sessions. No verdict yet.",
    };
  }
  const res = [
    ["call_wall", 34, 31, 0.21, 0.09, 0.33],
    ["put_wall", 31, 29, 0.17, 0.04, 0.3],
    ["zero_gamma", 38, 33, 0.06, -0.06, 0.18],
    ["round", 96, 41, 0.12, 0.05, 0.19],
    ["prior_high", 33, 30, 0.14, 0.02, 0.26],
    ["prior_low", 30, 28, 0.1, -0.03, 0.23],
    ["prior_close", 41, 35, 0.04, -0.07, 0.15],
    ["pm_high", 32, 30, 0.08, -0.05, 0.21],
    ["pm_low", 30, 27, 0.05, -0.09, 0.19],
  ] as const;
  const s1lead = "Across 44 SPY sessions, the median 30-minute reaction after a first touch, in ATR, was call wall 0.21 vs placebo 0.04; put wall 0.17 vs placebo 0.03; zero gamma 0.06 vs placebo 0.05.";
  const s2lead = "Median trend efficiency from 10:06 to the close was 0.16 on 46 positive-gamma sessions and 0.27 on 41 negative-gamma sessions.";
  return {
    protocol: MOCK_PROTOCOL,
    study1: {
      ticker: "SPY",
      sessions: 44,
      sessions_by_gate: { "1.0": 1, "1.2": 43 },
      gate_sessions: 40,
      families: res.map(([f, n, s]) => fam(f, n, s)),
      passed: true,
      results: {
        sessions: 44,
        lead: s1lead,
        method: "Median reaction in ATR, positive means back toward the approach side. 90% interval from a session-clustered bootstrap (2000 resamples of whole sessions).",
        families: res.map(([family, n, s, m, lo, hi]) => ({
          family,
          gex: isG(family),
          first_touches: n,
          sessions_with_touch: s,
          touch_rate: 0.3,
          median_reaction_atr: m,
          ci90: [lo, hi] as [number, number],
          placebo: isG(family) ? { events: 3100, touch_rate: 0.31, median_reaction_atr: family === "zero_gamma" ? 0.05 : family === "put_wall" ? 0.03 : 0.04, ci90: [0.0, 0.08] as [number, number] } : null,
        })),
      },
    },
    study2: {
      positive: { sessions: 46, after_lock: 46, gate: 40, gate_after_lock: 20, sessions_by_gate: { "1.0": 1, "1.2": 45 } },
      negative: { sessions: 41, after_lock: 41, gate: 40, gate_after_lock: 20, sessions_by_gate: { "1.2": 41 } },
      excluded_neutral: 0,
      passed: true,
      results: {
        lead: s2lead,
        by_sign: {
          positive: { sessions: 46, median_trend_efficiency: 0.16, ci90: [0.12, 0.2], secondary_medians: { range_atr: 0.74, realized_vol: 0.0071, vwap_crosses: 15, close_location: 0.48, variance_ratio: 0.93 } },
          negative: { sessions: 41, median_trend_efficiency: 0.27, ci90: [0.21, 0.34], secondary_medians: { range_atr: 1.02, realized_vol: 0.0093, vwap_crosses: 9, close_location: 0.44, variance_ratio: 1.08 } },
        },
        oos: { target: "trend_efficiency", method: "expanding window OLS, first 20 sessions train only", n_predictions: 67, mae_baseline: 0.081, mae_with_gex: 0.077, mae_change_pct: -4.94 },
      },
    },
    lead: `${s1lead} ${s2lead}`,
  };
}

// ---------- router ----------
export async function mockFetch(path: string, init?: RequestInit): Promise<unknown> {
  const url = new URL(path, "http://mock.local");
  const p = url.searchParams;
  await sleep(url.pathname === "/api/map" ? 260 : 140);
  switch (url.pathname) {
    case "/api/health": {
      const h: Health = {
        status: "ok",
        demo: true,
        feeds: { alpaca_options: "opra", stocks: "sip", cboe: true },
        market_open: true,
        last_frozen_capture: { ticker: "SPY", date: SESSION, status: "eligible" },
        rate: { used_last_min: 7, cap: 60 },
      };
      return h;
    }
    case "/api/update":
      return { enabled: false, current: "1.1.0", latest: null, available: false, url: "", title: "", checked_at: null, error: null };
    case "/api/tickers":
      return { pinned: ["SPY", "QQQ", "SPX", "NDX"] };
    case "/api/search": {
      const term = (p.get("q") || "").toUpperCase();
      return UNIVERSE.filter((u) => u.symbol.startsWith(term) || u.name.toUpperCase().includes(term)).slice(0, 10);
    }
    case "/api/expirations":
      return expirationsFor((p.get("ticker") || "SPY").toUpperCase());
    case "/api/map": {
      const t = (p.get("ticker") || "SPY").toUpperCase();
      const ex = (p.get("expiries") || "").split(",").filter(Boolean);
      return buildMap(t, ex, p.get("signed") !== "false");
    }
    case "/api/captures":
      return captures((p.get("ticker") || "SPY").toUpperCase(), Number(p.get("days") || 30));
    case "/api/replay/dates": {
      const t = (p.get("ticker") || "SPY").toUpperCase();
      const done = tradingDaysBack(PRIOR, 30).reverse();
      return { ticker: t, dates: [SESSION, ...done], processed: done };
    }
    case "/api/replay": {
      const t = (p.get("ticker") || "SPY").toUpperCase();
      const d = p.get("date") || PRIOR;
      return UNPROCESSED.has(d) ? unprocessedReplay(t, d) : replay(t, d);
    }
    case "/api/recap":
      return recapGet((p.get("ticker") || "SPY").toUpperCase(), p.get("date") || PRIOR);
    case "/api/recap/generate": {
      const body = JSON.parse(String(init?.body || "{}"));
      return recapGenerate(String(body.ticker || "SPY").toUpperCase(), String(body.date || PRIOR));
    }
    case "/api/scorecard":
      return scorecard();
    case "/api/futures":
      throw new Error("The mock build has no Futures view: run the API with GEXLAB_DEMO=1 for a synthetic one");
    default:
      throw new Error(`Mock has no route for ${url.pathname}`);
  }
}

export const MOCK_SESSION = SESSION;
