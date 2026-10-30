"""Cboe public delayed quotes: the whole chain for SPX and NDX (Alpaca has no index option
quotes), and open interest only for SPY, QQQ and stocks (model 1.1, apply_oi below).

Source: https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json (about
13 MB) and _NDX.json (about 7 MB). Delayed about 15 minutes. This source is
used knowingly (the terms risk is stated in the README); we stay polite: a browser User-Agent that
also names Edge GEX Lab, at most one fetch per ticker per 60 s, cached on disk so the
API server and the recorder share one fetch. The cache is guarded by a
cross-process file lock: freshness is rechecked under the lock, and the body is
written to a unique temp file and atomically swapped in. A downloaded payload whose
`timestamp` is older than the cached one's (the CDN can serve a stale edge copy) never
replaces the cache and is never served: the cached payload is returned instead.

A payload with no `timestamp` keeps source_ts and spot.ts as None (age unknown);
the request time is never substituted, so such a capture cannot pass the
freshness gate.

We use Cboe's `iv` (solving from the mid only where Cboe reports 0, which it
does for many in-the-money contracts) and compute gamma ourselves so every ticker runs through the
same model math. OI date is not published in the payload; it is inferred
as the session before the latest session on or before the payload date, and the
map says so.

Open interest for Alpaca-quoted tickers (config.OI_SOURCE = "cboe"): Alpaca's contracts
endpoint can lag a session or more (verified 2026-10-19: QQQ Oct 19 0DTE still dated Oct 15
at 11:50 ET). apply_oi() joins Cboe's open_interest onto the Alpaca contracts by OCC symbol;
quotes, spot and IV stay on Alpaca. If Cboe is unreachable, unreadable, lists under
CBOE_OI_MIN_MATCH of the contracts, or carries older OI than Alpaca, the chain keeps Alpaca
OI and says why. OI date: when Cboe's numbers differ from Alpaca's (more than
CBOE_OI_SAME_MAX_FRAC of the contracts both report), Cboe is the newer file and its date is
inferred as the most recent completed session before the payload day; when they match, the
numbers are Alpaca's vintage and take Alpaca's reported date.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutTimeout
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import httpx

from gexlab import config
from gexlab.engine.calendar import NY, UTC, TradingCalendar, ny_today, predates_session, time_to_expiry, valuation_time
from gexlab.engine.gex import iv_ok
from gexlab.engine.iv import implied_volatility
from gexlab.models import Chain, Contract, Spot, parse_occ
from gexlab.sources.base import Hasher, SourceError, atomic_write_bytes, file_lock

_LOCKS: dict[str, threading.Lock] = {"SPX": threading.Lock(), "NDX": threading.Lock()}


def parse_cboe_timestamp(s: str) -> datetime:
    dt = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")
    tz = UTC if config.CBOE_TIMESTAMP_TZ.upper() == "UTC" else ZoneInfo(config.CBOE_TIMESTAMP_TZ)
    return dt.replace(tzinfo=tz).astimezone(UTC)


def parse_index_time(s: str | None) -> datetime | None:
    """Cboe's `last_trade_time` ("2026-10-21T11:44:31") is New York wall time with no zone (checked 2026-10-21:
    payload timestamp 15:59:33Z = 11:59:33 ET, last_trade_time 11:44:31, the 15 minute delay)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip()[:19]).replace(tzinfo=NY).astimezone(UTC)
    except (TypeError, ValueError):
        return None


PRIOR_CLOSE_WARNING = ("{t} is the prior close: Cboe's first live {t} value arrives about 09:45 ET. Spot, the gamma "
                       "sign and the distances are measured at that close; the walls are strikes and still apply.")


def settlement_for_root(ticker: str, root: str) -> str | None:
    return config.CBOE_ROOTS.get(ticker, {}).get(root)


_SYMBOL_RE = re.compile(r"^[A-Z]{1,6}$")


def cboe_symbol(ticker: str) -> str:
    """Cboe payload symbol: _SPX / _NDX for the indexes, the plain ticker for ETFs and stocks.

    Tickers Cboe spells differently (BRK.B and other class shares) are refused, so they fall
    back to Alpaca OI instead of fetching a wrong or missing file."""
    t = ticker.upper().strip()
    if t in config.CBOE_INDEX_SYMBOLS:
        return config.CBOE_INDEX_SYMBOLS[t]
    if not _SYMBOL_RE.match(t):
        raise SourceError(f"no Cboe payload symbol for {ticker!r}")
    return t


def _cooldown_path(ticker: str):
    return config.CACHE_DIR / f"cboe_{ticker}.cooldown"


def cooling_down(ticker: str, now: float | None = None) -> float:
    """Seconds left in the shared failure cooldown for this payload (0 = none)."""
    try:
        until = float(_cooldown_path(ticker).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0
    return max(0.0, until - (now or _time.time()))


def _retry_after_s(v: str | None, now: float) -> float:
    """Retry-After as seconds: either a number or an HTTP date."""
    if not v:
        return 0.0
    try:
        return max(0.0, float(v))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(v).timestamp() - now)
    except (TypeError, ValueError, IndexError, OverflowError):
        return 0.0


def _start_cooldown(ticker: str, retry_after: str | None, now: float) -> None:
    ra = _retry_after_s(retry_after, now)
    wait = min(config.CBOE_FAIL_COOLDOWN_MAX_S, max(config.CBOE_FAIL_COOLDOWN_S, ra))
    try:
        atomic_write_bytes(_cooldown_path(ticker), f"{now + wait:.3f}".encode())
    except OSError:
        pass                                   # a cooldown is politeness, never a reason to fail


def fetch_raw(ticker: str, client: httpx.Client | None = None, now: float | None = None,
              max_age_s: float | None = None, budget_s: float | None = None,
              respect_cooldown: bool = True) -> tuple[bytes, bool]:
    """Raw payload bytes, from the disk cache when fresh. Returns (body, from_cache).

    max_age_s defaults to CBOE_CACHE_S (60 s, the politeness floor); the OI-only path passes
    CBOE_OI_CACHE_S. Never below the floor. The thread lock serialises this process; the file
    lock serialises the API, the frozen run and the snapshot run, which are separate
    processes. Freshness is rechecked after the lock is taken, so a fetch another process
    just finished is reused.

    budget_s bounds the WHOLE call, a hard deadline: the in-process lock, the cross-process lock and
    the download (streamed, checked per chunk) all count against it (day-1 audit: the 20 s OI timeout
    did not cover a 120 s lock wait, and a per-read timeout does not bound a slow download). A failed fetch starts
    a shared cooldown (CBOE_FAIL_COOLDOWN_S, or Retry-After); respect_cooldown=False (the recorder)
    still fetches during it, every other caller gets an immediate SourceError instead.
    """
    ticker = ticker.upper()
    symbol = cboe_symbol(ticker)
    max_age = max(config.CBOE_CACHE_S, max_age_s if max_age_s is not None else config.CBOE_CACHE_S)
    cache = config.CACHE_DIR / f"cboe_{ticker}.json"
    lock = _LOCKS.setdefault(ticker, threading.Lock())
    t0 = _time.monotonic()
    deadline = None if budget_s is None else t0 + budget_s
    lock_s = config.CBOE_LOCK_TIMEOUT_S if budget_s is None else max(0.1, min(config.CBOE_LOCK_TIMEOUT_S, budget_s))
    if not lock.acquire(timeout=lock_s):
        raise SourceError("Cboe time budget spent waiting for another request in this process")
    try:
        left = lock_s if deadline is None else max(0.1, deadline - _time.monotonic())
        with file_lock(config.CACHE_DIR / f"cboe_{ticker}.lock", timeout_s=left):
            return _fetch_locked(ticker, symbol, cache, max_age, client, now, deadline, respect_cooldown)
    finally:
        lock.release()


def _fetch_locked(ticker, symbol, cache, max_age, client, now, deadline, respect_cooldown) -> tuple[bytes, bool]:
    """fetch_raw's body, run with both locks held: cache, cooldown, then a streamed, deadline-checked GET."""
    t_now = now or _time.time()
    if cache.is_file() and t_now - cache.stat().st_mtime < max_age:
        return cache.read_bytes(), True
    if respect_cooldown:
        left = cooling_down(ticker, t_now)
        if left > 0:
            raise SourceError(f"Cboe cooling down after a failure ({left:.0f} s left)")
    timeout = 60.0
    if deadline is not None:
        timeout = deadline - _time.monotonic()
        if timeout < 1.0:
            raise SourceError("Cboe time budget spent waiting for the cache lock")
    own = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    url = config.CBOE_URL.format(symbol=symbol)
    pool = None
    try:
        if deadline is None:
            status, retry_after, body = _download(client, url, timeout)
        else:
            # A hard wall clock: the request (headers and every read) runs in a worker and this thread
            # stops waiting at the deadline. A per-read timeout alone lets a slow download run on
            # (day-1 verification). An abandoned worker never writes the cache: only this thread does.
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                status, retry_after, body = pool.submit(_download, client, url, timeout).result(
                    timeout=max(0.1, deadline - _time.monotonic()))
            except _FutTimeout:
                _start_cooldown(ticker, None, _time.time())
                raise SourceError("Cboe download exceeded its time budget") from None
    except httpx.HTTPError as e:
        _start_cooldown(ticker, None, _time.time())
        raise SourceError(f"Cboe fetch failed: {type(e).__name__}") from e
    finally:
        if own:
            client.close()                     # also ends an abandoned worker's download
        if pool is not None:
            pool.shutdown(wait=False)
    if status != 200:
        _start_cooldown(ticker, retry_after, _time.time())
        raise SourceError(f"Cboe HTTP {status}", status=status)
    held = cache.read_bytes() if cache.is_file() else None
    if held is not None and _older(body, held):
        # Cboe's CDN can answer with an older copy than the one on disk (2026-10-20: feed.data_ts went
        # 13:44:48Z, 13:43:12Z, 13:45:09Z). An older payload never replaces the cache and is never
        # served; the held file is touched so the 60 s politeness floor still applies.
        try:
            os.utime(cache)
        except OSError:
            pass
        return held, False
    atomic_write_bytes(cache, body)
    return body, False


_TS_RE = re.compile(rb'"timestamp"\s*:\s*"([^"]{1,40})"')


def payload_ts(body: bytes) -> datetime | None:
    """The payload's top-level timestamp without a full parse (the SPX file is about 13 MB); None if absent."""
    m = _TS_RE.search(body)
    if not m:
        return None
    try:
        return parse_cboe_timestamp(m.group(1).decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None


def _older(new: bytes, held: bytes) -> bool:
    """True only when both payloads carry a timestamp and the new one is strictly older."""
    a, b = payload_ts(new), payload_ts(held)
    return a is not None and b is not None and a < b


def _download(client: httpx.Client, url: str, timeout: float) -> tuple[int, str | None, bytes]:
    with client.stream("GET", url, timeout=timeout,
                       headers={"User-Agent": config.CBOE_USER_AGENT, "Accept": "application/json"}) as r:
        body = b"".join(r.iter_bytes()) if r.status_code == 200 else b""
        return r.status_code, r.headers.get("Retry-After"), body


def _solve_from_mid(c: Contract, spot: float, ts: datetime, cal: TradingCalendar | None) -> None:
    """ts is the chain's valuation time (T math only).

    Cboe reports iv = 0 for many ITM contracts. Solve from the mid (same policy as the OPRA path)."""
    from gexlab.sources.alpaca import mid_usable
    if not mid_usable(c.bid, c.ask):
        c.invalid_reason = "no_iv"
        return
    T = time_to_expiry(ts, c.expiry, c.settlement, cal)
    iv = implied_volatility(0.5 * (c.bid + c.ask), spot, c.strike, T, 0.0, c.type)
    if iv_ok(iv):
        c.iv, c.iv_source = iv, "solved_mid"
    else:
        c.invalid_reason = "no_iv"


def parse_chain(ticker: str, body: bytes, as_of: datetime, cal: TradingCalendar | None) -> Chain:
    ticker = ticker.upper()
    j = json.loads(body)
    data = j.get("data") or {}
    price = data.get("current_price")
    if not price:
        raise SourceError("Cboe payload has no current_price")
    src_ts = parse_cboe_timestamp(j["timestamp"]) if j.get("timestamp") else None   # never the request time
    # Valuation clock: the payload time, never later than now; with the market closed, never later
    # than the last session close (a 16:15 payload on a Saturday is valued at Friday's close).
    # A payload with no timestamp is valued at now (or the close); its provenance stays None.
    t_ref = valuation_time(cal, as_of, src_ts)
    today = ny_today(as_of)
    last = today + timedelta(days=config.WINDOW_DAYS)
    lo = price * (1 - config.FETCH_STRIKE_BAND)
    hi = price * (1 + config.FETCH_STRIKE_BAND)
    contracts = []
    for o in data.get("options") or []:
        try:
            occ = parse_occ(o["option"])
        except (KeyError, ValueError):
            continue
        settle = settlement_for_root(ticker, occ.root)
        if settle is None or not (today <= occ.expiry <= last) or not (lo <= occ.strike <= hi):
            continue
        oi = o.get("open_interest")
        iv = o.get("iv")
        c = Contract(symbol=o["option"], root=occ.root, expiry=occ.expiry, settlement=settle,
                     strike=occ.strike, type=occ.type, multiplier=100,
                     oi=int(oi) if oi is not None else None,
                     bid=o.get("bid"), ask=o.get("ask"), quote_ts=src_ts, iv_own=float(iv) if iv else None,
                     iv_own_source="cboe" if iv else None,
                     iv=float(iv) if iv else None, iv_source="cboe" if iv else None)
        if not iv:
            _solve_from_mid(c, float(price), t_ref, cal)
            c.iv_own, c.iv_own_source = c.iv, c.iv_source
        contracts.append(c)
    oi_date = None
    warnings = ["Cboe does not publish the OI date; shown as the prior session (inferred)."]
    if src_ts is None:
        warnings.append("Cboe payload has no timestamp; data age is unknown.")
    if cal is not None and src_ts is not None:
        src_day = src_ts.astimezone(ZoneInfo("America/New_York")).date()
        oi_date = cal.prev_session(cal.latest_session_on_or_before(src_day)).isoformat()
    for c in contracts:
        c.oi_date = oi_date
        c.oi_basis = "reported" if c.oi is not None else None
        c.oi_source = "cboe"
        c.iv_ts = src_ts if c.iv is not None else None   # the payload time is the quote time of every IV
    # Market closed and the payload is from or after the last close: current_price is the index's
    # regular-session close (indexes have no extended hours). Label it so, stamped at the close; the
    # payload time stays in source_ts (feed.data_ts) for the method drawer.
    spot = Spot(float(price), src_ts, "cboe", basis="payload")
    index_ts = parse_index_time(data.get("last_trade_time"))
    if cal is not None and src_ts is not None and not cal.market_open(as_of):
        try:
            close = cal.last_close(as_of)
        except ValueError:
            close = None
        # The closing value only once the index's own time reaches the close: the 16:00 to 16:15 payloads still
        # carry the 15:45 to 16:00 value (seen 2026-10-29: 7706.14 in the 16:00 file, close 7720.39 at 16:14:59).
        if close is not None and src_ts >= close and (index_ts is None or index_ts >= close):
            spot = Spot(float(price), close, "cboe", basis="regular_close")
        elif close is not None and index_ts is not None and index_ts < close and src_ts >= close:
            spot = Spot(float(price), index_ts, "cboe", basis="payload")
    # Market open but the index value predates today's open: Cboe still carries the prior close (until about
    # 09:45 ET). Stamp the spot with the index's own time and say so; the payload time stays in source_ts.
    if predates_session(cal, index_ts, as_of):
        spot = Spot(float(price), index_ts, "cboe", basis="prior_close")
        warnings.insert(0, PRIOR_CLOSE_WARNING.format(t=ticker))
    return Chain(ticker=ticker, as_of=as_of, spot=spot,
                 feed="cboe_delayed", contracts=contracts, oi_date=oi_date, source_ts=src_ts, index_ts=index_ts,
                 complete=True, raw_hash=hashlib.sha256(len(body).to_bytes(8, "big") + body).hexdigest(),
                 warnings=warnings, requests=1, valuation_ts=t_ref, oi_source="cboe",
                 oi_info={"source": "cboe", "basis": "inferred", "label": CBOE_INFERRED_LABEL, "oi_date": oi_date,
                          "alpaca_oi_date": None, "fallback_reason": None, "matched": len(contracts),
                          "total": len(contracts), "differ_frac": None,
                          "payload_ts": src_ts.isoformat() if src_ts is not None else None})


def fetch_chain(ticker: str, as_of: datetime, cal: TradingCalendar | None,
                client: httpx.Client | None = None, budget_s: float | None = None,
                respect_cooldown: bool = True) -> Chain:
    body, cached = fetch_raw(ticker, client, budget_s=budget_s, respect_cooldown=respect_cooldown)
    ch = parse_chain(ticker, body, as_of, cal)
    ch.requests = 0 if cached else 1
    return ch


# ---------------------------------------------------------------- open interest for Alpaca-quoted tickers

def normalize_option(code: str) -> str:
    """Cboe's option code as an OCC symbol: no spaces, upper case (QQQ261019C00725000)."""
    return code.replace(" ", "").strip().upper()


def parse_oi(body: bytes) -> tuple[dict[str, int], datetime | None]:
    """{OCC symbol: open interest} and the payload time. Raises ValueError on an unreadable payload."""
    try:
        j = json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError("not JSON") from e
    if not isinstance(j, dict) or not isinstance(j.get("data"), dict):
        raise ValueError("no data")
    out: dict[str, int] = {}
    for o in j["data"].get("options") or []:
        code, oi = o.get("option"), o.get("open_interest")
        if not code or oi is None:
            continue
        try:
            out[normalize_option(code)] = int(round(float(oi)))
        except (TypeError, ValueError):
            continue
    try:
        ts = parse_cboe_timestamp(j["timestamp"]) if j.get("timestamp") else None
    except (TypeError, ValueError):
        ts = None
    return out, ts


def _fmt_day(iso: str | None) -> str:
    if not iso:
        return "date unknown"
    d = date.fromisoformat(iso)
    return f"{d.strftime('%b')} {d.day}"


def alpaca_label(oi_date: str | None) -> str:
    return f"OI: Alpaca ({_fmt_day(oi_date)})"


CBOE_INFERRED_LABEL = "OI: Cboe (prior session, inferred)"


def infer_oi_date(cal: TradingCalendar | None, payload_ts: datetime | None, as_of: datetime) -> str | None:
    """The most recent completed session before the payload's New York day (before today when the
    payload has no time). None without a calendar."""
    if cal is None:
        return None
    day = (payload_ts.astimezone(ZoneInfo("America/New_York")).date() if payload_ts is not None
           else ny_today(as_of))
    try:
        return cal.prev_session(cal.latest_session_on_or_before(day)).isoformat()
    except (ValueError, KeyError, IndexError):
        return None


def _date_counts(contracts: list[Contract]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in contracts:
        if c.oi is not None:
            k = c.oi_date or "none"
            out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def _alpaca_info(contracts: list[Contract], reason: str | None) -> dict:
    dates = [c.oi_date for c in contracts if c.oi is not None and c.oi_date]
    d = max(dates) if dates else None
    for c in contracts:
        c.oi_source = "alpaca"
    return {"source": "alpaca", "basis": "reported", "label": alpaca_label(d), "oi_date": d,
            "alpaca_oi_date": d, "fallback_reason": reason, "matched": 0, "total": len(contracts),
            "differ_frac": None, "payload_ts": None, "mixed": False,
            "oi_date_min": min(dates) if dates else None, "oi_dates": _date_counts(contracts)}


def alpaca_only(contracts: list[Contract], reason: str | None = None) -> dict:
    """OI provenance for a chain that stays on Alpaca OI (OI_SOURCE = alpaca, or a fallback)."""
    return _alpaca_info(contracts, reason)


def apply_oi(ticker: str, contracts: list[Contract], as_of: datetime, cal: TradingCalendar | None,
             hasher: Hasher | None = None, client: httpx.Client | None = None,
             fetch=None, respect_cooldown: bool = True, clock=None) -> dict:
    """Replace Alpaca open interest with Cboe's, joined by OCC symbol. Mutates the contracts and
    returns the OI provenance (source, basis, label, oi_date, counts, fallback_reason).

    Contracts Cboe does not list keep Alpaca OI (oi_source alpaca). Any failure leaves every
    contract on Alpaca OI with fallback_reason set; this function never raises for vendor trouble.
    """
    if not contracts:
        return _alpaca_info(contracts, None)
    alpaca_dates = [c.oi_date for c in contracts if c.oi is not None and c.oi_date]
    alpaca_date = max(alpaca_dates) if alpaca_dates else None
    try:
        if fetch is not None:
            body, _cached = fetch(ticker)
        else:
            # One end-to-end budget (lock wait plus fetch): the spot is already fetched, and a slow Cboe
            # must not age it past the gate.
            body, _cached = fetch_raw(ticker, client, max_age_s=config.CBOE_OI_CACHE_S,
                                      budget_s=config.CBOE_OI_BUDGET_S, respect_cooldown=respect_cooldown)
    except SourceError as e:
        return _alpaca_info(contracts, f"Cboe unavailable ({e})")
    try:
        cboe_oi, payload_ts = parse_oi(body)
    except ValueError as e:
        return _alpaca_info(contracts, f"Cboe payload unreadable ({e})")
    # Cboe publishes no OI date: it is inferred from the payload time. With no time, or a time in the
    # future, nothing can be inferred, so the payload is not used (day-1 audit).
    if payload_ts is None:
        return _alpaca_info(contracts, "Cboe payload has no timestamp, so its OI date cannot be inferred")
    # "Future" is judged on the acquisition clock (when the payload was received), never on the run's
    # start time: a payload generated during the fetch is healthy (day-1 verification).
    acquired = (clock or (lambda: datetime.now(UTC)))()
    if (payload_ts - acquired).total_seconds() > config.CLOCK_SKEW_S:
        return _alpaca_info(contracts, "Cboe payload time is in the future")
    matched = [c for c in contracts if c.symbol in cboe_oi]
    share = len(matched) / len(contracts)
    if share < config.CBOE_OI_MIN_MATCH:
        return _alpaca_info(contracts, f"Cboe lists {len(matched)} of {len(contracts)} contracts")
    both = [c for c in matched if c.oi is not None]
    differ = sum(1 for c in both if cboe_oi[c.symbol] != c.oi)
    differ_frac = differ / len(both) if both else None
    inferred = infer_oi_date(cal, payload_ts, as_of)
    # An older Cboe file is never used, in either branch: even one differing value from it (a zero, say)
    # would replace Alpaca's newer open interest (day-1 verification).
    if alpaca_date and inferred and inferred < alpaca_date:
        return _alpaca_info(contracts, f"Cboe OI (inferred {inferred}) is older than Alpaca's ({alpaca_date})")
    if differ_frac is not None and differ_frac <= config.CBOE_OI_SAME_MAX_FRAC and alpaca_date:
        basis, oi_date = "matches_alpaca", alpaca_date
        label = f"OI: Cboe (matches Alpaca, {_fmt_day(alpaca_date)})"
    else:
        basis, oi_date, label = "inferred", inferred, CBOE_INFERRED_LABEL
    if hasher is not None:
        hasher.add(body)                    # the OI is a valuation input: bind the payload in response_hash
    for c in contracts:
        if c.symbol in cboe_oi:
            # Provenance per contract (day-1 verification). A value equal to Alpaca's is Alpaca's vintage and
            # keeps ITS OWN Alpaca date. A value that differs, or a contract Alpaca left undated, is Cboe's
            # and takes Cboe's inferred date, so an older Cboe file can never borrow Alpaca's newer date.
            same = basis == "matches_alpaca" and c.oi is not None and c.oi_date and cboe_oi[c.symbol] == c.oi
            own_date = c.oi_date if same else inferred
            c.oi, c.oi_date, c.oi_basis, c.oi_source = cboe_oi[c.symbol], own_date, "reported", "cboe"
        else:
            c.oi_source = "alpaca"
    mixed = len(matched) < len(contracts)
    if mixed:   # contracts Cboe lacks keep Alpaca OI (and Alpaca's date): say so in the label
        label += f" + Alpaca on {len(contracts) - len(matched)} contracts"
    dates = [c.oi_date for c in contracts if c.oi is not None and c.oi_date]
    return {"source": "cboe", "basis": basis, "label": label, "oi_date": oi_date, "alpaca_oi_date": alpaca_date,
            "fallback_reason": None, "matched": len(matched), "total": len(contracts),
            "differ_frac": round(differ_frac, 4) if differ_frac is not None else None,
            "payload_ts": payload_ts.isoformat() if payload_ts is not None else None, "mixed": mixed,
            "oi_date_min": min(dates) if dates else None, "oi_dates": _date_counts(contracts)}
