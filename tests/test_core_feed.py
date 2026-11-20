"""Feed detection and per-contract IV fallback, with mocked HTTP (no network)."""
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from gexlab.engine.bsm import call_price
from gexlab.engine.calendar import time_to_expiry
from gexlab.sources import alpaca as A
from gexlab.sources.base import RateLimiter, SourceError

UTC = timezone.utc
AS_OF = datetime(2026, 10, 16, 14, 0, 30, tzinfo=UTC)
QUOTE_T = "2026-10-16T14:00:00.123456789Z"


@pytest.fixture(autouse=True)
def _reset_state():
    A.FEED_STATE.update(options=None, options_checked=0.0, stocks=None)
    A._CONTRACT_CACHE.clear()
    yield
    A.FEED_STATE.update(options=None, options_checked=0.0, stocks=None)
    A._CONTRACT_CACHE.clear()


def _fair_call():
    T = time_to_expiry(datetime(2026, 10, 16, 14, 0, 0, 123456, tzinfo=UTC), date(2026, 10, 19), "PM")
    return call_price(100.0, 100.0, T, 0.0, 0.20)


CONTRACTS = [
    {"symbol": "SPY261019C00100000", "expiration_date": "2026-10-19", "strike_price": "100", "type": "call",
     "root_symbol": "SPY", "multiplier": "100", "open_interest": "1200", "open_interest_date": "2026-10-15"},
    {"symbol": "SPY261019P00100000", "expiration_date": "2026-10-19", "strike_price": "100", "type": "put",
     "root_symbol": "SPY", "multiplier": "100", "open_interest": "900", "open_interest_date": "2026-10-15"},
    {"symbol": "SPY261019C00105000", "expiration_date": "2026-10-19", "strike_price": "105", "type": "call",
     "root_symbol": "SPY", "multiplier": "100", "open_interest": None, "open_interest_date": None},
    {"symbol": "SPY261019C00110000", "expiration_date": "2026-10-19", "strike_price": "110", "type": "call",
     "root_symbol": "SPY", "multiplier": "100", "open_interest": "50", "open_interest_date": "2026-10-15"},
]


def make_handler(opra_status=200, sip_status=200, calls=None):
    fair = _fair_call()

    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((req.url.path, dict(req.url.params)))
        assert req.headers["APCA-API-KEY-ID"] == "k"
        assert "/v2/orders" not in req.url.path        # never an order endpoint
        p, q = req.url.path, req.url.params
        if p == "/v2/stocks/SPY/trades/latest":
            if q["feed"] == "sip" and sip_status != 200:
                return httpx.Response(sip_status, json={"message": "subscription does not permit"})
            return httpx.Response(200, json={"symbol": "SPY", "trade": {"p": 100.0, "t": "2026-10-16T14:00:29Z"}})
        if p == "/v2/stocks/SPY/bars":           # market closed: the official session close
            day = q["start"][:10]
            return httpx.Response(200, json={"bars": [{"t": f"{day}T04:00:00Z", "c": 100.0}], "symbol": "SPY"})
        if p == "/v2/options/contracts":
            return httpx.Response(200, json={"option_contracts": CONTRACTS, "next_page_token": None})
        if p == "/v1beta1/options/snapshots/SPY":
            if q["feed"] == "opra":
                if opra_status != 200:
                    return httpx.Response(opra_status, json={"message": "forbidden"})
                snaps = {
                    "SPY261019C00100000": {"latestQuote": {"bp": round(fair - 0.02, 4), "ap": round(fair + 0.02, 4), "t": QUOTE_T}},
                    "SPY261019P00100000": {"latestQuote": {"bp": 0.10, "ap": 3.00, "t": QUOTE_T}},   # wide
                    "SPY261019C00105000": {"latestQuote": {"bp": 0.0, "ap": 0.05, "t": QUOTE_T}},    # no bid
                }
                # second page exercises pagination
                if "page_token" in q:
                    return httpx.Response(200, json={"snapshots": {"SPY261019C00110000": {
                        "latestQuote": {"bp": 0.0, "ap": 0.01, "t": QUOTE_T}}}, "next_page_token": None})
                return httpx.Response(200, json={"snapshots": snaps, "next_page_token": "p2"})
            ind = {s["symbol"]: {"impliedVolatility": 0.25, "greeks": {"gamma": 0.1},
                                 "latestQuote": {"bp": 1.0, "ap": 1.1, "t": QUOTE_T}} for s in CONTRACTS}
            ind["SPY261019C00110000"] = {"latestQuote": {"bp": 0, "ap": 0.01, "t": QUOTE_T}}  # no IV anywhere
            return httpx.Response(200, json={"snapshots": ind, "next_page_token": None})
        return httpx.Response(404, json={"message": "not mocked"})
    return handler


def _source(tmp_path, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    rate = RateLimiter(tmp_path / "rl.sqlite", cap=60, reserve=15)
    return A.AlpacaSource(A.AlpacaCreds("k", "s", "paper"), client=client, rate=rate)


def test_opra_mid_solve_with_per_contract_indicative_fallback(tmp_path):
    calls = []
    ch = _source(tmp_path, make_handler(calls=calls)).fetch_chain("SPY", AS_OF, None)
    assert ch.feed == "opra"
    c = {x.symbol: x for x in ch.contracts}
    atm = c["SPY261019C00100000"]
    assert atm.iv_source == "solved_mid" and abs(atm.iv - 0.20) < 2e-3
    # The put's own mid is wide; at K = spot the call is the OTM member, so the put shares its IV.
    assert c["SPY261019P00100000"].iv_source == "solved_mid" and c["SPY261019P00100000"].iv == atm.iv
    assert c["SPY261019C00105000"].iv_source == "indicative"
    # Alpaca's null OI on a series that never traded is a certain zero (decode_null_oi), not unknown.
    assert c["SPY261019C00105000"].oi == 0 and c["SPY261019C00105000"].oi_basis == "zero_never_traded"
    assert c["SPY261019C00110000"].iv is None and c["SPY261019C00110000"].invalid_reason == "mid_unusable"
    assert ch.oi_date == "2026-10-15"
    assert ch.raw_hash and len(ch.raw_hash) == 64
    feeds = [p.get("feed") for path, p in calls if "snapshots" in path]
    assert feeds.count("opra") == 2 and "indicative" in feeds     # two OPRA pages, one fallback fetch
    assert A.FEED_STATE["options"] == "opra"


def test_opra_403_falls_back_to_indicative_only(tmp_path):
    ch = _source(tmp_path, make_handler(opra_status=403)).fetch_chain("SPY", AS_OF, None)
    assert ch.feed == "indicative"
    assert all(x.iv_source == "indicative" for x in ch.contracts if x.iv is not None)
    assert A.FEED_STATE["options"] == "indicative"


def test_feed_detection_cached_after_first_run(tmp_path):
    src = _source(tmp_path, make_handler(opra_status=403))
    src.fetch_chain("SPY", AS_OF, None)
    calls = []
    src.client = httpx.Client(transport=httpx.MockTransport(make_handler(opra_status=403, calls=calls)))
    src.fetch_chain("SPY", AS_OF, None)
    assert not any(p.get("feed") == "opra" for _, p in calls)      # not re-probed within the hour


def test_stock_spot_sip_403_falls_back_to_iex(tmp_path):
    ch = _source(tmp_path, make_handler(sip_status=403)).fetch_chain("SPY", AS_OF, None)
    assert ch.spot.source == "alpaca_iex" and ch.spot.price == 100.0


def test_401_raises_without_leaking_keys(tmp_path):
    src = _source(tmp_path, lambda req: httpx.Response(401, json={"message": "unauthorized."}))
    with pytest.raises(SourceError) as e:
        src.fetch_chain("SPY", AS_OF, None)
    assert e.value.status == 401
    assert "k" not in repr(src.creds).replace("key=<hidden>", "")


def test_mid_usable_rules():
    assert A.mid_usable(1.0, 1.1)
    assert not A.mid_usable(0.0, 0.05)       # no bid
    assert not A.mid_usable(1.2, 1.1)        # crossed
    assert not A.mid_usable(0.1, 3.0)        # wide
    assert not A.mid_usable(None, 1.0)


def test_parse_nanosecond_timestamp():
    assert A.parse_ts(QUOTE_T) == datetime(2026, 10, 16, 14, 0, 0, 123456, tzinfo=UTC)


def test_rate_limiter_priority_and_cap(tmp_path):
    now = [1000.0]
    slept = []
    rl = RateLimiter(tmp_path / "rl.sqlite", cap=3, reserve=1, clock=lambda: now[0],
                     sleep=lambda s: slept.append(s))
    rl.acquire("low")
    rl.acquire("low")
    with pytest.raises(SourceError):            # low priority stops at cap - reserve = 2
        rl.acquire("low", max_wait_s=1)
    rl.acquire("high")                          # recorder still has its reserved slot
    assert rl.used_last_min() == 3
    now[0] += 61
    rl.acquire("low")                           # window rolled
    assert rl.used_last_min() == 1


def test_credentials_from_env_file(tmp_path, monkeypatch):
    f = tmp_path / "x.env"
    f.write_text("ALPACA_TRADING_KEY=abc\nALPACA_TRADING_SECRET=def\nALPACA_MODE=paper\n")
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.setenv("ALPACA_ENV_FILE", str(f))
    c = A.load_credentials()
    assert c.key == "abc" and c.trading_base == "https://paper-api.alpaca.markets"
    assert "abc" not in repr(c)
    monkeypatch.setenv("APCA_API_KEY_ID", "envk")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "envs")
    assert A.load_credentials().key == "envk"


def test_no_keys_message_points_at_env(tmp_path, monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.setenv("ALPACA_ENV_FILE", str(tmp_path / "missing.env"))
    assert A.load_credentials() is None
    with pytest.raises(SourceError, match=r"APCA_API_KEY_ID and APCA_API_SECRET_KEY in .env .*free Alpaca plan works"):
        A.AlpacaSource()


def test_pair_iv_from_otm():
    from gexlab.models import Contract
    d = date(2026, 10, 19)
    mk = lambda k, t, iv, src: Contract(symbol=f"{k}{t}", root="SPY", expiry=d, settlement="PM", strike=k,
                                        type=t, oi=10, iv=iv, iv_source=src)
    cs = [mk(95.0, "call", None, None), mk(95.0, "put", 0.30, "solved_mid"),      # OTM put feeds ITM call
          mk(105.0, "call", 0.22, "solved_mid"), mk(105.0, "put", 0.50, "indicative"),  # OTM call wins
          mk(110.0, "call", None, None), mk(110.0, "put", 0.40, "indicative")]     # OTM missing: ITM backup
    assert A.pair_iv_from_otm(cs, 100.0) == 3
    assert cs[0].iv == 0.30 and cs[3].iv == 0.22 and cs[3].iv_source == "solved_mid"
    assert cs[4].iv == 0.40 and cs[4].iv_source == "indicative"
