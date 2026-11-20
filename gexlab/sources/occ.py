"""OCC open interest (2026-11-20): the clearing house's own per-series file, for every Alpaca-quoted chain.

Why: Cboe's free payload (the previous OI source) now blocks apps, and Alpaca's contracts endpoint runs a session
behind (on Fri 2026-11-20 at noon it still carried Wed Nov 18 open interest). OCC clears every listed US option
and publishes open interest by series. Its site documents script access ("Batch Processing": "set up your
scripts" with these URLs); its terms bar commercial exploitation, which a free local tool is not. Each user's
app downloads its own copy, once per ticker per OCC_CACHE_S, with a plain identifying User-Agent.

Source: https://marketdata.theocc.com/series-search?symbolType=U&symbol=SPX (text, one row per strike and
expiry: root, year, month, day, strike integer and decimals, C/P flags, call OI, put OI, position limit).
Verified 2026-11-20 11:40 ET: SPXW Nov 20 7150 put 3,527 (Alpaca and Cboe's Nov 18 file: 2,449), so OCC
carried the prior session's positions while Alpaca was a session older.

OCC prints no OI date. It is inferred like Cboe's was: when OCC's numbers match Alpaca's dated ones, Alpaca's
date; otherwise the most recent completed session before the download's New York day. A download before
OCC_FRESH_AFTER (07:00 ET) may still be the older file, so it is dated one session earlier (never newer than
it is). Any failure leaves the chain on Alpaca OI with the reason; this module never raises for vendor trouble.
"""
from __future__ import annotations

import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from gexlab import config
from gexlab.engine.calendar import TradingCalendar
from gexlab.models import Contract
from gexlab.sources.base import Hasher
from gexlab.sources.cboe import _alpaca_info, _date_counts, _fmt_day, infer_oi_date

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
_LOCKS: dict[str, threading.Lock] = {}
_FAILED: dict[str, float] = {}          # ticker -> time of the last failed download (cooldown)


def _cache_path(ticker: str) -> Path:
    return config.ROOT / "data" / "cache" / f"occ_{ticker.upper()}.txt"


def parse_series(text: str) -> dict[str, int]:
    """{OCC option symbol: open interest} from a series-search file. Rows whose shape does not fit are skipped
    (headers, adjusted roots with odd strikes still parse; they simply match no Alpaca contract)."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        t = line.split()
        if len(t) < 9 or not (t[1].isdigit() and len(t[1]) == 4):
            continue
        root, y, m, d, ki, kd = t[0], t[1], t[2], t[3], t[4], t[5]
        rest = t[6:]
        flags = [x for x in rest if x in ("C", "P")]
        nums = [x for x in rest if x not in ("C", "P")]
        if len(nums) < 2 or not all(x.isdigit() for x in (m, d, ki, kd, nums[0], nums[1])):
            continue
        strike_milli = int(ki) * 1000 + int(kd.ljust(3, "0")[:3])
        stem = f"{root}{y[2:]}{int(m):02d}{int(d):02d}"
        if "C" in flags:
            out[f"{stem}C{strike_milli:08d}"] = int(nums[0])
        if "P" in flags:
            out[f"{stem}P{strike_milli:08d}"] = int(nums[1])
    return out


def fetch_series(ticker: str, client: httpx.Client | None = None, now: float | None = None) -> tuple[str, datetime]:
    """The series-search file for one underlying and the time it was downloaded. Reuses the disk copy while it is
    younger than OCC_CACHE_S and from the same New York day. Raises RuntimeError on any failure."""
    ticker = ticker.upper()
    now = _time.time() if now is None else now
    path = _cache_path(ticker)
    lock = _LOCKS.setdefault(ticker, threading.Lock())
    with lock:
        if path.is_file():
            mtime = path.stat().st_mtime
            same_day = (datetime.fromtimestamp(mtime, NY).date() == datetime.fromtimestamp(now, NY).date())
            if now - mtime < config.OCC_CACHE_S and same_day:
                return path.read_text(encoding="utf-8"), datetime.fromtimestamp(mtime, UTC)
        if now - _FAILED.get(ticker, 0.0) < config.OCC_FAIL_COOLDOWN_S:
            raise RuntimeError("OCC failed moments ago; waiting before asking again")
        own = client is None
        client = client or httpx.Client(timeout=config.OCC_TIMEOUT_S, follow_redirects=True)
        try:
            r = client.get(config.OCC_SERIES_URL, params={"symbolType": "U", "symbol": ticker},
                           headers={"User-Agent": f"edge-gex-lab/{config.APP_VERSION} (+https://github.com/{config.UPDATE_REPO})"})
        except httpx.HTTPError as e:
            _FAILED[ticker] = now
            raise RuntimeError(f"OCC download failed: {type(e).__name__}") from e
        finally:
            if own:
                client.close()
        if r.status_code != 200:
            _FAILED[ticker] = now
            raise RuntimeError(f"OCC HTTP {r.status_code}")
        text = r.text
        if "Series Search Results" not in text[:400]:
            _FAILED[ticker] = now
            raise RuntimeError("OCC answered with something other than a series file")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return text, datetime.fromtimestamp(now, UTC)


def oi_date_for(cal: TradingCalendar | None, fetched: datetime, as_of: datetime) -> str | None:
    """The session OCC's file most likely carries: the one before the download's day, or one earlier for a
    download before OCC_FRESH_AFTER (the overnight file may not be out yet)."""
    d = infer_oi_date(cal, fetched, as_of)
    if d is None or cal is None or fetched.astimezone(NY).time() >= config.OCC_FRESH_AFTER:
        return d
    try:
        from datetime import date
        return cal.prev_session(date.fromisoformat(d)).isoformat()
    except (ValueError, KeyError, IndexError):
        return d


def apply_oi(ticker: str, contracts: list[Contract], as_of: datetime, cal: TradingCalendar | None,
             hasher: Hasher | None = None, fetch=None, client: httpx.Client | None = None) -> dict:
    """Replace Alpaca open interest with OCC's, joined by OCC symbol. Mutates the contracts and returns the OI
    provenance in the same shape as cboe.apply_oi (source "occ"). Never raises for vendor trouble."""
    if not contracts:
        return _alpaca_info(contracts, None)
    alpaca_dates = [c.oi_date for c in contracts if c.oi is not None and c.oi_date]
    alpaca_date = max(alpaca_dates) if alpaca_dates else None
    try:
        text, fetched = fetch(ticker) if fetch is not None else fetch_series(ticker, client)
    except RuntimeError as e:
        return _alpaca_info(contracts, f"OCC unavailable ({e})")
    occ_oi = parse_series(text)
    matched = [c for c in contracts if c.symbol in occ_oi]
    if len(matched) < config.OCC_OI_MIN_MATCH * len(contracts):
        return _alpaca_info(contracts, f"OCC lists {len(matched)} of {len(contracts)} contracts")
    both = [c for c in matched if c.oi is not None]
    differ = sum(1 for c in both if occ_oi[c.symbol] != c.oi)
    differ_frac = differ / len(both) if both else None
    inferred = oi_date_for(cal, fetched, as_of)
    if alpaca_date and inferred and inferred < alpaca_date:
        return _alpaca_info(contracts, f"OCC OI (inferred {inferred}) is older than Alpaca's ({alpaca_date})")
    if differ_frac is not None and differ_frac <= config.CBOE_OI_SAME_MAX_FRAC and alpaca_date:
        basis, oi_date = "matches_alpaca", alpaca_date
        label = f"OI: OCC (matches Alpaca, {_fmt_day(alpaca_date)})"
    else:
        basis, oi_date = "inferred", inferred
        label = f"OI: OCC ({_fmt_day(inferred)}, prior session)"
    if hasher is not None:
        hasher.add(text.encode("utf-8"))     # the OI is a valuation input: bind the file in response_hash
    for c in contracts:
        if c.symbol in occ_oi:
            same = basis == "matches_alpaca" and c.oi is not None and c.oi_date and occ_oi[c.symbol] == c.oi
            c.oi, c.oi_date = occ_oi[c.symbol], (c.oi_date if same else inferred)
            c.oi_basis, c.oi_source = "reported", "occ"
        else:
            c.oi_source = "alpaca"
    mixed = len(matched) < len(contracts)
    if mixed:
        label += f" + Alpaca on {len(contracts) - len(matched)} contracts"
    dates = [c.oi_date for c in contracts if c.oi is not None and c.oi_date]
    return {"source": "occ", "basis": basis, "label": label, "oi_date": oi_date, "alpaca_oi_date": alpaca_date,
            "fallback_reason": None, "matched": len(matched), "total": len(contracts),
            "differ_frac": round(differ_frac, 4) if differ_frac is not None else None,
            "payload_ts": fetched.isoformat(), "mixed": mixed,
            "oi_date_min": min(dates) if dates else None, "oi_dates": _date_counts(contracts)}
