"""SPX on Alpaca (2026-11-20): roots asked by name, spot from put-call parity, every free-feed IV solved."""
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from gexlab import config, service
from gexlab.sources import occ
from gexlab.engine.bsm import call_price, put_price
from gexlab.engine.calendar import time_to_expiry
from gexlab.models import Chain, Spot
from gexlab.sources import alpaca as A
from gexlab.sources.base import RateLimiter, SourceError

pytestmark = pytest.mark.alpaca_index

UTC = timezone.utc
AS_OF = datetime(2026, 10, 16, 14, 0, 30, tzinfo=UTC)      # Friday 10:00:30 ET, 3rd Friday: SPX monthly expired at the open
QUOTE_T = "2026-10-16T14:00:20Z"
QT = datetime(2026, 10, 16, 14, 0, 20, tzinfo=UTC)
S = 7150.0
TODAY, LATER = "2026-10-16", "2026-10-19"
STRIKES = [7100, 7125, 7150, 7175, 7200]


@pytest.fixture(autouse=True)
def _reset_state():
    A.FEED_STATE.update(options=None, options_checked=0.0, stocks=None)
    A._CONTRACT_CACHE.clear()
    yield
    A.FEED_STATE.update(options=None, options_checked=0.0, stocks=None)
    A._CONTRACT_CACHE.clear()


def sym(root, exp, typ, k):
    return f"{root}{exp[2:4]}{exp[5:7]}{exp[8:10]}{typ[0].upper()}{int(k * 1000):08d}"


def fair(k, typ, exp, settlement="PM", iv=0.15, spot=S):
    T = time_to_expiry(QT, date.fromisoformat(exp), settlement)
    return (call_price if typ == "call" else put_price)(spot, k, T, 0.0, iv)


def q(px, t=QUOTE_T, half=0.05):
    return {"latestQuote": {"bp": round(px - half, 2), "ap": round(px + half, 2), "t": t}}


def row(root, exp, typ, k):
    return {"symbol": sym(root, exp, typ, k), "expiration_date": exp, "strike_price": str(k), "type": typ,
            "root_symbol": root, "multiplier": "100", "open_interest": "500", "open_interest_date": "2026-10-15"}


ROWS = ([row("SPXW", e, t, k) for e in (TODAY, LATER) for t in ("call", "put") for k in STRIKES]
        + [row("SPX", TODAY, t, k) for t in ("call", "put") for k in STRIKES])


def spxw_snaps():
    return {sym("SPXW", e, t, k): q(fair(k, t, e)) for e in (TODAY, LATER) for t in ("call", "put") for k in STRIKES}


def spx_snaps():   # the expired AM monthly: yesterday's quotes, as Alpaca still serves them
    return {sym("SPX", TODAY, t, k): q(50.0, t="2026-10-15T19:59:59Z") for t in ("call", "put") for k in STRIKES}


def make_handler(opra_status=403, calls=None):
    def handler(req: httpx.Request) -> httpx.Response:
        p, qp = req.url.path, req.url.params
        assert "/v2/orders" not in p
        if calls is not None:
            calls.append((p, qp.get("feed")))
        if p == "/v2/stocks/SPY/trades/latest":
            return httpx.Response(200, json={"symbol": "SPY", "trade": {"p": 713.0, "t": "2026-10-16T14:00:25Z"}})
        if p == "/v2/options/contracts":
            assert qp["underlying_symbols"] == "SPX"
            return httpx.Response(200, json={"option_contracts": ROWS, "next_page_token": None})
        if p.startswith("/v1beta1/options/snapshots/"):
            if qp["feed"] == "opra" and opra_status != 200:
                return httpx.Response(opra_status, json={"message": "subscription does not permit"})
            root = p.rsplit("/", 1)[1]
            snaps = {"SPXW": spxw_snaps, "SPX": spx_snaps}[root]()
            return httpx.Response(200, json={"snapshots": snaps, "next_page_token": None})
        return httpx.Response(404, json={"message": "not mocked"})
    return handler


def chain(tmp_path, **kw):
    client = httpx.Client(transport=httpx.MockTransport(make_handler(**kw)))
    src = A.AlpacaSource(A.AlpacaCreds("k", "s", "paper"), client=client,
                         rate=RateLimiter(tmp_path / "rl.sqlite", cap=60, reserve=15))
    return src.fetch_chain("SPX", AS_OF, None)


def test_config_puts_spx_on_alpaca_and_keeps_ndx_on_cboe():
    assert "SPX" in config.ALPACA_INDEX and config.CBOE_TICKERS == {"NDX"}
    assert config.INDEX_TICKERS == {"SPX", "NDX"}


def test_free_feed_spot_comes_from_parity(tmp_path):
    ch = chain(tmp_path)
    assert ch.feed == "indicative"
    assert ch.spot.source == "alpaca_parity" and ch.spot.basis == "parity"
    assert abs(ch.spot.price - S) < 0.5
    assert ch.spot.ts == QT


def test_every_root_is_asked_for_by_name(tmp_path):
    calls = []
    chain(tmp_path, calls=calls)
    snap_paths = {p for p, _ in calls if "/snapshots/" in p}
    assert snap_paths == {"/v1beta1/options/snapshots/SPX", "/v1beta1/options/snapshots/SPXW"}


def test_free_feed_iv_is_solved_for_every_expiry(tmp_path):
    ch = chain(tmp_path)
    c = {x.symbol: x for x in ch.contracts}
    for e in (TODAY, LATER):
        k = c[sym("SPXW", e, "call", 7175)]
        assert k.invalid_reason is None and k.iv_source == "solved_mid" and abs(k.iv - 0.15) < 0.01
    assert any("solved from the free feed's quotes" in w for w in ch.warnings)


def test_monthly_root_settles_am(tmp_path):
    c = {x.symbol: x for x in chain(tmp_path).contracts}
    assert c[sym("SPX", TODAY, "put", 7150)].settlement == "AM"
    assert c[sym("SPXW", TODAY, "put", 7150)].settlement == "PM"


def test_opra_feed_needs_no_indicative_calls(tmp_path):
    calls = []
    ch = chain(tmp_path, opra_status=200, calls=calls)
    assert ch.feed == "opra" and abs(ch.spot.price - S) < 0.5
    assert all(f == "opra" for p, f in calls if "/snapshots/" in p)


def test_parity_skips_stale_quotes_and_expired_expiries():
    snaps = {**spx_snaps(), **spxw_snaps()}
    ps = A.parity_spot(snaps, AS_OF, None, max_age_s=120)
    assert ps["expiry"] == TODAY and ps["pairs"] == 5 and abs(ps["price"] - S) < 0.5
    old = AS_OF - timedelta(minutes=10)
    stale = {k: {"latestQuote": {**v["latestQuote"], "t": old.isoformat()}} for k, v in snaps.items()}
    assert A.parity_spot(stale, AS_OF, None, max_age_s=120) is None
    assert A.parity_spot(stale, AS_OF, None, max_age_s=None) is not None      # market closed: any age


def test_parity_moves_on_when_the_pairs_disagree():
    snaps = spxw_snaps()
    for k in (7100, 7125, 7150):        # today's pairs priced at a different spot: forwards spread 40 pt
        for t in ("call", "put"):
            snaps[sym("SPXW", TODAY, t, k)] = q(fair(k, t, TODAY, spot=S + 40))
    ps = A.parity_spot(snaps, AS_OF, None, max_age_s=120)
    assert ps["expiry"] == LATER and abs(ps["price"] - S) < 0.5


def test_no_pairs_is_a_clear_error(tmp_path):
    def h(req):
        if "/snapshots/" in req.url.path:
            return httpx.Response(200, json={"snapshots": {}, "next_page_token": None})
        return make_handler()(req)
    client = httpx.Client(transport=httpx.MockTransport(h))
    src = A.AlpacaSource(A.AlpacaCreds("k", "s", "paper"), client=client,
                         rate=RateLimiter(tmp_path / "rl.sqlite", cap=60, reserve=15))
    with pytest.raises(SourceError, match="parity"):
        src.fetch_chain("SPX", AS_OF, None)


def test_service_routes_spx_to_alpaca(monkeypatch):
    seen = {}

    class Stub:
        def __init__(self, **kw):
            pass

        def fetch_chain(self, ticker, now, cal, strict=False, oi_overlay=None):
            seen.update(ticker=ticker, overlay=oi_overlay)
            raise SourceError("stub")

    monkeypatch.setattr(service, "AlpacaSource", Stub)
    with pytest.raises(SourceError):
        service.fetch_chain("SPX", now=AS_OF, cal=object())
    assert seen == {"ticker": "SPX", "overlay": occ.apply_oi}  # OCC open interest, never Cboe's


def test_live_spot_layer_stays_off_for_a_parity_spot():
    livespot = pytest.importorskip("gexlab.livespot")          # private edition only
    ch = Chain(ticker="SPX", as_of=AS_OF, spot=Spot(S, QT, "alpaca_parity", basis="parity"), feed="opra",
               contracts=[], oi_date=None, complete=True, raw_hash="x")
    assert livespot.estimate(ch, AS_OF, None, True) is None


def test_index_prices_stay_whole_points():
    assert service.fmt_spot(7150.4, "SPX") == "7,150"
