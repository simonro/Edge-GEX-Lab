"""Chain fetching, caching and map assembly shared by the API and the recorder."""
from __future__ import annotations
import math

import json
import threading
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path

import logging

from gexlab import config, magnitude, store
from gexlab.engine import dex as DX
from gexlab.engine import gex as G
from gexlab.engine import voldrift as VD
from gexlab.engine.calendar import (NY, UTC, TradingCalendar, expiry_kind, iso_z, ny_today,
                                    settlement_dt, utc_now, valuation_label)
from gexlab.models import Chain, Contract, Spot
from gexlab.sources import cboe
from gexlab.sources.alpaca import FEED_STATE, AlpacaSource, load_credentials
from gexlab.sources.base import SourceError, limiter

log = logging.getLogger("gexlab.service")

# ---------------------------------------------------------------- calendar

_CAL: dict = {"cal": None, "at": 0.0, "ttl": 0.0, "error": None}


def _calendar_needed(d: date) -> tuple[date, date]:
    """Dates a capture on day d depends on: prior sessions (OI date) through the last expiry."""
    return d - timedelta(days=10), d + timedelta(days=config.WINDOW_DAYS)


def save_calendar_cache(cal: TradingCalendar, path: Path | None = None) -> None:
    from gexlab.sources.base import atomic_write_bytes
    atomic_write_bytes(path or config.CALENDAR_DISK_CACHE, json.dumps(cal.to_json()).encode("utf-8"))


def load_calendar_cache(d: date, path: Path | None = None) -> TradingCalendar | None:
    """The last saved exchange calendar, only if it parses and covers what day d needs."""
    p = path or config.CALENDAR_DISK_CACHE
    try:
        cal = TradingCalendar.from_json(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return cal if cal.covers(*_calendar_needed(d)) else None


def get_calendar(now: datetime | None = None) -> TradingCalendar:
    """Exchange calendar (early closes included).

    Order: live Alpaca calendar (saved to disk on success) -> the validated disk copy
    (source alpaca_cache) -> a weekday calendar (source weekday_fallback). The fallback is
    retried after CALENDAR_RETRY_S, never trusted for the full cache period, and the
    recorder marks any capture computed on it invalid ("calendar_unavailable").
    """
    now = now or utc_now()
    if config.is_demo():
        d = demo_as_of().date()
        return TradingCalendar.weekdays(d - timedelta(days=120), d + timedelta(days=30))
    if _CAL["cal"] is not None and _time.time() - _CAL["at"] < _CAL["ttl"]:
        return _CAL["cal"]
    d = ny_today(now)
    try:
        cal = AlpacaSource(priority="high", who="calendar").calendar(d)
        cal.source = "alpaca"
        cal.fetched_at = cal.fetched_at or iso_z(utc_now())
        try:
            save_calendar_cache(cal)
        except OSError:
            pass
        ttl, err = config.CALENDAR_CACHE_S, None
    except SourceError as e:
        err = str(e)
        cal = load_calendar_cache(d)
        if cal is None:
            cal = TradingCalendar.weekdays(d - timedelta(days=120), d + timedelta(days=30))
        ttl = config.CALENDAR_RETRY_S
    _CAL.update(cal=cal, at=_time.time(), ttl=ttl, error=err)
    return cal


def calendar_status(cal: TradingCalendar | None = None) -> dict:
    cal = cal or get_calendar()
    return {**cal.provenance(), "degraded": not cal.exchange, "last_error": _CAL.get("error")}


# ---------------------------------------------------------------- demo fixtures

def _fixture(name: str) -> dict | list:
    return json.loads((config.FIXTURES_DIR / name).read_text(encoding="utf-8"))


def demo_as_of() -> datetime:
    meta = _fixture("demo_meta.json")
    return datetime.fromisoformat(meta["as_of"].replace("Z", "+00:00"))


def chain_from_fixture(ticker: str) -> Chain:
    path = config.FIXTURES_DIR / f"chain_{ticker.upper()}.json"
    if not path.is_file():
        raise SourceError(f"no demo fixture for {ticker}", status=404)
    j = json.loads(path.read_text(encoding="utf-8"))
    as_of = datetime.fromisoformat(j["as_of"].replace("Z", "+00:00"))
    contracts = [Contract(symbol=c["symbol"], root=c["root"], expiry=date.fromisoformat(c["expiry"]),
                          settlement=c["settlement"], strike=float(c["strike"]), type=c["type"],
                          multiplier=int(c.get("multiplier", 100)), oi=c.get("oi"),
                          bid=c.get("bid"), ask=c.get("ask"), iv=c.get("iv"),
                          iv_source="demo" if c.get("iv") else None, quote_ts=as_of,
                          iv_ts=as_of if c.get("iv") else None,
                          oi_basis="reported" if c.get("oi") is not None else None)
                 for c in j["contracts"]]
    return Chain(ticker=j["ticker"], as_of=as_of,
                 spot=Spot(float(j["spot"]), as_of, "demo"), feed="demo",
                 contracts=contracts, oi_date=j.get("oi_date"), source_ts=as_of,
                 raw_hash=None, warnings=["Synthetic fixture"], requests=0)


# ---------------------------------------------------------------- fetching

def fetch_chain(ticker: str, priority: str = "low", now: datetime | None = None,
                cal: TradingCalendar | None = None, strict: bool = False) -> Chain:
    """Fetch and finalize (T, gamma, GEX at spot) a fresh 0 to 7 DTE chain.

    strict=True (the recorder and `diagnose`): an IV without a known, fresh quote time is never used."""
    ticker = ticker.upper()
    if config.is_demo():
        ch = chain_from_fixture(ticker)
        cal = cal or get_calendar()
    else:
        now = now or utc_now()
        cal = cal or get_calendar(now)
        recorder = priority == "high"      # the recorder keeps its budget but ignores the dashboard cooldown
        if ticker in config.CBOE_TICKERS:
            ch = cboe.fetch_chain(ticker, now, cal, budget_s=config.CBOE_CHAIN_BUDGET_S if recorder else None,
                                  respect_cooldown=not recorder)
        else:
            src = AlpacaSource(priority=priority, who="recorder" if recorder else "api")
            overlay = ((lambda t, cs, at, cl, h: cboe.apply_oi(t, cs, at, cl, h, respect_cooldown=not recorder))
                       if config.OI_SOURCE == "cboe" else None)
            ch = src.fetch_chain(ticker, now, cal, strict=strict, oi_overlay=overlay)
    ch.contracts = G.finalize(ch.contracts, ch.spot.price, ch.valuation, cal)   # T from the valuation clock
    return ch


class _Entry:
    def __init__(self, chain: Chain):
        self.chain = chain
        self.fetched = _time.time()
        self.last_success = utc_now() if not config.is_demo() else chain.as_of
        self.error: str | None = None


_CHAINS: dict[str, _Entry] = {}
_LOCKS: dict[str, threading.Lock] = {}
_LEVEL_CACHE: dict = {}


def cached_chain(ticker: str) -> tuple[Chain, _Entry, bool]:
    """Chain for the live map: reused for REFRESH_MIN_S, stale copy served if a refresh fails.

    Returns (chain, cache slot, stale).
    """
    ticker = ticker.upper()
    lock = _LOCKS.setdefault(ticker, threading.Lock())
    with lock:
        e = _CHAINS.get(ticker)
        if e is None or _time.time() - e.fetched >= config.REFRESH_MIN_S:
            try:
                ch = fetch_chain(ticker, "low")
                e = _Entry(ch)
                _CHAINS[ticker] = e
            except SourceError as ex:
                if e is None:
                    raise
                e.error = str(ex)
                e.fetched = _time.time()          # do not hammer a failing vendor
        stale = (not config.is_demo()) and (utc_now() - e.last_success).total_seconds() > config.STALE_AFTER_S
        return e.chain, e, stale


# ---------------------------------------------------------------- formatting

def fmt_px(x: float | None, ticker: str) -> str:
    if x is None:
        return "n/a"
    if ticker in config.CBOE_TICKERS:
        return f"{math.floor(x + 0.5):,}"  # index points: whole numbers, halves up like the web
    return f"{x:.2f}".rstrip("0").rstrip(".") if x != int(x) else f"{int(x)}"


def fmt_spot(x: float, ticker: str = "") -> str:
    if ticker in config.CBOE_TICKERS:
        return f"{math.floor(x + 0.5):,}"  # index points: whole numbers, halves up
    return f"{x:,.2f}"


def fmt_date_short(iso: str | None) -> str | None:
    if not iso:
        return None
    d = date.fromisoformat(iso)
    return f"{d.strftime('%b')} {d.day}"


def lead_sentence(ticker: str, spot: float, lv: G.Levels, coverage_pct: float, prior_close: bool = False) -> str:
    """Plain-English, data-derived, descriptive only (no signal words). One line at 1920 px:
    the coverage rule detail lives in the coverage panel and the method drawer, not here.
    Vocabulary: always "gamma flip", never "flip" alone or "zero gamma"."""
    s = (f"{ticker} at its prior close ({fmt_spot(spot, ticker)})" if prior_close
         else f"{ticker} at {fmt_spot(spot, ticker)}")
    parts = []
    if lv.nearest_zero_gamma is not None:
        rel = "above" if spot > lv.nearest_zero_gamma else "below" if spot < lv.nearest_zero_gamma else "at"
        parts.append(f"{rel} the gamma flip ({fmt_px(lv.nearest_zero_gamma, ticker)})")
    if lv.call_wall is not None:
        rel = "below" if spot < lv.call_wall else "above" if spot > lv.call_wall else "at"
        parts.append(f"{rel} the call wall ({fmt_px(lv.call_wall, ticker)})")
    if parts:
        body = f"{s} sits " + " and ".join(parts) + "."
    else:
        body = f"{s}."
    if lv.nearest_zero_gamma is None:
        band = int(round(config.SWEEP_BAND * 100))
        body += (f" Net gamma stays {lv.regime if lv.regime != 'neutral' else 'near zero'} "
                 f"with no gamma flip within {band}% of spot.")
    if prior_close:
        body += f" Cboe's first live {ticker} value arrives about 09:45 ET."
    band = int(round(config.COVERAGE_STRIKE_BAND * 100))
    return f"{body} Built from {coverage_pct:.1f}% of the chain within {band}% of spot."


def quote_state(contracts: list[Contract], cov: G.Coverage) -> dict:
    """Whether the selected chain has anything the map can be built from.

    usable is False when no contract within the coverage band is valid. reason "no_live_quotes": no
    contract in the selection has a bid, an ask or an IV (Cboe's delayed payload in the first minutes
    after the open, 2026-10-20); "no_usable_contracts": quotes exist but none is usable. Either way the
    walls, the gamma flip and the regime are unavailable, not neutral or zero."""
    live = sum(1 for c in contracts if (c.bid or 0) > 0 or (c.ask or 0) > 0 or (c.iv or 0) > 0)
    usable = cov.valid > 0
    reason = None if usable else "no_live_quotes" if live == 0 else "no_usable_contracts"
    return {"usable": usable, "reason": reason, "live": live}


def no_quotes_lead(ticker: str, spot: float, q: dict, cov: G.Coverage, feed: str,
                   cal: TradingCalendar | None, now: datetime, market_open: bool) -> str:
    """The lead when quote_state says unusable. Says what is missing; never reads as a market state."""
    s = f"{ticker} at {fmt_spot(spot, ticker)}."
    band = int(round(config.COVERAGE_STRIKE_BAND * 100))
    if q["reason"] == "no_live_quotes":
        if feed == "cboe_delayed" and market_open and cal is not None:
            sess = cal.sessions.get(now.astimezone(NY).date())
            if sess is not None:
                opened = datetime.combine(now.astimezone(NY).date(), sess.open, NY)
                if (now - opened).total_seconds() < config.CBOE_OPEN_GAP_MIN * 60:
                    by = opened + timedelta(minutes=config.CBOE_FIRST_QUOTES_MIN)
                    return (f"{s} No usable quotes yet: Cboe's feed runs about {config.CBOE_DELAY_MIN} minutes "
                            f"behind, so right after the {opened:%H:%M} open it still shows the market before the "
                            f"open. Quotes usually arrive by about {by:%H:%M} ET.")
        src = "Cboe's delayed feed" if feed == "cboe_delayed" else "the feed"
        return f"{s} No usable quotes yet: {src} has no quotes for this chain."
    if cov.eligible == 0:
        return f"{s} No usable quotes: the selected chain has no contracts within {band}% of spot."
    return (f"{s} No usable quotes: none of the {cov.eligible:,} contracts within {band}% of spot "
            "has a usable quote and IV.")


def _signed(v: float, signed: bool) -> float:
    return v if signed else abs(v)


# ---------------------------------------------------------------- map

def build_map(ticker: str, expiries: list[str] | None, signed: bool) -> dict:
    ticker = ticker.upper()
    chain, slot, stale = cached_chain(ticker)
    cal = get_calendar()
    now = chain.as_of if config.is_demo() else utc_now()
    market_open = cal.market_open(now) if not config.is_demo() else True

    exp_set = {date.fromisoformat(e) for e in expiries} if expiries else None
    sel = G.filter_expiries(chain.contracts, exp_set)
    spot = chain.spot.price
    cov = G.coverage(sel, spot)
    eligible, valid, pct = cov.eligible, cov.valid, cov.pct
    quotes = quote_state(sel, cov)

    key = (ticker, slot.fetched, tuple(sorted(exp_set)) if exp_set else None)
    cached = _LEVEL_CACHE.get(key)
    if cached is None:
        lv, _, _ = G.compute_levels(ticker, sel, spot)
        prof = G.profile_curve(sel, spot)
        cached = (lv, prof)
        if len(_LEVEL_CACHE) > 64:
            _LEVEL_CACHE.clear()
        _LEVEL_CACHE[key] = cached
    lv, prof = cached

    lo, hi = spot * (1 - config.DISPLAY_STRIKE_BAND), spot * (1 + config.DISPLAY_STRIKE_BAND)
    rows = G.strike_rows(sel, lo, hi)
    strikes = []
    for r in rows:
        cg, pg = r["call_gex"], _signed(r["put_gex"], signed)
        strikes.append({"strike": r["strike"], "call_gex": cg, "put_gex": pg, "net_gex": cg + pg,
                        "call_oi": r["call_oi"], "put_oi": r["put_oi"]})

    by_exp: dict[date, list[Contract]] = {}
    for c in sel:
        by_exp.setdefault(c.expiry, []).append(c)
    by_expiry = []
    for d in sorted(by_exp):
        cs = by_exp[d]
        cv = G.coverage(cs, spot)
        # Same population as `coverage` (+-10% of spot, confirmed zero OI out), so the rows add up
        # to coverage.eligible / coverage.valid. net_gex still sums every valid contract, like totals.
        by_expiry.append({"expiry": d.isoformat(), "contracts": cv.eligible, "valid": cv.valid,
                          "valid_pct": cv.pct,
                          "net_gex": sum(_signed(c.gex, signed) for c in cs if c.valid)})

    call_tot = sum(c.gex for c in sel if c.valid and c.type == "call")
    put_tot = sum(c.gex for c in sel if c.valid and c.type == "put")
    put_tot_out = _signed(put_tot, signed)

    kind = chain.feed
    val_label = None
    if not market_open and not config.is_demo():
        try:
            at_close = chain.valuation == cal.last_close(now)
        except ValueError:
            at_close = False
        val_label = valuation_label(chain.valuation, at_close)
    if config.is_demo():
        label = config.FEED_LABELS["demo"]
    elif stale:
        label = "Stale: no successful refresh in 3 min"
    elif not market_open:
        label = val_label                                  # "Valued at Fri Oct 16 close", New York time
    else:
        label = config.FEED_LABELS.get(kind, kind)

    warnings = list(chain.warnings)
    available = sorted({c.expiry for c in chain.contracts})
    sel_all = exp_set is None or set(available) <= exp_set
    oi_fresh = None
    if not config.is_demo():
        try:
            exp_oi = cal.prev_session(cal.latest_session_on_or_before(ny_today(now))).isoformat()
            fr = G.oi_freshness(sel, spot, exp_oi)
            oi_fresh = {"expected": exp_oi, "stale_pct": round(fr.stale_frac * 100, 2),
                        "fresh": fr.stale_frac <= config.OI_STALE_MAX_FRAC}
        except (ValueError, KeyError, IndexError):
            oi_fresh = None
    if chain.oi_date:
        d = fmt_date_short(chain.oi_date)
        today = ny_today(now)
        expected = cal.prev_session(cal.latest_session_on_or_before(today))
        if chain.oi_date == expected.isoformat():
            warnings.append(f"OI is from {d} (one session behind).")
        else:
            warnings.append(f"OI is from {d}.")
    oc = G.oi_coverage(sel, spot)
    gc = G.gex_cov(sel, spot)
    if eligible and not G.gex_gate_ok(gc):
        warnings.append(f"Only {gc.pct:.1f}% of the estimated |GEX| within {config.GATE_OI_BAND * 100:g}% of spot "
                        "sits on contracts with usable OI and IV." if gc.pct is not None else
                        f"The |GEX| share within {config.GATE_OI_BAND * 100:g}% of spot could not be computed: "
                        "no usable or estimable contracts in that band.")
    if cov.oi_missing_frac > config.OI_MISSING_MAX_FRAC:
        warnings.append(f"{cov.oi_missing_frac * 100:.0f}% of contracts have no OI record; the OI feed may be incomplete.")
    if not config.is_demo() and not cal.exchange:
        warnings.append("Exchange calendar unavailable: holidays and early closes are not applied to time to expiry.")
    if signed:
        warnings.append("Signed view assumes calls add gamma (+) and puts subtract (-).")
    if slot.error and stale:
        warnings.append("Last refresh failed; showing the previous successful chain.")

    return {
        "ticker": ticker,
        "as_of": iso_z(chain.as_of),
        "model_version": config.MODEL_VERSION,
        "spot": {"price": spot, "ts": iso_z(chain.spot.ts), "source": chain.spot.source,
                 "basis": chain.spot.basis},
        "valuation": {"ts": iso_z(chain.valuation),
                      "basis": "session_close" if val_label and val_label.endswith(" close") else "quote_time",
                      "label": val_label},
        "feed": {"kind": kind, "label": label, "stale": stale, "market_open": market_open,
                 "last_success": iso_z(slot.last_success),
                 "data_ts": iso_z(chain.source_ts or chain.spot.ts),
                 "data_age_s": (round((now - (chain.source_ts or chain.spot.ts)).total_seconds(), 1)
                                if (chain.source_ts or chain.spot.ts) is not None and not config.is_demo() else None)},
        "oi_date": chain.oi_date,
        "oi_source": _oi_source_payload(chain),
        "oi_freshness": oi_fresh,
        "expiry_selection": {"all": sel_all, "selected": len(set(available) & exp_set) if exp_set else len(available),
                             "available": len(available),
                             "expiries": sorted(d.isoformat() for d in (set(available) & exp_set if exp_set else available))},
        "coverage": {"eligible": eligible, "valid": valid, "pct": pct, "oi_pct": oc.pct, "gex_pct": gc.pct,
                     "gate_pass": G.gex_gate_ok(gc), "left_out": gc.left_out,
                     "oi_band_pct": config.GATE_OI_BAND * 100, "gate_version": config.GATE_VERSION,
                     "gate_pct": config.GATE_GEX_MIN_PCT},
        "signed": signed,
        "strikes": strikes,
        "by_expiry": by_expiry,
        "totals": {"call_gex": call_tot, "put_gex": put_tot_out,
                   "net_gex": call_tot + put_tot_out},
        "magnitude": _magnitude(ticker, chain, call_tot, put_tot, spot, cal, now),
        "dex": _dex(sel, spot, chain, quotes["usable"]),
        "vol_drift": _vol_drift(ticker, chain, cal, now, market_open, lv.regime if quotes["usable"] else None),
        "quotes": quotes,
        "levels": {"call_wall": lv.call_wall, "put_wall": lv.put_wall,
                   "zero_gamma": lv.zero_gamma, "nearest_zero_gamma": lv.nearest_zero_gamma,
                   "regime": lv.regime if quotes["usable"] else None},
        "profile_curve": prof,
        "frozen_today": (
            {"captured_at": iso_z(chain.as_of), "status": "eligible", "reason": None, "call_wall": lv.call_wall,
             "put_wall": lv.put_wall, "nearest_zero_gamma": lv.nearest_zero_gamma,
             "model_version": config.MODEL_VERSION, "gate_version": config.GATE_VERSION,
             "oi_source": chain.oi_source, "expiries": sorted({c.expiry.isoformat() for c in chain.contracts}),
             "excluded": None}
            if config.is_demo() else frozen_today(ticker, ny_today(now))),
        "lead": (lead_sentence(ticker, spot, lv, pct, chain.spot.basis == "prior_close") if quotes["usable"] else
                 no_quotes_lead(ticker, spot, quotes, cov, kind, None if config.is_demo() else cal, now, market_open)),
        "warnings": warnings,
    }


def _oi_source_payload(chain: Chain) -> dict | None:
    """Which vendor's open interest the map uses, its label and how its date was set (null in demo)."""
    oi = chain.oi_info or {}
    if not oi:
        return None
    return {"kind": oi.get("source"), "label": oi.get("label"), "basis": oi.get("basis"),
            "oi_date": oi.get("oi_date"), "alpaca_oi_date": oi.get("alpaca_oi_date"),
            "fallback_reason": oi.get("fallback_reason"), "matched": oi.get("matched"),
            "total": oi.get("total"), "differ_frac": oi.get("differ_frac"), "payload_ts": oi.get("payload_ts")}


def _magnitude(ticker: str, chain: Chain, call_gex: float, put_gex: float, spot: float,
               cal: TradingCalendar, now: datetime) -> dict | None:
    """Net GEX size next to average daily dollar volume, per 1% and per ATR, gross, and the percentile.

    Never takes the map down: any failure here leaves `magnitude` null."""
    window_net = float(sum(c.gex for c in chain.contracts if c.valid))
    try:
        return magnitude.build(ticker, net_signed=call_gex + put_gex, window_net_signed=window_net, spot=spot,
                               cal=cal, now=now, call_gex=call_gex, put_gex=put_gex)
    except Exception as ex:  # noqa: BLE001
        log.warning("magnitude unavailable for %s: %s", ticker, ex)
        return None


# ---------------------------------------------------------------- context metrics (display only)

DEX_CAVEAT = ("Built from the previous session's open interest, so it sizes yesterday's positioning at today's price "
              "and IV. It is not a live flow scoreboard.")
DEX_METHOD = ("DEX per contract = delta x OI x 100 x spot, in dollars of the underlying. Delta is Black-Scholes with "
              "r = q = 0, the same IV and valuation clock as GEX. A call's delta is positive and a put's negative, so net "
              "DEX is call delta exposure minus put delta exposure: positive when call delta exposure exceeds put delta "
              "exposure, negative when put delta exposure exceeds call delta exposure. Delta weighted, so it is not "
              "a count of open interest. Each side is also shown without a sign. Valid contracts of the selected "
              "expiries only, as in GEX.")


def _dex(sel: list[Contract], spot: float, chain: Chain, usable: bool) -> dict | None:
    """Delta exposure of the selected expiries. Never takes the map down: a failure leaves `dex` null."""
    try:
        d = DX.compute(sel, spot)
    except Exception as ex:  # noqa: BLE001
        log.warning("dex unavailable for %s: %s", chain.ticker, ex)
        return None
    ok = usable and d.contracts > 0
    return {"available": ok, "reason": None if ok else "No usable contracts in the selected expiries.",
            "net_usd": d.net if ok else None,
            "call_usd": d.call_abs if ok else None, "put_usd": d.put_abs if ok else None,
            "contracts": d.contracts, "oi_date": chain.oi_date, "caveat": DEX_CAVEAT, "method": DEX_METHOD}


VOL_METHOD = ("At-the-money implied volatility of one expiry: the IVs of the two strikes that bracket spot, "
              "interpolated linearly to spot. A strike's IV is the average of its call and put when both have a "
              "usable IV of their own, else the one that does. The expiry is the nearest one with at least three full "
              "trading days left, fixed for the session, never 0DTE: late in the day 0DTE IV is distorted, a few hours of "
              "variance annualized over a sliver of time. Both strikes must lie within 5% of spot with at least 4 usable IVs "
              "there, or the value is unavailable, never a guess.")
VOL_REF_METHOD = ("Reference: the first measurement of the session. When this install records 15-minute snapshots, "
                  "the first usable one of the session (about 09:45 ET); otherwise the first value this app computed "
                  "after the session opened, labeled with its time.")
VOL_CLOCK_NOTE = ("The expiry is fixed for the whole session, so the reference and the current value measure the same "
                  "contracts. Time to expiry is calendar time, so part of any change is the clock itself: as the session "
                  "runs, more of the time left is overnight, when prices usually move less. With at least three full "
                  "sessions to expiry one session's clock is a smaller share of the time left, so that effect is "
                  "smaller than on a next day expiry; it is not removed.")
VOL_CHIP = "Untested: no study tests this pairing"
_REASONS = {
    "no_contracts": "the {exp} expiry is not in the chain",
    "too_few_quotes": "the {exp} expiry has too few usable quotes within 5% of spot",
    "no_bracket": "the {exp} expiry has no usable strike on one side of spot within 5%",
}

_VOL_FIRST: dict[tuple, dict] = {}          # (ticker, session, expiry, settlement) -> first app reading that session
_VOL_REC: dict[tuple, dict] = {}            # same key -> recorder reference once found (captures never change)
_VOL_PIN: dict[tuple, tuple] = {}           # (ticker, session) -> (expiry, settlement): fixed once chosen
_VOL_LOCK = threading.Lock()


def _day_text(d: date) -> str:
    return f"{d.strftime('%a')} {d.strftime('%b')} {d.day}"


def recorder_vol_reference(ticker: str, day: date, expiry: date, settlement: str,
                           path: Path | str | None = None) -> dict | None:
    """ATM IV of the first stored capture of the session (snapshot or 10:00 frozen, status not invalid) that
    yields one on this expiry. Read only."""
    key = (ticker, day.isoformat(), expiry.isoformat(), settlement)
    with _VOL_LOCK:
        if key in _VOL_REC:
            return _VOL_REC[key]
    with store.session(path) as conn:
        caps = conn.execute("SELECT id, kind, completed_at, spot, spot_ts, source_ts FROM captures WHERE ticker=? "
                            "AND session_date=? AND status!='invalid' AND spot IS NOT NULL AND completed_at IS NOT NULL "
                            "ORDER BY completed_at, id", (ticker, day.isoformat())).fetchall()
        for cap in caps:
            rows = conn.execute("SELECT strike, type, iv, iv_shared, iv_own FROM capture_contracts WHERE capture_id=? "
                                "AND expiry=? AND settlement=?", (cap["id"], expiry.isoformat(), settlement)).fetchall()
            cs = [Contract(symbol="", root="", expiry=expiry, settlement=settlement, strike=float(r[0]), type=r[1],
                           iv=r[2], iv_shared=bool(r[3]), iv_own=r[4]) for r in rows]
            res, _ = VD.atm_iv(cs, float(cap["spot"]), expiry, settlement)
            if res is not None:
                out = {"iv": res.iv, "ts": cap["completed_at"], "data_ts": cap["source_ts"] or cap["spot_ts"],
                       "source": "recorder", "kind": cap["kind"]}
                with _VOL_LOCK:
                    for k in [k for k in _VOL_REC if k[1] != key[1]]:
                        del _VOL_REC[k]                 # keep one session in memory
                    _VOL_REC[key] = out
                return out
    return None


def demo_vol_reference(ticker: str, expiry: date, settlement: str) -> dict | None:
    """The synthetic 09:45 reference for demo mode (fixtures), only for the expiry the demo chain targets."""
    try:
        j = json.loads(config.VOL_DRIFT_DEMO_REF.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    r = (j.get("tickers") or {}).get(ticker)
    if not r or r.get("expiry") != expiry.isoformat() or r.get("settlement") != settlement:
        return None
    return {"iv": float(r["iv"]), "ts": r["ts"], "data_ts": r["ts"], "source": "demo", "kind": "snapshot"}


def _remember_first(key: tuple, iv: float, at: datetime, data_ts: datetime) -> dict:
    with _VOL_LOCK:
        if key not in _VOL_FIRST:
            for k in [k for k in _VOL_FIRST if k[1] != key[1]]:
                del _VOL_FIRST[k]                       # keep one session in memory
            _VOL_FIRST[key] = {"iv": iv, "ts": iso_z(at), "data_ts": iso_z(data_ts), "source": "app", "kind": None}
        return _VOL_FIRST[key]


def _pinned_expiry(ticker: str, day: date, contracts: list[Contract], cal: TradingCalendar) -> tuple | None:
    """The session's expiry, chosen once per ticker and session, so the reference and every later reading measure
    the same contracts. A later chain that lacks it reads unavailable; another expiry is never substituted."""
    k = (ticker, day.isoformat())
    with _VOL_LOCK:
        if k in _VOL_PIN:
            return _VOL_PIN[k]
    tgt = VD.target_expiry(contracts, day, cal)
    if tgt is not None:
        with _VOL_LOCK:
            for x in [x for x in _VOL_PIN if x[1] != k[1]]:
                del _VOL_PIN[x]
            tgt = _VOL_PIN.setdefault(k, tgt)
    return tgt


def _parse_z(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def vol_drift(ticker: str, chain: Chain, cal: TradingCalendar, now: datetime, market_open: bool,
              regime: str | None, *, db_path: Path | str | None = None) -> dict:
    """GET /api/map `vol_drift`. Display only: reads the chain and, read only, the stored captures."""
    spot = chain.spot.price
    day = VD.session_day(cal, chain.valuation)
    out = {"available": False, "reason": None, "expiry": None, "settlement": None, "session": day.isoformat(),
           "current": None, "reference": None, "reference_reason": None, "change_pts": None, "label": None,
           "threshold_pts": None, "flat_min_pts": config.VOL_DRIFT_FLAT_MIN_PTS,
           "flat_rel": config.VOL_DRIFT_FLAT_REL, "regime": regime, "pairing": None, "tested": False,
           "chip": VOL_CHIP, "method": VOL_METHOD, "reference_method": VOL_REF_METHOD, "clock_note": VOL_CLOCK_NOTE}
    if not config.is_demo() and not cal.exchange:
        out["reason"] = ("Unavailable: the exchange calendar is unavailable, so the full trading days to expiry "
                         "cannot be confirmed.")
        return out
    if chain.spot.basis == "prior_close":
        # Cboe's file before about 09:45 ET: yesterday's index over pre-open quotes. Never a reading, never a reference.
        out["reason"] = (f"Unavailable until Cboe's first live {ticker} value, about 09:45 ET: the payload still "
                         "carries the prior close.")
        return out
    tgt = _pinned_expiry(ticker, day, chain.contracts, cal)
    if tgt is None:
        out["reason"] = (f"Unavailable: no listed expiry in the 0 to 7 day window has {config.VOL_DRIFT_MIN_FULL_SESSIONS} "
                         "full trading days left.")
        return out
    expiry, st = tgt
    out.update(expiry=expiry.isoformat(), settlement=st)
    # Off hours the vendor's indicative IV is computed on its own wall clock, not the close it is valued at.
    excl = ("indicative",) if not market_open and not config.is_demo() else ()
    res, why = VD.atm_iv(chain.contracts, spot, expiry, st, exclude_sources=excl)
    if res is None:
        out["reason"] = "Unavailable: " + _REASONS.get(why, "the {exp} expiry has no usable ATM IV").format(
            exp=_day_text(expiry)) + "."
        return out
    out["available"] = True
    out["current"] = {"iv_pct": round(res.iv * 100, 2), "ts": iso_z(chain.valuation), "spot": spot,
                      "k_lo": res.k_lo, "k_hi": res.k_hi, "iv_lo_pct": round(res.iv_lo * 100, 2),
                      "iv_hi_pct": round(res.iv_hi * 100, 2), "usable": res.usable}

    key = (ticker, day.isoformat(), expiry.isoformat(), st)
    ref = None
    if config.is_demo():
        ref = demo_vol_reference(ticker, expiry, st)
    else:
        try:
            ref = recorder_vol_reference(ticker, day, expiry, st, db_path)
        except Exception as ex:  # noqa: BLE001
            log.warning("stored vol reference unavailable for %s: %s", ticker, ex)
        sess = cal.sessions.get(day)
        opened = datetime.combine(day, sess.open, NY) if sess is not None else None
        # Only a reading whose ATM inputs carry a known quote time at or after the open can anchor the session.
        times = [c.iv_ts for c in res.inputs]
        known = (opened is not None and times and all(t is not None for t in times)
                 and (chain.feed != "cboe_delayed" or chain.source_ts is not None))
        if market_open and day == ny_today(now) and known and min(times) >= opened and chain.valuation >= opened:
            first = _remember_first(key, res.iv, chain.valuation, max(times))
        else:
            with _VOL_LOCK:
                first = _VOL_FIRST.get(key)
        ref = ref or first
    if ref is None:
        out["reference_reason"] = (f"No reading from the {_day_text(day)} session yet: the reference is the first "
                                   "value measured after the session opened.")
        return out
    out["reference"] = {"iv_pct": round(ref["iv"] * 100, 2), "ts": ref["ts"], "data_ts": ref["data_ts"],
                        "source": ref["source"], "kind": ref["kind"]}
    # The reference's earliest clock (its market-data time or its valuation / completion time): a quote stamped a
    # second after the fetch started must not make the very reading it came from look older than itself.
    ref_ts = [t for t in (_parse_z(ref["data_ts"]), _parse_z(ref["ts"])) if t is not None]
    ref_at = min(ref_ts) if ref_ts else None
    if ref_at is not None and (ref_at - chain.valuation).total_seconds() > config.CLOCK_SKEW_S:
        out["reference_reason"] = ("The current chain is older than the reference, so no change is shown until "
                                   "the next refresh.")
        return out
    chg = (res.iv - ref["iv"]) * 100
    band = VD.flat_band(ref["iv"] * 100)                    # the label reads the unrounded band
    out["threshold_pts"] = round(band, 2)                    # rounded for display only
    out["change_pts"] = round(chg, 1)
    out["label"] = VD.change_label(chg, band)
    out["pairing"] = VD.pairing(regime, out["label"])
    return out


def _vol_drift(ticker: str, chain: Chain, cal: TradingCalendar, now: datetime, market_open: bool,
               regime: str | None) -> dict | None:
    """Never takes the map down: any failure here leaves `vol_drift` null."""
    try:
        return vol_drift(ticker, chain, cal, now, market_open, regime)
    except Exception as ex:  # noqa: BLE001
        log.warning("vol drift unavailable for %s: %s", ticker, ex)
        return None


def expirations(ticker: str) -> dict:
    chain, _, _ = cached_chain(ticker)
    today = ny_today(chain.as_of)
    seen: dict[date, set] = {}
    for c in chain.contracts:
        seen.setdefault(c.expiry, set()).add(c.settlement)
    try:
        cal = get_calendar(chain.as_of)
    except Exception:
        cal = None
    out = []
    for d in sorted(seen):
        st = sorted(seen[d])
        # settles_at: the LAST settlement instant of the date (PM wins over AM, early closes respected),
        # so the dashboard can skip an expiry that has already settled when it picks the default.
        settles = max(settlement_dt(d, s, cal) for s in st)
        out.append({"date": d.isoformat(), "dte": (d - today).days,
                    "settlement": "PM" if "PM" in st else "AM", "settlements": st,
                    "kind": expiry_kind(d), "settles_at": iso_z(settles)})
    return {"ticker": ticker.upper(), "window_days": config.WINDOW_DAYS, "expirations": out}


# ---------------------------------------------------------------- DB reads

def frozen_today(ticker: str, d: date) -> dict | None:
    try:
        with store.session() as conn:
            row = conn.execute("SELECT * FROM captures WHERE kind='frozen' AND ticker=? AND session_date=?",
                               (ticker, d.isoformat())).fetchone()
            if not row:
                return None
            lv = conn.execute("SELECT family, price, is_nearest FROM capture_levels WHERE capture_id=?",
                              (row["id"],)).fetchall()
            exps = [r[0] for r in conn.execute("SELECT DISTINCT expiry FROM capture_contracts WHERE capture_id=? "
                                               "ORDER BY expiry", (row["id"],))]
            x = conn.execute("SELECT reason FROM analysis_exclusions WHERE capture_id=?", (row["id"],)).fetchone()
    except Exception:
        return None
    get = lambda fam: next((r["price"] for r in lv if r["family"] == fam), None)
    nz = next((r["price"] for r in lv if r["family"] == "zero_gamma" and r["is_nearest"]), None)
    return {"captured_at": row["completed_at"], "status": row["status"], "reason": row["status_reason"],
            "call_wall": get("call_wall"), "put_wall": get("put_wall"), "nearest_zero_gamma": nz,
            "model_version": row["model_version"], "gate_version": row["gate_version"] or "1.0",
            "oi_source": row["oi_source"], "expiries": exps, "excluded": x[0].split(":")[0] if x else None}


def captures(ticker: str, days: int, now: datetime | None = None) -> list[dict]:
    ticker = ticker.upper()
    if config.is_demo():
        p = config.FIXTURES_DIR / f"captures_{ticker}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []
    return []   # public edition: no recorder, so no capture history


def _json_or_none(v):
    try:
        return json.loads(v) if v else None
    except ValueError:
        return None


def latest_captures() -> list[dict]:
    """The newest frozen capture per pinned ticker, with its stored invalid-reason counts."""
    out = []
    try:
        with store.session() as conn:
            for t in config.PINNED_TICKERS:
                r = conn.execute("SELECT * FROM captures WHERE kind='frozen' AND ticker=? "
                                 "ORDER BY session_date DESC LIMIT 1", (t,)).fetchone()
                if r:
                    out.append({"ticker": t, "date": r["session_date"], "status": r["status"],
                                "reason": r["status_reason"], "coverage_pct": r["coverage_pct"],
                                "gate_version": r["gate_version"] or "1.0",
                                "oi_coverage_pct": r["oi_coverage_pct"],
                                "gex_coverage_pct": r["gex_coverage_pct"],
                                "eligible_count": r["eligible_count"], "valid_count": r["valid_count"],
                                "invalid_counts": _json_or_none(r["invalid_counts"])})
    except Exception:
        return []
    return out


def _demo_latest_captures() -> list[dict]:
    out = []
    for t in config.PINNED_TICKERS:
        rows = [r for r in captures(t, 60) if r.get("captured_at")]
        if rows:
            r = max(rows, key=lambda x: x["date"])
            out.append({"ticker": t, "date": r["date"], "status": r["status"], "reason": r.get("reason"),
                        "coverage_pct": r.get("coverage_pct"), "eligible_count": r.get("contracts"),
                        "valid_count": r.get("valid"), "invalid_counts": r.get("invalid_counts")})
    return out


def last_frozen() -> dict | None:
    try:
        with store.session() as conn:
            r = conn.execute("SELECT ticker, session_date, status FROM captures WHERE kind='frozen' "
                             "ORDER BY session_date DESC, completed_at DESC LIMIT 1").fetchone()
    except Exception:
        return None
    return {"ticker": r["ticker"], "date": r["session_date"], "status": r["status"]} if r else None


# ---------------------------------------------------------------- search + health

def search(q: str) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    if config.is_demo():
        rows = _fixture("search.json")
    else:
        rows = [{"symbol": "SPX", "name": "S&P 500 Index", "source": "cboe"},
                {"symbol": "NDX", "name": "Nasdaq-100 Index", "source": "cboe"}]
        try:
            rows += [dict(a, source="alpaca") for a in AlpacaSource(who="search").assets()]
        except SourceError:
            pass
    qu, ql = q.upper(), q.lower()
    sym = [r for r in rows if r["symbol"].upper().startswith(qu)]
    sym.sort(key=lambda r: (r["symbol"].upper() != qu, len(r["symbol"]), r["symbol"]))
    names = [r for r in rows if r not in sym and ql in (r.get("name") or "").lower()]
    names.sort(key=lambda r: r["symbol"])
    return [{"symbol": r["symbol"], "name": r.get("name", ""), "source": r.get("source", "alpaca")}
            for r in (sym + names)[:10]]


def health() -> dict:
    demo = config.is_demo()
    if demo:
        feeds = {"alpaca_options": None, "stocks": None, "cboe": False}
        market_open = True
        rate = {"used_last_min": 0, "cap": config.RATE_CAP_PER_MIN}
        calendar = {"source": "demo", "fetched_at": None, "covered": None, "degraded": False, "last_error": None}
    else:
        feeds = {"alpaca_options": FEED_STATE["options"], "stocks": FEED_STATE["stocks"], "cboe": True}
        cal = get_calendar()
        market_open = cal.market_open(utc_now())
        calendar = calendar_status(cal)
        try:
            used = limiter().used_last_min()
        except Exception:
            used = None
        rate = {"used_last_min": used, "cap": config.RATE_CAP_PER_MIN}
    return {"status": "ok", "demo": demo, "feeds": feeds, "market_open": market_open,
            "last_frozen_capture": None if demo else last_frozen(),
            "latest_captures": _demo_latest_captures() if demo else latest_captures(), "rate": rate,
            "keys_configured": demo or load_credentials() is not None, "calendar": calendar}


# ---------------------------------------------------------------- futures view (display transform only)

def futures_view(ticker: str, override_price: float | None = None, override_basis: float | None = None) -> dict:
    """GET /api/futures. Reads the index's Cboe payload through the map's own cache (same 60 s politeness floor)
    and, for SPY/QQQ or a live override, Alpaca REST trades. Never touches the map, the recorder or the studies."""
    from gexlab import futures
    ticker = ticker.upper()
    index = config.ETF_INDEX.get(ticker, ticker)
    if index not in config.FUTURES:
        raise SourceError("The Futures view covers SPX, NDX, SPY and QQQ", status=404)
    if config.is_demo():
        now, cal = demo_as_of(), get_calendar()
        spots = {t: chain_from_fixture(t).spot.price for t in (index, *[e for e, i in config.ETF_INDEX.items() if i == index])}
        demo_spot = lambda sym, at=None, lookback_s=None: Spot(spots[sym], now, "demo")   # noqa: E731
        return futures.build(ticker, now=now, cal=cal, market_open=True,
                             index_body=futures.demo_payload(index, spots[index], now), trade_at=demo_spot,
                             latest_trade=demo_spot, override_price=override_price, override_basis=override_basis,
                             demo=True)
    now = utc_now()
    cal = get_calendar(now)
    body, _ = cboe.fetch_raw(index)
    trade_at = latest = None
    if load_credentials() is not None:
        src = AlpacaSource(who="futures")
        trade_at, latest = src.trade_at, src.latest_trade
    return futures.build(ticker, now=now, cal=cal, market_open=cal.market_open(now), index_body=body,
                         trade_at=trade_at, latest_trade=latest, override_price=override_price,
                         override_basis=override_basis)
