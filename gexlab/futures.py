"""Futures view: SPX, NDX, SPY and QQQ levels as /ES (or /MNQ) equivalents. A display transform only.

Nothing here feeds the GEX math, the levels, the recorder, the frozen captures or the studies. The map keeps
every number in index or ETF units; GET /api/futures returns the conversion (`ratio`, `basis`, `tick`) and the UI
shows futures = round_tick(level * ratio + basis).

Basis, from put-call parity on the option chain the map already pulls from Cboe (no new feed, no account):
- /ES and /MES settle to the SPX special opening quotation on the quarterly expiry (third Friday of Mar, Jun, Sep,
  Dec), the same settlement as the AM settled monthly SPX options (root SPX, not SPXW). /NQ and /MNQ settle to the
  NDX one, like the AM settled monthly NDX options. So the forward of those options IS the futures fair value.
- Front contract: the nearest quarterly whose roll date is still ahead. The roll date is CME's customary one, the
  Monday FUTURES_ROLL_DAYS (4) calendar days before the third Friday; ON that date the view moves to the next quarter.
- F_K = K + e^(rT) * (C_mid - P_mid) at each strike with usable call and put mids (alpaca.mid_usable, the map's own
  check). Pass 1 takes the strikes nearest spot, pass 2 the FUTURES_PARITY_STRIKES nearest that first forward, where
  C - P is near zero and r barely matters. F = median of pass 2. Fewer than FUTURES_PARITY_MIN usable pairs:
  unavailable, never a guess.
- basis = F - index spot, both from the SAME Cboe payload, so the 15 minute delay cancels.
- The quarterly expiry is outside the map's 0 to 7 day window. It is read here for parity only and never joins
  the map's GEX, its coverage gate or anything stored.

SPY and QQQ: the ETF level goes to index points by ratio = index / ETF, priced at the same instant: the index value
from the Cboe payload (its market-data time, `last_trade_time`) and the last ETF trade at or before that time from
Alpaca REST. Pairing the delayed index with a live ETF price would put up to 15 minutes of market move into the
ratio. Without Alpaca keys there is no ETF price and the SPY/QQQ view is unavailable (so is the SPY/QQQ map).

Override: the user types the live futures price they see; basis = that price minus the latest index value. With Alpaca
keys and the market open that index value is live (last ETF trade times the ratio); otherwise it is the Cboe index,
about 15 minutes delayed (or the close), and labeled so. A gap to the parity basis over the family's tolerance warns.
"""
from __future__ import annotations

import json
import math
import statistics
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from gexlab import config
from gexlab.engine.calendar import NY, UTC, TradingCalendar, iso_z, ny_today, third_friday, time_to_expiry, valuation_time
from gexlab.models import parse_occ
from gexlab.sources.base import SourceError

ESTIMATE_LABEL = "Estimate: futures equivalents from the option chain, not a futures quote"


# ---------------------------------------------------------------- contract and roll

@dataclass(frozen=True)
class FuturesContract:
    index: str                 # SPX | NDX
    symbol: str                # /ESZ26
    also: tuple[str, ...]      # ("/MESZ26",): same index, same price
    month: str                 # "2026-12"
    expiry: date               # nominal last trading day: the third Friday
    roll_date: date            # the front moves to the next quarter on this date

    def to_json(self) -> dict:
        y, m = self.expiry.year, self.expiry.month
        return {"index": self.index, "symbol": self.symbol, "also": list(self.also), "month": self.month,
                "label": f"{self.expiry.strftime('%b')} {y}", "expiry": self.expiry.isoformat(),
                "roll_date": self.roll_date.isoformat(), "code": f"{config.FUTURES_MONTHS[m]}{y % 100:02d}",
                "settlement": f"{self.index} special opening quotation, 09:30 ET on the expiry"}


def roll_date(expiry: date) -> date:
    return expiry - timedelta(days=config.FUTURES_ROLL_DAYS)


def contract_for(index: str, year: int, month: int) -> FuturesContract:
    spec = config.FUTURES[index]
    code = f"{config.FUTURES_MONTHS[month]}{year % 100:02d}"
    exp = third_friday(year, month)
    return FuturesContract(index=index, symbol=f"{spec['symbol']}{code}", also=tuple(f"{a}{code}" for a in spec["also"]),
                           month=f"{year}-{month:02d}", expiry=exp, roll_date=roll_date(exp))


def front_contract(index: str, today: date) -> FuturesContract:
    """The nearest quarterly whose roll date is after `today` (New York date). On the roll date it is the next one."""
    y, m = today.year, today.month
    for _ in range(6):
        if m in config.FUTURES_MONTHS:
            c = contract_for(index, y, m)
            if today < c.roll_date:
                return c
        m += 1
        if m > 12:
            y, m = y + 1, 1
    raise ValueError("no quarterly contract found")       # unreachable: one of 6 months always qualifies


def next_contract(c: FuturesContract) -> FuturesContract:
    y, m = c.expiry.year, c.expiry.month + 3
    if m > 12:
        y, m = y + 1, m - 12
    return contract_for(c.index, y, m)


# ---------------------------------------------------------------- parity

@dataclass
class Parity:
    forward: float | None
    strikes: list[dict]            # {"strike","call_mid","put_mid","forward"} for the strikes used
    usable: int                    # strike pairs with usable call and put mids
    spread: float | None           # max - min of the per-strike forwards used
    reason: str | None             # why forward is None


def round_tick(x: float, tick: float | None = None) -> float:
    """Nearest tick, halves up (0.125 -> 0.25). Never Python's round-half-even."""
    t = tick or config.FUTURES_TICK
    return math.floor(x / t + 0.5) * t


def _mid_usable(bid, ask) -> bool:
    from gexlab.sources.alpaca import mid_usable
    return mid_usable(bid, ask)


def parity_forward(pairs: dict[float, dict], spot: float, T: float, r: float | None = None,
                   n: int | None = None, min_n: int | None = None) -> Parity:
    """pairs: {strike: {"call": (bid, ask), "put": (bid, ask)}}. Median parity forward, two passes (see module doc)."""
    r = config.FUTURES_RATE if r is None else r
    n = n or config.FUTURES_PARITY_STRIKES
    min_n = min_n or config.FUTURES_PARITY_MIN
    if not T or T <= 0:
        return Parity(None, [], 0, None, "the quarterly options have expired")
    rows = []
    for k, q in pairs.items():
        c, p = q.get("call"), q.get("put")
        if c and p and _mid_usable(*c) and _mid_usable(*p):
            rows.append((float(k), 0.5 * (c[0] + c[1]), 0.5 * (p[0] + p[1])))
    if len(rows) < min_n:
        return Parity(None, [], len(rows), None,
                      f"only {len(rows)} strike{'s' if len(rows) != 1 else ''} with usable call and put quotes "
                      f"(needs {min_n})")
    g = math.exp(r * T)
    fwd = lambda row: row[0] + g * (row[1] - row[2])   # noqa: E731
    first = statistics.median(fwd(x) for x in sorted(rows, key=lambda x: (abs(x[0] - spot), x[0]))[:n])
    used = sorted(sorted(rows, key=lambda x: (abs(x[0] - first), x[0]))[:n])
    fs = [fwd(x) for x in used]
    return Parity(statistics.median(fs),
                  [{"strike": k, "call_mid": round(c, 4), "put_mid": round(p, 4), "forward": round(f, 4)}
                   for (k, c, p), f in zip(used, fs)],
                  len(rows), max(fs) - min(fs), None)


# ---------------------------------------------------------------- the Cboe payload

def parse_index_time(s: str | None) -> datetime | None:
    """Cboe's `last_trade_time` ("2026-10-21T11:44:31") is New York wall time with no zone (checked 2026-10-21:
    payload timestamp 15:59:33Z = 11:59:33 ET, last_trade_time 11:44:31, the 15 minute delay)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip()[:19]).replace(tzinfo=NY).astimezone(UTC)
    except ValueError:
        return None


@dataclass
class IndexPayload:
    index: str
    price: float
    payload_ts: datetime | None    # when Cboe built the file (UTC)
    index_time: datetime | None    # the index value's market-data time (about 15 min before payload_ts)
    pairs_by_expiry: dict[date, dict[float, dict]]   # option_root contracts only, quarterly months only


_PARSED: dict[str, tuple[tuple, IndexPayload]] = {}
_PARSED_LOCK = threading.Lock()


def parse_payload(index: str, body: bytes) -> IndexPayload:
    """Index price and times, plus bid/ask pairs of the AM settled monthly root in quarterly months. Raises
    ValueError on an unreadable payload. The map's own parse (cboe.parse_chain) is not touched."""
    from gexlab.sources.cboe import parse_cboe_timestamp
    try:
        j = json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError("Cboe payload is not JSON") from e
    data = j.get("data") if isinstance(j, dict) else None
    if not isinstance(data, dict) or not data.get("current_price"):
        raise ValueError("Cboe payload has no current_price")
    root = config.FUTURES[index]["option_root"]
    pairs: dict[date, dict[float, dict]] = {}
    for o in data.get("options") or []:
        try:
            occ = parse_occ(o["option"])
        except (KeyError, ValueError, TypeError):
            continue
        if occ.root != root or occ.expiry.month not in config.FUTURES_MONTHS:
            continue
        bid, ask = o.get("bid"), o.get("ask")
        if bid is None or ask is None:
            continue
        pairs.setdefault(occ.expiry, {}).setdefault(occ.strike, {})[occ.type] = (float(bid), float(ask))
    try:
        pts = parse_cboe_timestamp(j["timestamp"]) if j.get("timestamp") else None
    except (TypeError, ValueError):
        pts = None
    return IndexPayload(index, float(data["current_price"]), pts, parse_index_time(data.get("last_trade_time")), pairs)


def cached_payload(index: str, body: bytes) -> IndexPayload:
    """parse_payload, reused while the payload is the same file (the SPX file is about 13 MB)."""
    from gexlab.sources.cboe import payload_ts
    key = (len(body), payload_ts(body))
    with _PARSED_LOCK:
        hit = _PARSED.get(index)
        if hit and hit[0] == key and key[1] is not None:
            return hit[1]
    p = parse_payload(index, body)
    with _PARSED_LOCK:
        _PARSED[index] = (key, p)
    return p


def option_expiry(c: FuturesContract, p: IndexPayload) -> date | None:
    """The AM monthly expiry that settles this contract: the third Friday, or the session before it when the Friday
    is an exchange holiday (Jun 2027: Juneteenth, the SPX AM options list Thu Jun 17)."""
    for d in (c.expiry, c.expiry - timedelta(days=1)):
        if d in p.pairs_by_expiry:
            return d
    return None


def parity_block(c: FuturesContract, p: IndexPayload, now: datetime, cal: TradingCalendar | None) -> dict:
    exp = option_expiry(c, p)
    base = {"available": False, "reason": None, "forward": None, "index_spot": p.price, "basis": None,
            "basis_raw": None, "basis_window": None,
            "rate": config.FUTURES_RATE, "rate_source": config.FUTURES_RATE_SOURCE, "t_years": None,
            "strikes": [], "usable_pairs": 0, "spread": None, "option_root": config.FUTURES[c.index]["option_root"],
            "option_expiry": exp.isoformat() if exp else None, "settlement": "AM",
            "as_of": iso_z(p.payload_ts), "index_time": iso_z(p.index_time)}
    if exp is None:
        base["reason"] = (f"Cboe's payload lists no {base['option_root']} AM options for {c.expiry:%b %d}, "
                          "the expiry that settles this contract")
        return base
    t_ref = valuation_time(cal, now, p.payload_ts)
    T = time_to_expiry(t_ref, exp, "AM", cal)
    par = parity_forward(p.pairs_by_expiry[exp], p.price, T)
    base.update(t_years=round(T, 6), strikes=par.strikes, usable_pairs=par.usable,
                spread=round(par.spread, 4) if par.spread is not None else None)
    if par.forward is None:
        base["reason"] = par.reason
        return base
    raw = par.forward - p.price
    base.update(available=True, forward=round(par.forward, 4), basis_raw=round(raw, 4))
    base.update(smoothed_basis(c, p.payload_ts, raw))
    return base


# ---------------------------------------------------------------- basis over recent payloads

_BASIS_HIST: dict[str, dict[datetime, float]] = {}
_BASIS_LOCK = threading.Lock()


def smoothed_basis(c: FuturesContract, payload_ts: datetime | None, raw: float) -> dict:
    """Median of this contract's parity basis over the payloads of the last FUTURES_BASIS_WINDOW_S (payload time).
    One payload per timestamp; a payload with no time is used alone. Returns basis and its window."""
    if payload_ts is None:
        return {"basis": round(raw, 4), "basis_window": {"payloads": 1, "seconds": 0}}
    with _BASIS_LOCK:
        h = _BASIS_HIST.setdefault(c.symbol, {})
        h[payload_ts] = raw
        lo = payload_ts - timedelta(seconds=config.FUTURES_BASIS_WINDOW_S)
        for t in [t for t in h if t < lo - timedelta(seconds=config.FUTURES_BASIS_WINDOW_S)]:
            del h[t]                                   # forget payloads well outside the window
        vals = [(t, v) for t, v in h.items() if lo <= t <= payload_ts]
    span = (payload_ts - min(t for t, _ in vals)).total_seconds()
    return {"basis": round(statistics.median(v for _, v in vals), 4),
            "basis_window": {"payloads": len(vals), "seconds": round(span)}}


# ---------------------------------------------------------------- ETF ratio, override

def check_override(override_basis: float, parity_basis: float | None, tolerance: float) -> dict:
    """Compare the basis from the typed price with the parity estimate. warning when they differ by more than tolerance."""
    if parity_basis is None:
        return {"diff": None, "tolerance": tolerance, "warning": False}
    diff = override_basis - parity_basis
    return {"diff": round(diff, 4), "tolerance": tolerance, "warning": abs(diff) > tolerance}


def to_futures(level: float, basis: float, ratio: float = 1.0, tick: float | None = None) -> float:
    """An index (ratio 1) or ETF level as a futures equivalent, on the tick. The UI mirrors this in web/src/lib/futures.ts."""
    return round_tick(level * ratio + basis, tick)


def etf_ratio(etf: str, p: IndexPayload, trade_at) -> dict:
    """ratio = index / ETF at the index value's market-data time. trade_at(symbol, at, lookback_s) -> Spot | None.
    Raises SourceError when no ETF price of that instant exists."""
    if p.index_time is None:
        raise SourceError(f"Cboe's {p.index} payload carries no index time, so no same-instant {etf} price")
    tr = trade_at(etf, p.index_time, config.FUTURES_RATIO_MAX_LAG_S)
    if tr is None or not tr.price:
        raise SourceError(f"no {etf} trade within {config.FUTURES_RATIO_MAX_LAG_S} s before {iso_z(p.index_time)}")
    return {"value": p.price / tr.price, "index_price": p.price, "index_time": iso_z(p.index_time),
            "etf": etf, "etf_price": tr.price, "etf_time": iso_z(tr.ts),
            "lag_s": round((p.index_time - tr.ts).total_seconds(), 1) if tr.ts else None, "source": tr.source,
            "note": (f"{p.index} from Cboe (its {p.index_time.astimezone(NY):%H:%M:%S} ET value) over the last {etf} "
                     f"trade at or before that second (Alpaca). Same instant, so the 15 minute delay drops out.")}


def _etf_for(index: str) -> str:
    return next(e for e, i in config.ETF_INDEX.items() if i == index)


# ---------------------------------------------------------------- the endpoint

_RATIO_CACHE: dict[tuple, dict] = {}


def build(ticker: str, *, now: datetime, cal: TradingCalendar | None, market_open: bool,
          index_body: bytes, trade_at=None, latest_trade=None, override_price: float | None = None,
          override_basis: float | None = None, demo: bool = False) -> dict:
    """The /api/futures payload. index_body: the Cboe payload of the index (shared cache with the map).
    trade_at / latest_trade: Alpaca REST callables, None without keys. Never raises for vendor trouble."""
    ticker = ticker.upper()
    index = config.ETF_INDEX.get(ticker, ticker)
    etf = ticker if ticker in config.ETF_INDEX else None
    spec = config.FUTURES[index]
    c = front_contract(index, ny_today(now))
    out = {"ticker": ticker, "index": index, "etf": etf, "available": False, "reason": None,
           "contract": {**c.to_json(), "next": next_contract(c).to_json()}, "parity": None, "ratio": None,
           "override": None, "check": None, "basis": None, "convert": None, "tick": config.FUTURES_TICK,
           "tolerance": spec["tolerance"], "label": ESTIMATE_LABEL, "warnings": [], "as_of": iso_z(now),
           "demo": demo}
    try:
        p = cached_payload(index, index_body)
    except ValueError as e:
        out["reason"] = f"{index} payload unreadable: {e}"
        return out
    par = parity_block(c, p, now, cal)
    out["parity"] = par
    if par["spread"] is not None and par["spread"] > config.FUTURES_PARITY_WARN_FRAC * p.price:
        out["warnings"].append(f"The per-strike forwards spread {par['spread']:.2f} points; quotes may be wide or moving.")

    ratio = 1.0
    if etf:
        if trade_at is None:
            out["reason"] = (f"{etf} needs Alpaca keys: the {etf} price at the index's time sets the "
                             f"{etf} to {index} ratio")
            return out
        key = (etf, iso_z(p.index_time), p.price)
        try:
            rb = _RATIO_CACHE.get(key) or etf_ratio(etf, p, trade_at)
        except SourceError as e:
            out["reason"] = f"{etf} to {index} ratio unavailable: {e}"
            return out
        if len(_RATIO_CACHE) > 16:
            _RATIO_CACHE.clear()
        _RATIO_CACHE[key] = rb
        out["ratio"] = rb
        ratio = rb["value"]

    if override_price is not None:
        out["override"] = _override(index, etf or _etf_for(index), p, ratio if etf else None, override_price,
                                    market_open, trade_at, latest_trade, demo)
        if out["override"].get("basis") is not None:
            override_basis = out["override"]["basis"]
    if override_basis is not None:
        out["basis"] = {"value": round(override_basis, 4), "source": "override", "label": "your price"}
        out["check"] = check_override(override_basis, par["basis"], spec["tolerance"])
        if out["check"]["warning"]:
            out["warnings"].append(f"Your price implies a basis {out['check']['diff']:+.2f} points from the parity "
                                   f"estimate (tolerance {spec['tolerance']:g}).")
    elif par["available"]:
        out["basis"] = {"value": par["basis"], "source": "parity", "label": "parity estimate"}
    else:
        out["reason"] = f"Parity basis unavailable: {par['reason']}. Type your {spec['symbol']} price to use it instead."
        return out
    out["available"] = True
    out["convert"] = {"ratio": ratio, "basis": out["basis"]["value"], "tick": config.FUTURES_TICK}
    return out


def _override(index, etf, p: IndexPayload, ratio_known, price, market_open, trade_at, latest_trade, demo) -> dict:
    """basis = the typed price minus the latest index value; live when Alpaca keys and an open market allow it."""
    o = {"price": price, "index_value": p.price, "index_source": "cboe_delayed", "index_time": iso_z(p.index_time),
         "delayed": True, "basis": None, "at": None,
         "label": f"Cboe {index}, about 15 min delayed: your basis also holds any move since then"}
    if demo:
        o.update(index_source="demo", delayed=False, label="Synthetic fixture")
    elif not market_open:
        o["label"] = f"Cboe {index} at the close: your basis also holds any move since the close"
    elif trade_at is not None and latest_trade is not None:
        try:
            ratio = ratio_known if ratio_known is not None else etf_ratio(etf, p, trade_at)["value"]
            last = latest_trade(etf)
            o.update(index_value=round(last.price * ratio, 4), index_source="live_etf_ratio",
                     index_time=iso_z(last.ts), delayed=False,
                     label=f"Live {index} estimate: last {etf} trade times the {index}/{etf} ratio")
        except SourceError as e:
            o["label"] += f" (live estimate unavailable: {e})"
    o["basis"] = round(price - o["index_value"], 4)
    o["at"] = o["index_time"]
    return o


# ---------------------------------------------------------------- demo (synthetic, labeled)

DEMO_STRIKE_STEP = {"SPX": 5.0, "NDX": 25.0}   # the listed strike spacing near the money
DEMO_IV = 0.15                                 # flat synthetic IV; any value gives the same parity forward


def demo_payload(index: str, spot: float, now: datetime) -> bytes:
    """A synthetic Cboe-shaped payload for GEXLAB_DEMO=1: quarterly AM options priced by Black-Scholes with
    r = FUTURES_RATE and no dividend, so the parity forward is spot * e^(rT). Labeled synthetic in the UI."""
    from gexlab.engine import bsm
    c = front_contract(index, ny_today(now))
    T = time_to_expiry(now, c.expiry, "AM")
    step = DEMO_STRIKE_STEP[index]
    root = config.FUTURES[index]["option_root"]
    k0 = round(spot / step) * step
    opts = []
    for i in range(-20, 21):
        k = k0 + i * step
        for cp, fn in (("C", bsm.call_price), ("P", bsm.put_price)):
            mid = fn(spot, k, T, config.FUTURES_RATE, DEMO_IV)
            half = max(0.05, mid * 0.002)
            opts.append({"option": f"{root}{c.expiry:%y%m%d}{cp}{int(round(k * 1000)):08d}",
                         "bid": round(mid - half, 2), "ask": round(mid + half, 2)})
    return json.dumps({"timestamp": now.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                       "data": {"current_price": spot, "last_trade_time": now.astimezone(NY).strftime("%Y-%m-%dT%H:%M:%S"),
                                "options": opts}}).encode()
