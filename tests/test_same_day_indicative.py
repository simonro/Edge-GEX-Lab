"""Issue 29: on the indicative feed Alpaca gives no IV for same-day expiries; the app solves it from the mid."""
from datetime import date, datetime, timezone

import httpx
import pytest

from gexlab.engine.bsm import call_price, put_price
from gexlab.engine.calendar import time_to_expiry
from gexlab.sources import alpaca as A
from gexlab.sources.base import RateLimiter

UTC = timezone.utc
AS_OF = datetime(2026, 10, 16, 14, 0, 30, tzinfo=UTC)      # Friday 10:00:30 ET, market open
QUOTE_T = "2026-10-16T14:00:00Z"
TODAY, LATER = "2026-10-16", "2026-10-19"


@pytest.fixture(autouse=True)
def _reset_state():
    A.FEED_STATE.update(options=None, options_checked=0.0, stocks=None)
    A._CONTRACT_CACHE.clear()
    yield
    A.FEED_STATE.update(options=None, options_checked=0.0, stocks=None)
    A._CONTRACT_CACHE.clear()


def _row(sym, exp, k, typ):
    return {"symbol": sym, "expiration_date": exp, "strike_price": str(k), "type": typ, "root_symbol": "SPY",
            "multiplier": "100", "open_interest": "1000", "open_interest_date": "2026-10-15"}


ROWS = [_row("SPY261016C00100500", TODAY, 100.5, "call"), _row("SPY261016P00099500", TODAY, 99.5, "put"),
        _row("SPY261016C00115000", TODAY, 115, "call"), _row("SPY261019C00101000", LATER, 101, "call")]


def _fair(k, typ, exp, iv):
    T = time_to_expiry(datetime(2026, 10, 16, 14, 0, 0, tzinfo=UTC), date.fromisoformat(exp), "PM")
    return (call_price if typ == "call" else put_price)(100.0, k, T, 0.0, iv)


def _q(px, half=0.002):
    return {"bp": round(px - half, 4), "ap": round(px + half, 4), "t": QUOTE_T}


def handler(req: httpx.Request) -> httpx.Response:
    p, q = req.url.path, req.url.params
    assert "/v2/orders" not in p
    if p == "/v2/stocks/SPY/trades/latest":
        return httpx.Response(200, json={"symbol": "SPY", "trade": {"p": 100.0, "t": "2026-10-16T14:00:05Z"}})
    if p == "/v2/options/contracts":
        return httpx.Response(200, json={"option_contracts": ROWS, "next_page_token": None})
    if p == "/v1beta1/options/snapshots/SPY":
        if q["feed"] == "opra":
            return httpx.Response(403, json={"message": "subscription does not permit"})
        snaps = {   # same-day contracts carry a quote but no impliedVolatility, as Alpaca serves them
            "SPY261016C00100500": {"latestQuote": _q(_fair(100.5, "call", TODAY, 0.18))},
            "SPY261016P00099500": {"latestQuote": _q(_fair(99.5, "put", TODAY, 0.22))},
            "SPY261016C00115000": {"latestQuote": {"bp": 0.0, "ap": 0.01, "t": QUOTE_T}},   # far OTM, no bid
            "SPY261019C00101000": {"impliedVolatility": 0.19, "latestQuote": _q(_fair(101, "call", LATER, 0.19))},
        }
        return httpx.Response(200, json={"snapshots": snaps, "next_page_token": None})
    return httpx.Response(404, json={"message": "not mocked"})


def _chain(tmp_path, h=handler):
    client = httpx.Client(transport=httpx.MockTransport(h))
    src = A.AlpacaSource(A.AlpacaCreds("k", "s", "paper"), client=client,
                         rate=RateLimiter(tmp_path / "rl.sqlite", cap=60, reserve=15))
    return src.fetch_chain("SPY", AS_OF, None)


def test_same_day_iv_is_solved_from_the_indicative_mid(tmp_path):
    ch = _chain(tmp_path)
    assert ch.feed == "indicative"
    c = {x.symbol: x for x in ch.contracts}
    call, put = c["SPY261016C00100500"], c["SPY261016P00099500"]
    assert call.invalid_reason is None and call.iv_source == "solved_mid" and abs(call.iv - 0.18) < 0.01
    assert put.invalid_reason is None and put.iv_source == "solved_mid" and abs(put.iv - 0.22) < 0.01
    assert any("expiring today use an IV solved from the indicative quote" in w for w in ch.warnings)


def test_unusable_same_day_quote_stays_invalid(tmp_path):
    far = {x.symbol: x for x in _chain(tmp_path).contracts}["SPY261016C00115000"]
    assert far.iv is None and far.invalid_reason == "mid_unusable"


def test_later_expiries_keep_alpacas_own_iv(tmp_path):
    later = {x.symbol: x for x in _chain(tmp_path).contracts}["SPY261019C00101000"]
    assert later.iv_source == "indicative" and later.iv == 0.19


def test_quote_far_from_the_spot_time_is_not_solved(tmp_path):
    def stale(req):
        r = handler(req)
        if req.url.path == "/v1beta1/options/snapshots/SPY" and req.url.params["feed"] == "indicative":
            j = r.json()
            for s in j["snapshots"].values():
                s["latestQuote"]["t"] = "2026-10-16T13:50:00Z"      # 10 minutes before the spot trade
            return httpx.Response(200, json=j)
        return r
    call = {x.symbol: x for x in _chain(tmp_path, stale).contracts}["SPY261016C00100500"]
    assert call.iv is None and call.invalid_reason == "quote_spot_misaligned"
