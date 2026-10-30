// Number and time formatting. All numbers render in JetBrains Mono with tabular figures.
// Negative numbers use the ASCII hyphen-minus on purpose (house rule: no dash characters).

export function gex(v: number | null | undefined, opts: { sign?: boolean } = {}): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "n/a";
  const a = Math.abs(v);
  let body: string;
  if (a >= 1e9) body = `$${(a / 1e9).toFixed(a >= 1e11 ? 0 : a >= 1e10 ? 1 : 2)}B`;
  else if (a >= 1e6) body = `$${(a / 1e6).toFixed(a >= 1e8 ? 0 : 1)}M`;
  else if (a >= 1e3) body = `$${(a / 1e3).toFixed(0)}K`;
  else body = `$${a.toFixed(0)}`;
  if (v < 0) return `-${body}`;
  return opts.sign && v > 0 ? `+${body}` : body;
}

export function price(v: number | null | undefined, digits?: number): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "n/a";
  const d = digits ?? (Math.abs(v) >= 1000 ? 2 : 2);
  return v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

/** Strike: drop trailing .00, keep .5 */
export function strike(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "n/a";
  const isInt = Math.abs(v - Math.round(v)) < 1e-9;
  return v.toLocaleString("en-US", { minimumFractionDigits: isInt ? 0 : 1, maximumFractionDigits: 2 });
}

export function int(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "n/a";
  return Math.round(v).toLocaleString("en-US");
}

export function pct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "n/a";
  return `${v.toFixed(digits)}%`;
}

export function signed(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "n/a";
  const s = Math.abs(v).toFixed(digits);
  return v > 0 ? `+${s}` : v < 0 ? `-${s}` : s;
}

export function num(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "n/a";
  return v.toFixed(digits);
}

const NY = "America/New_York";

const fmtTime = new Intl.DateTimeFormat("en-US", { timeZone: NY, hour: "2-digit", minute: "2-digit", hour12: false });
const fmtTimeS = new Intl.DateTimeFormat("en-US", {
  timeZone: NY,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

/** "10:41" or "10:41:07" in New York time. */
export function nyTime(iso: string | null | undefined, seconds = false): string {
  if (!iso) return "n/a";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "n/a";
  return (seconds ? fmtTimeS : fmtTime).format(d).replace(/^24:/, "00:");
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** Parse a YYYY-MM-DD calendar date with no time zone shift. */
export function parseDay(day: string): { y: number; m: number; d: number; dow: number } {
  const [y, m, d] = day.split("-").map(Number);
  const dow = new Date(Date.UTC(y, m - 1, d)).getUTCDay();
  return { y, m, d, dow };
}

/** "Oct 16" */
export function dayShort(day: string | null | undefined): string {
  if (!day) return "n/a";
  const { m, d } = parseDay(day);
  return `${MONTHS[m - 1]} ${d}`;
}

/** "Fri Oct 16" */
export function dayLong(day: string | null | undefined): string {
  if (!day) return "n/a";
  const { m, d, dow } = parseDay(day);
  return `${DOW[dow]} ${MONTHS[m - 1]} ${d}`;
}

/** "Fri Oct 16, 2026" */
export function dayFull(day: string | null | undefined): string {
  if (!day) return "n/a";
  const { y } = parseDay(day);
  return `${dayLong(day)}, ${y}`;
}

const nyParts = new Intl.DateTimeFormat("en-US", {
  timeZone: NY,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

/**
 * lightweight-charts renders timestamps as UTC. Convert a real instant into "New York wall clock
 * expressed as UTC seconds" so the time axis reads in exchange time.
 */
export function nyChartTime(iso: string): number {
  const d = new Date(iso);
  const p: Record<string, number> = {};
  for (const part of nyParts.formatToParts(d)) if (part.type !== "literal") p[part.type] = Number(part.value);
  const hour = p.hour === 24 ? 0 : p.hour;
  return Date.UTC(p.year, p.month - 1, p.day, hour, p.minute, p.second) / 1000;
}

/** Today's date in New York as YYYY-MM-DD. */
export function nyToday(): string {
  const p: Record<string, string> = {};
  for (const part of nyParts.formatToParts(new Date())) p[part.type] = part.value;
  return `${p.year}-${p.month}-${p.day}`;
}

export function familyLabel(f: string): string {
  switch (f) {
    case "call_wall":
      return "Call wall";
    case "put_wall":
      return "Put wall";
    case "zero_gamma":
      return "Gamma flip";
    case "call_put_wall":
      return "Call + put walls";
    case "round":
      return "Round number";
    case "prior_high":
      return "Prior high";
    case "prior_low":
      return "Prior low";
    case "prior_close":
      return "Prior close";
    case "pm_high":
      return "Premarket high";
    case "pm_low":
      return "Premarket low";
    case "placebo":
      return "Placebo";
    default:
      return f.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
  }
}

/** Brand color per level family. GEX families carry the accents, free levels stay gray. */
export function familyColor(f: string): string {
  switch (f) {
    case "call_wall":
      return "var(--green)";
    case "put_wall":
      return "var(--purple-1)";
    case "zero_gamma":
      return "var(--purple-2)";
    default:
      return "var(--muted)";
  }
}

export function isGexFamily(f: string): boolean {
  return f === "call_wall" || f === "put_wall" || f === "zero_gamma" || f === "call_put_wall";
}

/** Cash indexes quote in index points, everything else in dollars. */
export function isIndex(ticker: string): boolean {
  return ticker === "SPX" || ticker === "NDX";
}

/** Spot and level prices: whole index points for SPX/NDX, cents for everything else. */
export function px(v: number | null | undefined, ticker: string): string {
  return price(v, isIndex(ticker) ? 0 : 2);
}

/** "$2.35 below spot" (SPY, QQQ, stocks) or "4 index points above spot" (SPX, NDX). ref names what spot is
 *  ("the prior close" while Cboe still carries the prior session's index value). */
export function distText(level: number | null | undefined, spot: number, ticker: string, ref = "spot"): string {
  if (level === null || level === undefined || !Number.isFinite(level)) return "none in range";
  const d = level - spot;
  if (Math.abs(d) < 0.005) return `at ${ref}`;
  const dp = isIndex(ticker) ? 0 : 2;
  const a = Math.abs(d).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  const side = d > 0 ? "above" : "below";
  return isIndex(ticker) ? `${a} index points ${side} ${ref}` : `$${a} ${side} ${ref}`;
}

/** What distances are measured from: "spot", or "the prior close" when the map's spot is the prior session's. */
export function spotRef(basis: string | null | undefined): string {
  return basis === "prior_close" ? "the prior close" : "spot";
}

const fmtNyDay = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "short", day: "numeric" });

/** "Oct 28" in New York time. */
export function nyDay(iso: string | null | undefined): string {
  if (!iso) return "n/a";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "n/a" : fmtNyDay.format(d);
}

/**
 * One name for one concept. API text written before the vocabulary pass says "the flip" or
 * "zero gamma"; on screen it always reads "gamma flip". Wording only, no number changes.
 */
export function vocab(text: string): string {
  return text
    .replace(/\bthe nearest zero gamma\b/g, "the gamma flip")
    .replace(/\bnearest zero gamma\b/g, "gamma flip")
    .replace(/\bZero gamma\b/g, "Gamma flip")
    .replace(/\bzero gamma\b/g, "gamma flip")
    .replace(/\bthe flip\b/g, "the gamma flip");
}

/** GEX units, written out once wherever a GEX total appears. */
export const GEX_UNITS = "$ of delta exposure change per 1% underlying move";
