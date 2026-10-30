"""Vol Drift: is at-the-money implied volatility rising or falling today. Display only.

Measure: ATM IV of one expiry, interpolated linearly in strike between the two strikes that bracket spot.
A strike's IV is the average of its call and put when both are usable, else the one that is. Usable means
the contract's OWN IV (before the strike-pair sharing the GEX model does) passes iv_ok(); an IV the feed
dropped for its age never reaches here. No extrapolation: a side with no strike, a bracket wider than the band
or too few usable quotes is unavailable, never a guess.

Expiry: the nearest one that settles at or after the close of the session after the current one (at least
VOL_DRIFT_MIN_FULL_SESSIONS full trading days left), never 0DTE. When that expiry is listed with both AM and PM
roots (SPX and NDX monthlies), the PM root is used so the two clocks never mix. If that expiry has too few
usable quotes the result is unavailable; the next expiry is NOT used instead, because the reference was taken
on this one and two expiries are not comparable.

Change: current minus reference in vol points. The label reads the change as displayed (0.1 point) against a
band relative to the IV level: flat while below max(VOL_DRIFT_FLAT_MIN_PTS, VOL_DRIFT_FLAT_REL x reference IV),
rising or falling at or beyond it.

Nothing here touches the GEX math, the levels, the gate, the recorder captures or the studies.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from gexlab import config
from gexlab.engine.calendar import TradingCalendar, settlement_dt
from gexlab.engine.gex import iv_ok
from gexlab.models import Contract


@dataclass
class AtmIv:
    iv: float                  # decimal, e.g. 0.1432
    expiry: date
    settlement: str            # AM | PM
    k_lo: float
    k_hi: float
    iv_lo: float
    iv_hi: float
    usable: int                # usable IVs (calls plus puts) inside the band
    inputs: list               # the contracts whose own IV entered the two bracketing strikes


def own_iv(c: Contract, exclude_sources: tuple = ()) -> float | None:
    """The contract's own IV: before strike-pair sharing, after the feed's age checks. None when unusable or when
    its method is in exclude_sources (off hours: the vendor's indicative IV runs on its own clock)."""
    v = c.iv_own if c.iv_shared else c.iv
    src = c.iv_own_source if c.iv_shared else c.iv_source
    if exclude_sources and src in exclude_sources:
        return None
    return float(v) if iv_ok(v) else None


def next_session(cal: TradingCalendar, d: date) -> date:
    x = d + timedelta(days=1)
    for _ in range(15):
        if cal.is_session(x):
            return x
        x += timedelta(days=1)
    return x


def session_day(cal: TradingCalendar, valuation: datetime) -> date:
    """The session the measurement belongs to: the valuation's New York day, or the session before it."""
    from gexlab.engine.calendar import NY
    return cal.latest_session_on_or_before(valuation.astimezone(NY).date())


def target_expiry(contracts: list[Contract], day: date, cal: TradingCalendar,
                  min_sessions: int | None = None) -> tuple[date, str] | None:
    """(expiry, settlement) of the nearest expiry settling at or after the close of the session `min_sessions`
    sessions after `day`. PM wins over AM on the same date. None when the chain lists no such expiry."""
    n = config.VOL_DRIFT_MIN_FULL_SESSIONS if min_sessions is None else min_sessions
    last = day
    for _ in range(max(0, n)):
        last = next_session(cal, last)
    need = cal.close_dt(last)
    by: dict[date, set[str]] = {}
    for c in contracts:
        by.setdefault(c.expiry, set()).add(c.settlement)
    for d in sorted(by):
        st = "PM" if "PM" in by[d] else "AM"
        if settlement_dt(d, st, cal) >= need:
            return d, st
    return None


def atm_iv(contracts: list[Contract], spot: float, expiry: date, settlement: str,
           band: float | None = None, min_usable: int | None = None,
           exclude_sources: tuple = ()) -> tuple[AtmIv | None, str | None]:
    """(result, None) or (None, reason). reason: no_contracts | too_few_quotes | no_bracket."""
    band = config.VOL_DRIFT_BAND if band is None else band
    min_usable = config.VOL_DRIFT_MIN_USABLE if min_usable is None else min_usable
    cs = [c for c in contracts if c.expiry == expiry and c.settlement == settlement]
    if not cs:
        return None, "no_contracts"
    by: dict[float, dict[str, float]] = {}
    used: dict[float, list[Contract]] = {}
    usable = 0
    for c in cs:
        v = own_iv(c, exclude_sources)
        if v is None:
            continue
        by.setdefault(c.strike, {})[c.type] = v
        used.setdefault(c.strike, []).append(c)
        if abs(c.strike / spot - 1.0) <= band + 1e-12:
            usable += 1
    if usable < min_usable:
        return None, "too_few_quotes"
    k_iv = {k: sum(v.values()) / len(v) for k, v in by.items()}
    lo = [k for k in k_iv if k <= spot and spot - k <= band * spot + 1e-9]
    hi = [k for k in k_iv if k >= spot and k - spot <= band * spot + 1e-9]
    if not lo or not hi:
        return None, "no_bracket"
    k_lo, k_hi = max(lo), min(hi)
    iv_lo, iv_hi = k_iv[k_lo], k_iv[k_hi]
    w = 0.0 if k_hi == k_lo else (spot - k_lo) / (k_hi - k_lo)
    inputs = used[k_lo] + (used[k_hi] if k_hi != k_lo else [])
    return AtmIv(iv_lo + w * (iv_hi - iv_lo), expiry, settlement, k_lo, k_hi, iv_lo, iv_hi, usable, inputs), None


def flat_band(ref_iv_pct: float) -> float:
    """Half width of the flat band in vol points: max(VOL_DRIFT_FLAT_MIN_PTS, VOL_DRIFT_FLAT_REL x reference IV)."""
    return max(config.VOL_DRIFT_FLAT_MIN_PTS, config.VOL_DRIFT_FLAT_REL * ref_iv_pct)


def change_label(change_pts: float, threshold: float) -> str:
    """rising | falling | flat, read on the change as displayed (rounded to 0.1 vol point)."""
    t = threshold
    shown = round(change_pts, 1)
    if shown >= t:
        return "rising"
    if shown <= -t:
        return "falling"
    return "flat"


PAIRINGS = {
    ("negative", "rising"): "Negative gamma and rising IV: the conditions where moves tend to get bigger.",
    ("negative", "falling"): "Negative gamma with falling IV: the options market is pricing smaller moves than at "
                             "the reference, even with dealers modeled short gamma.",
    ("negative", "flat"): "Negative gamma with flat IV: the change in IV is inside the flat band.",
    ("positive", "rising"): "Positive gamma with rising IV: the options market is pricing bigger moves than at the "
                            "reference, even with dealers modeled long gamma.",
    ("positive", "falling"): "Positive gamma and falling IV: the conditions where moves tend to stay smaller.",
    ("positive", "flat"): "Positive gamma with flat IV: the change in IV is inside the flat band.",
    ("neutral", "rising"): "Gamma near neutral with rising IV: the options market is pricing bigger moves.",
    ("neutral", "falling"): "Gamma near neutral with falling IV: the options market is pricing smaller moves.",
    ("neutral", "flat"): "Gamma near neutral with flat IV: the change in IV is inside the flat band.",
}


def pairing(regime: str | None, label: str | None) -> str | None:
    if regime is None or label is None:
        return None
    return PAIRINGS.get((regime, label))
