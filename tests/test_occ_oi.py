"""OCC open interest (2026-11-20): parse the series file, join by OCC symbol, date it, fall back cleanly."""
from datetime import date, datetime, timezone

import httpx
import pytest

from gexlab import config
from gexlab.engine.calendar import NY, TradingCalendar
from gexlab.models import Contract
from gexlab.sources import occ

UTC = timezone.utc
CAL = TradingCalendar.weekdays(date(2026, 10, 1), date(2026, 12, 31))
AS_OF = datetime(2026, 11, 20, 11, 30, tzinfo=NY).astimezone(UTC)

FILE = """Series Search Results for SPX

Products for this underlying symbol are traded on:
CBOE

\t\tSeries/contract\t\tStrike\t\t\tOpen Interest\t\t\t
ProductSymbol\tyear\tMonth\tDay\tInteger\tDec\tC/P\tCall\tPut\tPosition Limit\t
SPX   \t\t2026\t11\t20\t7150\t000\tC P \t991\t4520\t0
SPXW  \t\t2026\t11\t20\t7150\t000\tC P \t246\t3527\t0
SPXW  \t\t2026\t11\t23\t7150\t000\tC P \t52\t1983\t0
4SPX  \t\t2026\t11\t19\t6538\t760\tC   \t0\t0\t0
"""


def contract(sym, oi, oi_date="2026-11-18"):
    from gexlab.models import parse_occ
    o = parse_occ(sym)
    return Contract(symbol=sym, root=o.root, expiry=o.expiry, settlement="PM", strike=o.strike, type=o.type,
                    oi=oi, oi_date=oi_date)


def chain(alpaca_oi=(2449, 1519)):
    return [contract("SPXW261120P07150000", alpaca_oi[0]), contract("SPXW261123P07150000", alpaca_oi[1]),
            contract("SPX261120C07150000", 933)]


def fetch_at(t):
    return lambda ticker: (FILE, t)


def test_parse_series_builds_occ_symbols():
    m = occ.parse_series(FILE)
    assert m["SPXW261120P07150000"] == 3527 and m["SPXW261120C07150000"] == 246
    assert m["SPX261120P07150000"] == 4520 and m["SPXW261123P07150000"] == 1983
    assert m["4SPX261119C06538760"] == 0 and "4SPX261119P06538760" not in m


def test_newer_file_replaces_alpaca_and_is_dated_the_prior_session():
    cs = chain()
    info = occ.apply_oi("SPX", cs, AS_OF, CAL, fetch=fetch_at(AS_OF))
    assert info["source"] == "occ" and info["basis"] == "inferred" and info["oi_date"] == "2026-11-19"
    assert [c.oi for c in cs] == [3527, 1983, 991] and all(c.oi_source == "occ" for c in cs)
    assert all(c.oi_date == "2026-11-19" for c in cs)
    assert info["label"] == "OI: OCC (Nov 19, prior session)"


def test_same_numbers_keep_alpacas_date():
    cs = [contract("SPXW261120P07150000", 3527, "2026-11-19"), contract("SPXW261123P07150000", 1983, "2026-11-19"),
          contract("SPX261120C07150000", 991, "2026-11-19")]
    info = occ.apply_oi("SPX", cs, AS_OF, CAL, fetch=fetch_at(AS_OF))
    assert info["basis"] == "matches_alpaca" and info["oi_date"] == "2026-11-19"


def test_early_download_is_dated_one_session_earlier():
    early = datetime(2026, 11, 20, 6, 0, tzinfo=NY).astimezone(UTC)
    assert occ.oi_date_for(CAL, early, AS_OF) == "2026-11-18"
    assert occ.oi_date_for(CAL, AS_OF, AS_OF) == "2026-11-19"


def test_too_few_matches_keeps_alpaca():
    cs = chain() + [contract(f"SPXW261127P0{7000 + i}000", 10) for i in range(20)]
    info = occ.apply_oi("SPX", cs, AS_OF, CAL, fetch=fetch_at(AS_OF))
    assert info["source"] == "alpaca" and "OCC lists 3 of 23" in info["fallback_reason"]


def test_failure_keeps_alpaca_with_the_reason():
    def boom(ticker):
        raise RuntimeError("OCC HTTP 503")
    info = occ.apply_oi("SPX", chain(), AS_OF, CAL, fetch=boom)
    assert info["source"] == "alpaca" and "OCC HTTP 503" in info["fallback_reason"]


def test_fetch_caches_and_identifies_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    occ._FAILED.clear()
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, text=FILE)
    client = httpx.Client(transport=httpx.MockTransport(h))
    now = AS_OF.timestamp()
    text, t = occ.fetch_series("SPX", client, now=now)
    assert "Series Search Results" in text and len(seen) == 1
    assert seen[0].url.params["symbol"] == "SPX" and seen[0].headers["user-agent"].startswith("edge-gex-lab/")
    occ.fetch_series("SPX", client, now=now + 60)
    assert len(seen) == 1                       # reused from disk within OCC_CACHE_S


def test_fetch_rejects_a_non_series_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    occ._FAILED.clear()
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>blocked</html>")))
    with pytest.raises(RuntimeError, match="something other"):
        occ.fetch_series("SPX", client, now=AS_OF.timestamp())
