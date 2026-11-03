"""Alpaca REST source: contracts + OI, option snapshots, stock spot, calendar, assets.

REST ONLY. This module never opens a websocket (Alpaca allows one per account and
another app may own it) and never calls an order endpoint. Every request
goes through the shared rate limiter. Keys are read, never printed or logged.

IV policy (model v1.0):
  OPRA feed: quotes only, no greeks/IV (verified). Solve IV from the NBBO mid
    with European BSM, r = q = 0, T measured at the quote time. When the mid is
    unusable (no bid, crossed, or wider than WIDE_SPREAD_FRAC) or the solve
    fails, fall back per contract to the indicative feed's impliedVolatility.
  Indicative feed only (no OPRA entitlement): use its impliedVolatility.
  The contract's iv_source records which path was used.
  Alignment: an OPRA quote is solved only when its timestamp is known and within
    QUOTE_SPOT_MAX_SKEW_S of the spot trade; otherwise it takes the fallback path.
  IV age: every IV carries iv_ts, the time of the quote it came from (solved: the
    OPRA quote; indicative: the indicative snapshot's latestQuote time). The age check
    (iv_age_problem) runs BEFORE an IV is used or shared. Strict mode (the recorder:
    frozen and snapshot captures) drops a stale, future or unknown-age IV, so it never
    enters a research capture. Live mode (the map) drops a stale IV while the market is
    open and keeps an unknown-age IV with a warning that counts it.
  Sharing: the OTM member's IV is shared with its strike pair. The receiving
    contract keeps its own IV (iv_own), the donor symbol and IV time, and
    iv_shared = True, so a copied IV never passes as its own quote's solve.
  Null OI: Alpaca never returns open_interest 0; a series with no OI on the OI date
    comes back null. decode_null_oi() reads null as zero (oi_basis says why) only
    while the OI feed is healthy; otherwise the nulls stay unknown and count against
    coverage.
  OI dates are kept per contract; a chain with mixed dates says so in a warning.
  response_hash covers the spot response too (spot, contracts, snapshots, in fetch order).

Valuation clock (calendar.valuation_time): in regular hours the spot is the latest trade and
  T runs from the option quote time (never later than now). With the market closed the spot
  is the last session's official close (daily bar close, else the last 1-minute bar before the
  close; never an extended-hours print), T runs from that close, and an OPRA closing quote
  (stamped up to CLOSED_QUOTE_AFTER_CLOSE_S after the close) is solved at the close. Alpaca's
  indicative IV is computed on the vendor's own wall clock (verified 2026-10-17: on a Saturday
  it matches a solve with T from Saturday, not from Friday's close), so outside market hours
  it overstates IV and is labeled.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time as _time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values

from gexlab import config
from gexlab.engine.calendar import (NY, UTC, TradingCalendar, iso_z, ny_today, time_to_expiry,
                                    valuation_time)
from gexlab.engine.gex import iv_ok
from gexlab.engine.iv import implied_volatility
from gexlab.models import Chain, Contract, Spot, parse_occ
from gexlab.sources.base import Hasher, RateLimiter, SourceError, get_with_backoff, limiter


# ---------------------------------------------------------------- credentials

@dataclass
class AlpacaCreds:
    key: str
    secret: str
    mode: str                 # paper | live

    @property
    def trading_base(self) -> str:
        return config.ALPACA_TRADING_BASES.get(self.mode, config.ALPACA_TRADING_BASES["paper"])

    def __repr__(self) -> str:          # never leak keys through a repr or a log line
        return f"AlpacaCreds(mode={self.mode!r}, key=<hidden>)"


def load_credentials() -> AlpacaCreds | None:
    """APCA_API_KEY_ID / APCA_API_SECRET_KEY env vars first, else the file in ALPACA_ENV_FILE, else the
    project's own .env (next to README.md)."""
    k, s = os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY")
    mode = (os.getenv("ALPACA_MODE") or "paper").strip().lower()
    if k and s:
        return AlpacaCreds(k.strip(), s.strip(), mode)
    path = os.getenv("ALPACA_ENV_FILE") or str(config.ROOT / ".env")
    if not path or not Path(path).is_file():
        return None
    v = dotenv_values(path)          # read into a dict; never copied into os.environ
    k = v.get("ALPACA_TRADING_KEY") or v.get("APCA_API_KEY_ID")
    s = v.get("ALPACA_TRADING_SECRET") or v.get("APCA_API_SECRET_KEY")
    if not (k and s):
        return None
    mode = (v.get("ALPACA_MODE") or os.getenv("ALPACA_MODE") or "paper").strip().lower()
    return AlpacaCreds(k.strip(), s.strip(), mode)


# ---------------------------------------------------------------- shared state

FEED_STATE: dict = {"options": None, "options_checked": 0.0, "stocks": None}
_CAL_CACHE: dict = {"cal": None, "at": 0.0}
_ASSET_CACHE: dict = {"rows": None, "at": 0.0}
_CONTRACT_CACHE: dict = {}          # (ticker, gte, lte) -> (fetched_at, rows, bodies)
_STATE_LOCK = threading.Lock()


def parse_ts(s: str | None) -> datetime | None:
    """Parse Alpaca RFC 3339 timestamps, including nanosecond fractions."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac, tz = rest, ""
        for sep in ("+", "-"):
            if sep in rest:
                frac, tz = rest.split(sep, 1)
                tz = sep + tz
                break
        s = f"{head}.{frac[:6]}{tz}"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def mid_usable(bid: float | None, ask: float | None) -> bool:
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return False
    mid = 0.5 * (bid + ask)
    return (ask - bid) / mid <= config.WIDE_SPREAD_FRAC


def pair_iv_from_otm(contracts: list[Contract], spot: float) -> int:
    """Use the OTM member's IV for both the call and the put at each (root, expiry, strike).

    Under European BSM with r = q = 0 a call and a put at the same strike share
    one IV, and the OTM quote is the cleaner one (an ITM mid is mostly intrinsic
    and fails to solve when spot and quote times differ). The ITM member's own IV
    is the backup when the OTM side has none. Returns how many IVs were replaced.
    """
    pairs: dict[tuple, dict[str, Contract]] = {}
    for c in contracts:
        # By root too: two series can share an expiry and strike (SPX AM and SPXW PM on the third Friday,
        # NDX and NDXP, or a stock's adjusted series after a corporate action). Reported in discussion 19.
        pairs.setdefault((c.root, c.expiry, c.strike), {})[c.type] = c
    for c in contracts:                     # record each contract's own IV before any sharing
        if not c.iv_shared:
            c.iv_own, c.iv_own_source = c.iv, c.iv_source
    changed = 0
    for (_, _, k), pc in pairs.items():
        otm = pc.get("call") if k >= spot else pc.get("put")
        itm = pc.get("put") if k >= spot else pc.get("call")
        src = otm if otm is not None and iv_ok(otm.iv) else itm if itm is not None and iv_ok(itm.iv) else None
        if src is None:
            continue
        for c in (otm, itm):
            if c is not None and c is not src and c.iv != src.iv:
                c.iv, c.iv_source, c.invalid_reason = src.iv, src.iv_source, None
                c.iv_shared, c.iv_donor, c.iv_donor_ts, c.iv_ts = True, src.symbol, src.iv_ts, src.iv_ts
                changed += 1
    return changed


def iv_age_problem(iv_ts: datetime | None, ref: datetime, after_s: float | None = None,
                   received: datetime | None = None) -> str | None:
    """None when the IV's quote time is known and fresh against the valuation time `ref`.

    One quote clock (day-1 audit and verification): a quote is "future" when it is stamped after it
    was RECEIVED (`received`: the fetch's own clock when it arrived, or the capture's completion in the
    gate), plus the clock skew. Without `received`, after `ref` plus `after_s` (default CLOCK_SKEW_S).
    A chain valued at the session close also passes after_s = CLOSED_QUOTE_AFTER_CLOSE_S: a closing
    quote may be stamped up to 16 min after the close, never later. Stale = older than the valuation
    time by more than INDICATIVE_IV_MAX_AGE_S."""
    if iv_ts is None:
        return "iv_time_unknown"
    if received is not None and (iv_ts - received).total_seconds() > config.CLOCK_SKEW_S:
        return "iv_time_future"
    if (iv_ts - ref).total_seconds() > (config.CLOCK_SKEW_S if after_s is None else after_s) and \
            (received is None or after_s is not None):
        return "iv_time_future"
    if (ref - iv_ts).total_seconds() > config.INDICATIVE_IV_MAX_AGE_S:
        return "iv_stale"
    return None


def decode_null_oi(contracts: list[Contract]) -> tuple[bool, dict]:
    """Read Alpaca's null open_interest as zero when the OI feed is healthy.

    Healthy: some contracts carry OI with a date, and of the series that traded at some point
    (close_price_date present) at most OI_NULL_AMBIGUOUS_MAX_FRAC lack a record. A series that
    never traded cannot have open interest, so its null is a certain zero. Returns
    (healthy, counts by basis). Unhealthy leaves every null as unknown (oi None).
    """
    reported = [c for c in contracts if c.oi is not None]
    for c in reported:
        c.oi_basis = "reported"
    nulls = [c for c in contracts if c.oi is None]
    counts = Counter({"reported": len(reported)})
    if not nulls:
        return True, dict(counts)
    for c in nulls:
        c.oi_basis = "zero_never_traded" if not c.last_trade_date else "zero_vendor_null"
    ambiguous = sum(1 for c in nulls if c.oi_basis == "zero_vendor_null")
    has_date = any(c.oi_date for c in reported)
    denom = ambiguous + len(reported)
    share = ambiguous / denom if denom else 1.0
    healthy = bool(reported) and has_date and share <= config.OI_NULL_AMBIGUOUS_MAX_FRAC
    for c in nulls:
        if healthy:
            c.oi = 0
            counts[c.oi_basis] += 1
        else:
            c.oi_basis = None
            counts["unknown"] += 1
    return healthy, dict(counts)


class AlpacaSource:
    def __init__(self, creds: AlpacaCreds | None = None, client: httpx.Client | None = None,
                 rate: RateLimiter | None = None, priority: str = "low", who: str = "api"):
        self.creds = creds or load_credentials()
        if self.creds is None:
            raise SourceError("Alpaca keys not configured: put APCA_API_KEY_ID and APCA_API_SECRET_KEY in .env "
                              "(SPX and NDX work without keys)")
        self.client = client or httpx.Client(timeout=config.HTTP_TIMEOUT_S)
        self.rate = rate if rate is not None else limiter()
        self.priority, self.who = priority, who
        self.requests = 0

    @property
    def _headers(self) -> dict:
        return {"APCA-API-KEY-ID": self.creds.key, "APCA-API-SECRET-KEY": self.creds.secret}

    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        self.requests += 1
        r = get_with_backoff(self.client, url, params=params, headers=self._headers,
                             rate=self.rate, priority=self.priority, who=self.who)
        if r.status_code == 401:
            raise SourceError("Alpaca rejected the keys (HTTP 401)", status=401)
        return r

    # ------------------------------------------------------------ spot
    def latest_trade(self, symbol: str, hasher: Hasher | None = None) -> Spot:
        feeds = [FEED_STATE["stocks"]] if FEED_STATE["stocks"] else ["sip", "iex"]
        last_status = None
        for feed in feeds:
            r = self._get(f"{config.ALPACA_DATA_BASE}/v2/stocks/{symbol}/trades/latest",
                          {"feed": feed})
            if r.status_code == 200:
                t = r.json().get("trade") or {}
                if t.get("p"):
                    FEED_STATE["stocks"] = feed
                    if hasher:
                        hasher.add(r.content)         # the spot is a valuation input: bind it in the hash
                    return Spot(float(t["p"]), parse_ts(t.get("t")), f"alpaca_{feed}")
            last_status = r.status_code
            if FEED_STATE["stocks"]:          # cached feed broke: retry detection once
                FEED_STATE["stocks"] = None
                return self.latest_trade(symbol, hasher)
        raise SourceError(f"no latest trade for {symbol}", status=last_status)

    def trade_at(self, symbol: str, at: datetime, lookback_s: float) -> Spot | None:
        """The last trade at or before `at`, no older than lookback_s (REST, one request). None when there is none.

        The Futures view pairs it with the Cboe index value of the same instant for the ETF to index ratio."""
        feeds = [FEED_STATE["stocks"]] if FEED_STATE["stocks"] else ["sip", "iex"]
        last_status = None
        for feed in feeds:
            r = self._get(f"{config.ALPACA_DATA_BASE}/v2/stocks/{symbol}/trades",
                          {"feed": feed, "start": iso_z(at - timedelta(seconds=lookback_s)), "end": iso_z(at),
                           "limit": 1, "sort": "desc"})
            last_status = r.status_code
            if r.status_code != 200:
                continue
            FEED_STATE["stocks"] = feed
            rows = [t for t in (r.json().get("trades") or []) if t.get("p") and parse_ts(t.get("t"))]
            return Spot(float(rows[0]["p"]), parse_ts(rows[0]["t"]), f"alpaca_{feed}") if rows else None
        raise SourceError(f"no trades for {symbol}", status=last_status)

    def session_close(self, symbol: str, close: datetime, hasher: Hasher | None = None) -> Spot:
        """The official regular-session close for the session ending at `close`, never an
        extended-hours print. Daily bar close first (it is the closing-auction price: verified
        2026-10-17, SPY 763.25 and TSLA 354.72 match the closing-cross prints while TSLA's
        after-hours latest trade was 352.46); else the last 1-minute bar ending at or before
        the close. The returned Spot is stamped with the close instant."""
        feeds = [FEED_STATE["stocks"]] if FEED_STATE["stocks"] else ["sip", "iex"]
        day = close.astimezone(NY).date()
        last_status = None
        for feed in feeds:
            r = self._get(f"{config.ALPACA_DATA_BASE}/v2/stocks/{symbol}/bars",
                          {"feed": feed, "timeframe": "1Day", "start": day.isoformat(),
                           "end": day.isoformat(), "limit": 5})
            last_status = r.status_code
            if r.status_code != 200:
                continue
            FEED_STATE["stocks"] = feed
            bars = [b for b in (r.json().get("bars") or []) if b.get("c") and parse_ts(b.get("t"))
                    and parse_ts(b["t"]).astimezone(NY).date() == day]
            if bars:
                if hasher:
                    hasher.add(r.content)          # the spot is a valuation input: bind it in the hash
                return Spot(float(bars[-1]["c"]), close, f"alpaca_{feed}", basis="regular_close")
            start = close - timedelta(minutes=config.CLOSE_BAR_LOOKBACK_MIN)
            r = self._get(f"{config.ALPACA_DATA_BASE}/v2/stocks/{symbol}/bars",
                          {"feed": feed, "timeframe": "1Min", "start": iso_z(start),
                           "end": iso_z(close - timedelta(seconds=1)), "limit": 100})
            last_status = r.status_code
            if r.status_code == 200:
                mins = [b for b in (r.json().get("bars") or []) if b.get("c") and parse_ts(b.get("t"))
                        and parse_ts(b["t"]) + timedelta(minutes=1) <= close]
                if mins:
                    if hasher:
                        hasher.add(r.content)
                    return Spot(float(mins[-1]["c"]), close, f"alpaca_{feed}", basis="regular_close")
            break
        raise SourceError(f"no regular-session close for {symbol}", status=last_status)

    # ------------------------------------------------------------ calendar
    def calendar(self, around: date | None = None) -> TradingCalendar:
        with _STATE_LOCK:
            cached = _CAL_CACHE["cal"]
            if cached and _time.time() - _CAL_CACHE["at"] < config.CALENDAR_CACHE_S:
                return cached
        around = around or date.today()
        start, end = around - timedelta(days=90), around + timedelta(days=30)
        r = self._get(f"{self.creds.trading_base}/v2/calendar",
                      {"start": start.isoformat(), "end": end.isoformat()})
        if r.status_code != 200:
            raise SourceError(f"calendar HTTP {r.status_code}", status=r.status_code)
        cal = TradingCalendar.from_alpaca(r.json())
        cal.covered = (start, end)
        with _STATE_LOCK:
            _CAL_CACHE.update(cal=cal, at=_time.time())
        return cal

    # ------------------------------------------------------------ assets (search)
    def assets(self) -> list[dict]:
        with _STATE_LOCK:
            if _ASSET_CACHE["rows"] is not None and _time.time() - _ASSET_CACHE["at"] < config.ASSET_CACHE_S:
                return _ASSET_CACHE["rows"]
        disk = config.CACHE_DIR / "assets_optionable.json"
        if disk.is_file() and _time.time() - disk.stat().st_mtime < config.ASSET_CACHE_S:
            rows = json.loads(disk.read_text(encoding="utf-8"))
        else:
            r = self._get(f"{self.creds.trading_base}/v2/assets",
                          {"status": "active", "attributes": "has_options"})
            if r.status_code != 200:
                raise SourceError(f"assets HTTP {r.status_code}", status=r.status_code)
            rows = [{"symbol": a["symbol"], "name": a.get("name") or ""}
                    for a in r.json() if a.get("tradable", True)]
            disk.parent.mkdir(parents=True, exist_ok=True)
            disk.write_text(json.dumps(rows), encoding="utf-8")
        with _STATE_LOCK:
            _ASSET_CACHE.update(rows=rows, at=_time.time())
        return rows

    # ------------------------------------------------------------ contracts + OI
    def contracts(self, underlying: str, gte: date, lte: date, lo: float, hi: float,
                  hasher: Hasher | None = None, use_cache: bool = True) -> list[dict]:
        key = (underlying, gte, lte)
        hit = _CONTRACT_CACHE.get(key)
        if use_cache and hit and _time.time() - hit[0] < config.CONTRACTS_CACHE_S \
                and hit[3][0] <= lo and hit[3][1] >= hi:
            if hasher:
                for b in hit[2]:
                    hasher.add(b)
            return hit[1]
        # Fetch a slightly wider band than asked so small spot moves reuse the cache.
        flo, fhi = lo * 0.98, hi * 1.02
        rows, bodies, token = [], [], None
        while True:
            params = {"underlying_symbols": underlying, "expiration_date_gte": gte.isoformat(),
                      "expiration_date_lte": lte.isoformat(), "strike_price_gte": f"{flo:.2f}",
                      "strike_price_lte": f"{fhi:.2f}", "limit": config.CONTRACTS_PAGE_LIMIT}
            if token:
                params["page_token"] = token
            r = self._get(f"{self.creds.trading_base}/v2/options/contracts", params)
            if r.status_code != 200:
                raise SourceError(f"contracts HTTP {r.status_code}", status=r.status_code)
            bodies.append(r.content)
            j = r.json()
            rows.extend(j.get("option_contracts") or [])
            token = j.get("next_page_token")
            if not token:
                break
        _CONTRACT_CACHE[key] = (_time.time(), rows, bodies, (flo, fhi))
        if hasher:
            for b in bodies:
                hasher.add(b)
        return rows

    # ------------------------------------------------------------ snapshots
    def snapshots(self, underlying: str, feed: str, gte: date, lte: date, lo: float, hi: float,
                  hasher: Hasher | None = None) -> tuple[int, dict]:
        """Returns (http_status_of_first_page, {symbol: snapshot}). Paginates fully."""
        out, token, first_status = {}, None, None
        while True:
            params = {"feed": feed, "limit": config.SNAPSHOTS_PAGE_LIMIT,
                      "expiration_date_gte": gte.isoformat(), "expiration_date_lte": lte.isoformat(),
                      "strike_price_gte": f"{lo:.2f}", "strike_price_lte": f"{hi:.2f}"}
            if token:
                params["page_token"] = token
            r = self._get(f"{config.ALPACA_DATA_BASE}/v1beta1/options/snapshots/{underlying}", params)
            if first_status is None:
                first_status = r.status_code
            if r.status_code != 200:
                if out:      # a later page failed: the chain is incomplete
                    raise SourceError(f"snapshots page HTTP {r.status_code}", status=r.status_code)
                return r.status_code, {}
            if hasher:
                hasher.add(r.content)
            j = r.json()
            got = datetime.now(timezone.utc)        # receipt time of this page: no quote in it can be later
            for snap in (j.get("snapshots") or {}).values():
                if isinstance(snap, dict):
                    snap["_received"] = got
            out.update(j.get("snapshots") or {})
            token = j.get("next_page_token")
            if not token:
                return first_status, out

    # ------------------------------------------------------------ chain
    def fetch_chain(self, ticker: str, as_of: datetime, cal: TradingCalendar | None,
                    strict: bool = False, oi_overlay=None) -> Chain:
        """strict=True (recorder): an IV whose quote time is unknown, stale or in the future is never used.

        oi_overlay(ticker, contracts, as_of, cal, hasher) -> dict replaces the open interest (model 1.1:
        cboe.apply_oi) before null OI is decoded; None keeps Alpaca OI. Quotes, spot and IV stay Alpaca's."""
        ticker = ticker.upper()
        hasher = Hasher()
        self.requests = 0
        warnings: list[str] = []
        in_session = bool(cal is None or cal.market_open(as_of))   # no calendar: the in-session clock
        market_open = bool(cal is not None and cal.market_open(as_of))
        if in_session:
            spot = self.latest_trade(ticker, hasher)          # regular hours: the latest trade
            t_cap = as_of                                     # an IV solve never looks past now
        else:
            close = valuation_time(cal, as_of)                # the last session close
            spot = self.session_close(ticker, close, hasher)  # its official close, never after hours
            t_cap = close
        today = ny_today(as_of)
        gte, lte = today, today + timedelta(days=config.WINDOW_DAYS)
        lo, hi = spot.price * (1 - config.FETCH_STRIKE_BAND), spot.price * (1 + config.FETCH_STRIKE_BAND)
        rows = self.contracts(ticker, gte, lte, lo, hi, hasher)
        rows = [r for r in rows if lo <= float(r["strike_price"]) <= hi]

        feed = self._options_feed()
        primary: dict = {}
        if feed in (None, "opra"):
            status, primary = self.snapshots(ticker, "opra", gte, lte, lo, hi, hasher)
            if status == 200 and (primary or not rows):
                feed = "opra"
            elif status in (401,):
                raise SourceError("Alpaca rejected the keys (HTTP 401)", status=401)
            else:
                feed, primary = "indicative", {}
            self._set_options_feed(feed)
        indicative: dict = {}
        if feed == "indicative":
            status, indicative = self.snapshots(ticker, "indicative", gte, lte, lo, hi, hasher)
            if status != 200:
                raise SourceError(f"indicative snapshots HTTP {status}", status=status)

        contracts = [self._to_contract(r) for r in rows]
        contracts = [c for c in contracts if c is not None]
        if oi_overlay is not None:
            oi_info = oi_overlay(ticker, contracts, as_of, cal, hasher)
        else:
            from gexlab.sources.cboe import alpaca_only
            oi_info = alpaca_only(contracts)
        if oi_info.get("fallback_reason"):
            warnings.append(f"{oi_info['label']}: Cboe open interest not used ({oi_info['fallback_reason']}).")
        elif oi_info.get("source") == "cboe" and oi_info.get("matched", 0) < oi_info.get("total", 0):
            warnings.append(f"{oi_info['total'] - oi_info['matched']} contracts are not in the Cboe payload and "
                            "keep Alpaca open interest.")
        oi_ok, oi_counts = decode_null_oi(contracts)
        if not oi_ok:
            warnings.append(f"{oi_counts.get('unknown', 0)} contracts have no OI record and the OI feed looks "
                            "incomplete, so they count as unknown exposure.")
        elif oi_counts.get("zero_never_traded") or oi_counts.get("zero_vendor_null"):
            n0 = oi_counts.get("zero_never_traded", 0) + oi_counts.get("zero_vendor_null", 0)
            warnings.append(f"{n0} contracts have no OI record (Alpaca omits zero open interest; "
                            f"{oi_counts.get('zero_never_traded', 0)} never traded), so they count as zero OI.")
        age_gate = "strict" if strict else ("open" if market_open else "closed")
        after = config.QUOTE_SPOT_MAX_SKEW_S if in_session else config.CLOSED_QUOTE_AFTER_CLOSE_S
        if feed == "opra":
            need = [c for c in contracts
                    if not self._apply_opra(c, primary.get(c.symbol), spot.price, t_cap, cal, spot.ts,
                                            max_after_s=after)]
            if need:
                st, indicative = self.snapshots(ticker, "indicative", gte, lte, lo, hi, hasher)
                if st != 200:
                    warnings.append("Indicative fallback unavailable; contracts with unusable mids are invalid.")
                for c in need:
                    self._apply_indicative(c, indicative.get(c.symbol), as_of=as_of, age_gate=age_gate)
        else:
            n_same_day = 0
            for c in contracts:
                self._apply_indicative(c, indicative.get(c.symbol), quote_too=True, as_of=as_of,
                                       age_gate=age_gate)
                # Alpaca computes no IV or greeks for a contract expiring today (its market data FAQ), so on
                # this feed every same-day contract arrived invalid (issue 29). Solve it from the indicative
                # mid like the OPRA path: same quote-to-spot alignment, same spread and IV checks.
                if c.invalid_reason == "no_iv" and c.expiry == today and \
                        self._apply_opra(c, indicative.get(c.symbol), spot.price, t_cap, cal, spot.ts,
                                         max_after_s=after):
                    n_same_day += 1
            if n_same_day:
                warnings.append(f"{n_same_day} contracts expiring today use an IV solved from the indicative quote "
                                "(Alpaca gives no IV for same-day expiries). Walls and the flip hold up against "
                                "OPRA, but bar sizes and net GEX can read noticeably smaller or larger.")

        pair_iv_from_otm(contracts, spot.price)       # after the age check: a dropped IV is never shared
        n_unknown = sum(1 for c in contracts if iv_ok(c.iv) and c.iv_ts is None)
        n_stale = sum(1 for c in contracts if iv_ok(c.iv) and c.iv_ts is not None
                      and iv_age_problem(c.iv_ts, as_of) == "iv_stale")
        if n_unknown:
            warnings.append(f"{n_unknown} contracts use an IV of unknown age (no quote time); "
                            "frozen captures exclude them.")
        if n_stale:
            warnings.append(f"{n_stale} contracts use an IV more than {config.INDICATIVE_IV_MAX_AGE_S // 60} min "
                            "old (market closed); frozen captures exclude them.")
        oi_dates = Counter(c.oi_date for c in contracts if c.oi_date)
        oi_date = max(oi_dates) if oi_dates else None
        if len(oi_dates) > 1:
            parts = ", ".join(f"{n} from {d}" for d, n in sorted(oi_dates.items(), reverse=True))
            warnings.append(f"OI dates are mixed: {parts}.")
        if feed == "opra":
            n_fb = sum(1 for c in contracts if c.iv_source == "indicative")
            if n_fb:
                warnings.append(f"{n_fb} contracts use indicative IV because their mid was unusable.")
        if not in_session:
            n_ind = sum(1 for c in contracts if iv_ok(c.iv) and c.iv_source == "indicative")
            if n_ind:
                warnings.append(f"{n_ind} contracts use the vendor's indicative IV, computed on its own clock; "
                                "outside market hours it runs high.")
        if in_session:
            qts = [c.iv_ts for c in contracts if iv_ok(c.iv) and c.iv_ts is not None]
            valuation = valuation_time(cal, as_of, max(qts) if qts else None)
        else:
            valuation = t_cap
        return Chain(ticker=ticker, as_of=as_of, spot=spot, feed=feed, contracts=contracts,
                     oi_date=oi_date, complete=True, raw_hash=hasher.hexdigest(),
                     warnings=warnings, requests=self.requests, valuation_ts=valuation,
                     oi_source=oi_info.get("source"), oi_info=oi_info)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _options_feed() -> str | None:
        with _STATE_LOCK:
            if FEED_STATE["options"] and _time.time() - FEED_STATE["options_checked"] < config.FEED_REDETECT_S:
                return FEED_STATE["options"]
            return None

    @staticmethod
    def _set_options_feed(feed: str) -> None:
        with _STATE_LOCK:
            FEED_STATE.update(options=feed, options_checked=_time.time())

    @staticmethod
    def _to_contract(r: dict) -> Contract | None:
        try:
            occ = parse_occ(r["symbol"])
        except (KeyError, ValueError):
            return None
        oi = r.get("open_interest")
        try:
            oi = int(float(oi)) if oi not in (None, "") else None
        except ValueError:
            oi = None
        try:
            mult = int(float(r.get("multiplier") or r.get("size") or 100))
        except ValueError:
            mult = 100
        return Contract(symbol=r["symbol"], root=r.get("root_symbol") or occ.root,
                        expiry=date.fromisoformat(r.get("expiration_date") or occ.expiry.isoformat()),
                        settlement="PM", strike=float(r.get("strike_price") or occ.strike),
                        type=(r.get("type") or occ.type), multiplier=mult, oi=oi,
                        oi_date=r.get("open_interest_date") or None,
                        last_trade_date=r.get("close_price_date") or None)

    @staticmethod
    def _apply_opra(c: Contract, snap: dict | None, spot: float, as_of: datetime,
                    cal: TradingCalendar | None, spot_ts: datetime | None = None,
                    max_after_s: float | None = None) -> bool:
        """Solve IV from the OPRA mid. Returns False when the contract needs the fallback.

        The quote must carry a timestamp within QUOTE_SPOT_MAX_SKEW_S of the spot trade:
        an old (or future) quote solved against a newer spot would bake the mismatch into IV.
        max_after_s widens the window after the spot time only (market closed: the spot is the
        session close and a closing quote may be stamped later). as_of caps the solve clock.
        """
        q = (snap or {}).get("latestQuote") or {}
        c.bid, c.ask = q.get("bp"), q.get("ap")
        c.quote_ts = parse_ts(q.get("t"))
        if not snap:
            c.invalid_reason = "no_quote"
            return False
        if not mid_usable(c.bid, c.ask):
            c.invalid_reason = "mid_unusable"
            return False
        if c.quote_ts is None or spot_ts is None:
            c.invalid_reason = "quote_time_unknown"
            return False
        got = snap.get("_received")
        if got is not None and (c.quote_ts - got).total_seconds() > config.CLOCK_SKEW_S:
            c.invalid_reason = "quote_time_future"      # stamped after the page carrying it arrived
            return False
        lag = (c.quote_ts - spot_ts).total_seconds()
        after = config.QUOTE_SPOT_MAX_SKEW_S if max_after_s is None else max_after_s
        if lag < -config.QUOTE_SPOT_MAX_SKEW_S or lag > after:
            c.invalid_reason = "quote_spot_misaligned"
            return False
        t_ref = min(c.quote_ts, as_of)
        T = time_to_expiry(t_ref, c.expiry, c.settlement, cal)
        iv = implied_volatility(0.5 * (c.bid + c.ask), spot, c.strike, T, 0.0, c.type)
        if not iv_ok(iv):
            c.invalid_reason = "iv_solve_failed"
            return False
        c.iv, c.iv_source, c.iv_ts, c.invalid_reason = iv, "solved_mid", c.quote_ts, None
        return True

    @staticmethod
    def _apply_indicative(c: Contract, snap: dict | None, quote_too: bool = False,
                          as_of: datetime | None = None, age_gate: str = "strict",
                          received: datetime | None = None) -> None:
        """Use the indicative IV only after its age check.

        age_gate: strict (recorder) drops unknown, stale and future IVs; open (live map, market
        open) drops stale and future and keeps unknown with a label; closed (live map, market
        closed) keeps them all, labeled by the chain warnings.
        """
        q = (snap or {}).get("latestQuote") or {}
        if quote_too:
            c.bid, c.ask, c.quote_ts = q.get("bp"), q.get("ap"), parse_ts(q.get("t"))
        iv = (snap or {}).get("impliedVolatility")
        if iv is None or not iv_ok(float(iv)):
            if c.invalid_reason is None:
                c.invalid_reason = "no_iv"
            return
        iv_ts = parse_ts(q.get("t"))
        # as_of is the run's start clock; a quote received during the fetch may be stamped after it, so
        # "future" is judged against the time it arrived (received), never against the start.
        got = received or (snap or {}).get("_received") or datetime.now(timezone.utc)   # the page's receipt time
        problem = (iv_age_problem(iv_ts, as_of, received=got) if as_of is not None else "iv_time_unknown")
        drop = problem is not None and (age_gate == "strict" or (age_gate == "open" and problem != "iv_time_unknown"))
        if drop:
            c.iv, c.iv_source, c.iv_ts, c.invalid_reason = None, None, None, problem
            return
        c.iv, c.iv_source, c.iv_ts, c.invalid_reason = float(iv), "indicative", iv_ts, None
