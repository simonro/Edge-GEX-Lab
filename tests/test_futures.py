"""Futures view (display transform only): parity basis, contract and roll, ETF ratio, override, tick rounding.

Every chain here is synthetic: Black-Scholes prices with a stated r and dividend yield, so the true forward
S * e^((r - q) T) is known exactly and the parity estimate can be checked against it.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from gexlab import config, futures as F
from gexlab.engine import bsm
from gexlab.engine.calendar import NY, TradingCalendar, time_to_expiry
from gexlab.models import Spot
from gexlab.sources.base import SourceError

UTC = timezone.utc
CAL = TradingCalendar.weekdays(date(2026, 9, 1), date(2027, 7, 31))
NOW = datetime(2026, 10, 21, 11, 59, 33, tzinfo=NY).astimezone(UTC)   # market open, Wed
DEC = date(2026, 12, 18)
R, Q, IV = 0.04, 0.012, 0.16


def _pairs(spot, T, strikes, r=R, q=Q, half=0.10, iv=IV):
    out = {}
    for k in strikes:
        c = bsm.call_price(spot, k, T, r, iv, q)
        p = bsm.put_price(spot, k, T, r, iv, q)
        out[k] = {"call": (c - half, c + half), "put": (p - half, p + half)}
    return out


def _payload(spot=7000.0, expiry=DEC, root="SPX", ts=NOW, index_time=None, bid_zero=False, strikes=None,
             index_time_missing=False):
    T = time_to_expiry(ts, expiry, "AM")
    strikes = strikes if strikes is not None else [spot + 5 * i for i in range(-12, 13)]
    opts = []
    for k, q in _pairs(spot, T, strikes).items():
        for cp, (b, a) in (("C", q["call"]), ("P", q["put"])):
            opts.append({"option": f"{root}{expiry:%y%m%d}{cp}{int(round(k * 1000)):08d}",
                         "bid": 0.0 if bid_zero else round(b, 4), "ask": 0.0 if bid_zero else round(a, 4)})
    # a weekly root on the same date must be ignored (PM settled SPXW is not the futures settlement)
    opts.append({"option": f"SPXW{expiry:%y%m%d}C{int(spot * 1000):08d}", "bid": 1.0, "ask": 999.0})
    it = index_time or (ts - timedelta(minutes=15))
    data = {"current_price": spot, "options": opts}
    if not index_time_missing:
        data["last_trade_time"] = it.astimezone(NY).strftime("%Y-%m-%dT%H:%M:%S")
    return json.dumps({"timestamp": ts.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"), "data": data}).encode()


def _true_forward(spot, ts=NOW, expiry=DEC):
    T = time_to_expiry(ts, expiry, "AM")
    return spot * math.exp((R - Q) * T)


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    F._PARSED.clear()
    F._RATIO_CACHE.clear()
    F._BASIS_HIST.clear()
    monkeypatch.setattr(config, "FUTURES_RATE", R)       # the synthetic chain's r


# ----------------------------------------------------------------------------- parity math

def test_parity_recovers_known_forward():
    T = time_to_expiry(NOW, DEC, "AM")
    spot = 7000.0
    par = F.parity_forward(_pairs(spot, T, [spot + 5 * i for i in range(-12, 13)]), spot, T)
    assert par.reason is None and par.usable == 25
    assert par.forward == pytest.approx(_true_forward(spot), abs=1e-6)
    # pass 2 picks the 6 strikes nearest the forward, which sits above spot (r > q)
    used = [s["strike"] for s in par.strikes]
    assert len(used) == config.FUTURES_PARITY_STRIKES and used == sorted(used)
    assert all(abs(k - par.forward) <= 15 for k in used)


def test_parity_is_insensitive_to_r_near_the_forward():
    T = time_to_expiry(NOW, DEC, "AM")
    pairs = _pairs(7000.0, T, [7000.0 + 5 * i for i in range(-12, 13)])
    a = F.parity_forward(pairs, 7000.0, T, r=R).forward
    b = F.parity_forward(pairs, 7000.0, T, r=0.0).forward
    assert abs(a - b) < 0.05                               # strikes near F: C - P is small, e^(rT) barely matters


def test_parity_median_shrugs_off_one_bad_quote():
    T = time_to_expiry(NOW, DEC, "AM")
    pairs = _pairs(7000.0, T, [7000.0 + 5 * i for i in range(-12, 13)])
    k = min(pairs, key=lambda x: abs(x - _true_forward(7000.0)))
    c = pairs[k]["call"]
    pairs[k]["call"] = (c[0] + 8, c[1] + 8)                # one stale call mid, still a usable spread
    assert F.parity_forward(pairs, 7000.0, T).forward == pytest.approx(_true_forward(7000.0), abs=1e-6)


def test_too_few_usable_quotes_is_unavailable_never_a_guess():
    T = time_to_expiry(NOW, DEC, "AM")
    pairs = _pairs(7000.0, T, [6995.0, 7000.0, 7005.0, 7010.0])
    for k in (6995.0, 7000.0):
        pairs[k]["put"] = (0.0, 0.0)                      # no bid: not usable
    pairs[7005.0]["call"] = (1.0, 50.0)                   # spread over 50% of mid: not usable
    par = F.parity_forward(pairs, 7000.0, T)
    assert par.forward is None and par.usable == 1
    assert par.reason == "only 1 strike with usable call and put quotes (needs 3)"
    assert F.parity_forward(pairs, 7000.0, 0.0).reason == "the quarterly options have expired"


def test_build_with_no_quotes_reports_unavailable():
    out = F.build("SPX", now=NOW, cal=CAL, market_open=True, index_body=_payload(bid_zero=True))
    assert out["available"] is False and out["basis"] is None and out["convert"] is None
    assert out["parity"]["forward"] is None and out["parity"]["basis"] is None
    assert "only 0 strikes" in out["reason"] and "Type your /ES price" in out["reason"]


def test_build_spx_basis_from_the_same_payload():
    out = F.build("SPX", now=NOW, cal=CAL, market_open=True, index_body=_payload(spot=7000.0))
    assert out["available"] and out["index"] == "SPX" and out["etf"] is None
    p = out["parity"]
    assert p["index_spot"] == 7000.0 and p["option_root"] == "SPX" and p["option_expiry"] == "2026-12-18"
    assert p["basis"] == pytest.approx(_true_forward(7000.0) - 7000.0, abs=1e-3)
    assert out["basis"] == {"value": p["basis"], "source": "parity", "label": "parity estimate"}
    assert out["convert"] == {"ratio": 1.0, "basis": p["basis"], "tick": 0.25}
    assert out["contract"]["symbol"] == "/ESZ26" and out["contract"]["also"] == ["/MESZ26"]
    assert out["label"].startswith("Estimate")


def test_quarterly_parse_ignores_weekly_root_and_other_months():
    p = F.parse_payload("SPX", _payload())
    assert set(p.pairs_by_expiry) == {DEC}                # only root SPX, only Mar/Jun/Sep/Dec
    assert all(len(v) == 2 for v in p.pairs_by_expiry[DEC].values())


def test_basis_is_the_median_over_recent_payloads():
    c = F.front_contract("SPX", date(2026, 10, 21))
    t0 = NOW
    assert F.smoothed_basis(c, t0, 40.0) == {"basis": 40.0, "basis_window": {"payloads": 1, "seconds": 0}}
    F.smoothed_basis(c, t0 + timedelta(seconds=60), 48.0)
    out = F.smoothed_basis(c, t0 + timedelta(seconds=120), 41.0)
    assert out == {"basis": 41.0, "basis_window": {"payloads": 3, "seconds": 120}}
    # the same payload seen twice counts once
    assert F.smoothed_basis(c, t0 + timedelta(seconds=120), 41.0)["basis_window"]["payloads"] == 3
    # older than the window drops out
    late = t0 + timedelta(seconds=config.FUTURES_BASIS_WINDOW_S + 61)
    assert F.smoothed_basis(c, late, 45.0) == {"basis": 43.0, "basis_window": {"payloads": 2, "seconds": 241}}
    # no payload time: that payload alone
    assert F.smoothed_basis(c, None, 39.0)["basis"] == 39.0


def test_build_reports_raw_and_window_basis():
    a = F.build("SPX", now=NOW, cal=CAL, market_open=True, index_body=_payload(spot=7000.0))
    later = NOW + timedelta(seconds=60)
    b = F.build("SPX", now=later, cal=CAL, market_open=True, index_body=_payload(spot=7010.0, ts=later))
    pb = b["parity"]
    assert pb["basis_window"] == {"payloads": 2, "seconds": 60}
    assert pb["basis"] == pytest.approx((a["parity"]["basis_raw"] + pb["basis_raw"]) / 2, abs=1e-4)
    assert b["convert"]["basis"] == pb["basis"]


# ----------------------------------------------------------------------------- contract and roll

@pytest.mark.parametrize("today, symbol", [
    (date(2026, 10, 21), "/ESZ26"),
    (date(2026, 12, 10), "/ESZ26"),    # the old Thursday roll: still December
    (date(2026, 12, 11), "/ESZ26"),    # the trading day before the roll date
    (date(2026, 12, 14), "/ESH27"),    # roll date (Mon, 4 days before Fri Dec 18): already the next quarter
    (date(2026, 12, 18), "/ESH27"),    # expiry day
    (date(2026, 12, 21), "/ESH27"),
    (date(2027, 3, 14), "/ESH27"),     # third Friday Mar 19 2027, roll Mon Mar 15
    (date(2027, 3, 15), "/ESM27"),
    (date(2027, 9, 8), "/ESU27"),
])
def test_front_contract_and_roll(today, symbol):
    c = F.front_contract("SPX", today)
    assert c.symbol == symbol
    assert c.roll_date.weekday() == 0 and (c.expiry - c.roll_date).days == 4 and c.expiry.weekday() == 4
    assert today < c.roll_date


def test_roll_dates_and_ndx_symbols():
    z = F.front_contract("SPX", date(2026, 10, 21))
    assert (z.expiry, z.roll_date) == (date(2026, 12, 18), date(2026, 12, 14))
    n = F.front_contract("NDX", date(2026, 10, 21))
    assert n.symbol == "/MNQZ26" and n.also == ("/NQZ26",)
    assert F.next_contract(z).symbol == "/ESH27" and F.next_contract(F.contract_for("SPX", 2027, 12)).symbol == "/ESH28"
    j = z.to_json()
    assert j["code"] == "Z26" and j["label"] == "Dec 2026" and j["roll_date"] == "2026-12-14"


# CME's customary U.S. equity index roll dates, copied from cmegroup.com/trading/equity-index/rolldates.html
# (read 2026-10-25). Jun 2026: Juneteenth moves the expiry to Thu Jun 18, the roll stays Mon Jun 15.
CME_ROLL_DATES = [
    (2025, 3, date(2025, 3, 17)), (2025, 6, date(2025, 6, 16)), (2025, 9, date(2025, 9, 15)),
    (2025, 12, date(2025, 12, 15)), (2026, 3, date(2026, 3, 16)), (2026, 6, date(2026, 6, 15)),
    (2026, 9, date(2026, 9, 14)), (2026, 12, date(2026, 12, 14)), (2027, 3, date(2027, 3, 15)),
    (2027, 6, date(2027, 6, 14)), (2027, 9, date(2027, 9, 13)), (2027, 12, date(2027, 12, 13)),
    (2028, 3, date(2028, 3, 13)), (2028, 6, date(2028, 6, 12)), (2028, 9, date(2028, 9, 11)),
    (2028, 12, date(2028, 12, 11)),
]


@pytest.mark.parametrize("year, month, cme_roll", CME_ROLL_DATES)
def test_roll_date_matches_cme_table(year, month, cme_roll):
    assert F.contract_for("SPX", year, month).roll_date == cme_roll
    assert F.contract_for("NDX", year, month).roll_date == cme_roll


def test_holiday_friday_uses_thursday_am_options():
    # June 2027: Juneteenth falls on the third Friday, so the AM options expire Thu Jun 17
    c = F.front_contract("SPX", date(2027, 5, 3))
    assert c.symbol == "/ESM27" and c.expiry == date(2027, 6, 18)
    now = datetime(2027, 5, 3, 11, 0, tzinfo=NY).astimezone(UTC)
    out = F.build("SPX", now=now, cal=CAL, market_open=True,
                  index_body=_payload(spot=7000.0, expiry=date(2027, 6, 17), ts=now))
    assert out["available"] and out["parity"]["option_expiry"] == "2027-06-17"


def test_missing_quarterly_expiry_is_unavailable():
    out = F.build("SPX", now=NOW, cal=CAL, market_open=True, index_body=_payload(expiry=date(2027, 3, 19)))
    assert not out["available"] and "lists no SPX AM options for Dec 18" in out["parity"]["reason"]


# ----------------------------------------------------------------------------- ETF ratio

def _trade_at(price, calls=None):
    def f(sym, at, lookback_s):
        if calls is not None:
            calls.append((sym, at, lookback_s))
        return Spot(price, at - timedelta(seconds=2), "alpaca_sip")
    return f


def test_spy_ratio_priced_at_the_index_time():
    calls = []
    it = datetime(2026, 10, 21, 11, 44, 31, tzinfo=NY).astimezone(UTC)
    out = F.build("SPY", now=NOW, cal=CAL, market_open=True, index_body=_payload(spot=7000.0, index_time=it),
                  trade_at=_trade_at(700.5, calls))
    assert out["available"] and out["etf"] == "SPY" and out["index"] == "SPX"
    assert calls == [("SPY", it, config.FUTURES_RATIO_MAX_LAG_S)]
    rb = out["ratio"]
    assert rb["value"] == pytest.approx(7000.0 / 700.5) and rb["lag_s"] == 2.0 and rb["etf_price"] == 700.5
    assert out["convert"]["ratio"] == rb["value"]
    # an ETF level converts through the ratio, then the basis, then the tick
    b = out["basis"]["value"]
    lvl = 702.35
    assert F.to_futures(lvl, b, rb["value"]) == F.round_tick(lvl * 7000.0 / 700.5 + b)
    assert F.to_futures(700.5, b, rb["value"]) == F.round_tick(7000.0 + b)


def test_etf_without_keys_or_trade_is_unavailable():
    out = F.build("QQQ", now=NOW, cal=CAL, market_open=True, index_body=_payload(root="NDX", spot=25000.0))
    assert not out["available"] and out["reason"].startswith("QQQ needs Alpaca keys")
    out = F.build("SPY", now=NOW, cal=CAL, market_open=True, index_body=_payload(),
                  trade_at=lambda s, a, lb: None)
    assert not out["available"] and "no SPY trade within 90 s" in out["reason"]
    out = F.build("SPY", now=NOW, cal=CAL, market_open=True, index_body=_payload(index_time_missing=True),
                  trade_at=_trade_at(700.0))
    assert not out["available"] and "carries no index time" in out["reason"]


# ----------------------------------------------------------------------------- override

def test_check_override_tolerance():
    assert F.check_override(45.0, 41.3, 3.0) == {"diff": pytest.approx(3.7), "tolerance": 3.0, "warning": True}
    assert F.check_override(43.0, 41.3, 3.0)["warning"] is False
    assert F.check_override(38.0, 41.3, 3.0)["warning"] is True
    assert F.check_override(40.0, None, 3.0) == {"diff": None, "tolerance": 3.0, "warning": False}


def test_override_keyless_uses_delayed_index_and_warns():
    body = _payload(spot=7000.0)
    par_basis = _true_forward(7000.0) - 7000.0
    out = F.build("SPX", now=NOW, cal=CAL, market_open=True, index_body=body, override_price=7050.0)
    o = out["override"]
    assert o["index_value"] == 7000.0 and o["delayed"] is True and o["index_source"] == "cboe_delayed"
    assert o["basis"] == 50.0 and "15 min delayed" in o["label"]
    assert out["basis"] == {"value": 50.0, "source": "override", "label": "your price"}
    assert out["check"]["warning"] is True and out["check"]["diff"] == pytest.approx(50.0 - par_basis, abs=1e-3)
    assert any("from the parity estimate" in w for w in out["warnings"])
    # a stored override basis sent back on refresh is checked the same way, without a new index read
    ok = F.build("SPX", now=NOW, cal=CAL, market_open=True, index_body=body, override_basis=par_basis + 1.0)
    assert ok["override"] is None and ok["basis"]["source"] == "override" and ok["check"]["warning"] is False


def test_override_live_index_from_etf_times_ratio():
    out = F.build("SPX", now=NOW, cal=CAL, market_open=True, index_body=_payload(spot=7000.0),
                  trade_at=_trade_at(700.0), latest_trade=lambda s: Spot(701.0, NOW, "alpaca_sip"),
                  override_price=7055.0)
    o = out["override"]
    assert o["index_source"] == "live_etf_ratio" and o["delayed"] is False
    assert o["index_value"] == pytest.approx(7010.0) and o["basis"] == pytest.approx(45.0)
    assert out["available"] and out["convert"]["basis"] == pytest.approx(45.0)


def test_override_when_parity_is_unavailable():
    out = F.build("SPX", now=NOW, cal=CAL, market_open=True, index_body=_payload(bid_zero=True), override_price=7040.0)
    assert out["available"] and out["basis"]["source"] == "override" and out["check"]["diff"] is None


def test_override_market_closed_is_labeled():
    out = F.build("SPX", now=NOW, cal=CAL, market_open=False, index_body=_payload(), override_price=7040.0,
                  trade_at=_trade_at(700.0), latest_trade=lambda s: Spot(701.0, NOW, "alpaca_sip"))
    assert out["override"]["delayed"] is True and "at the close" in out["override"]["label"]


# ----------------------------------------------------------------------------- tick rounding

@pytest.mark.parametrize("x, want", [
    (7792.12, 7792.0), (7792.125, 7792.25), (7792.374, 7792.25), (7792.375, 7792.5),
    (7792.87, 7792.75), (7792.9, 7793.0), (30030.854, 30030.75), (41.2963, 41.25),
])
def test_round_tick(x, want):
    assert F.round_tick(x) == want


# ----------------------------------------------------------------------------- API and the display-only rule

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GEXLAB_DEMO", "1")
    from gexlab import api, service
    service._CHAINS.clear()
    service._LEVEL_CACHE.clear()
    return TestClient(api.app, base_url="http://127.0.0.1")


def test_api_futures_demo(client):
    for t, sym in (("SPX", "/ESZ26"), ("NDX", "/MNQZ26"), ("SPY", "/ESZ26"), ("QQQ", "/MNQZ26")):
        j = client.get("/api/futures", params={"ticker": t}).json()
        assert j["available"] and j["demo"] and j["contract"]["symbol"] == sym, (t, j.get("reason"))
        assert j["basis"]["source"] == "parity" and j["parity"]["basis"] > 0
        assert len(j["parity"]["strikes"]) == config.FUTURES_PARITY_STRIKES
    j = client.get("/api/futures", params={"ticker": "SPX", "override_price": 7100}).json()
    assert j["override"]["basis"] == pytest.approx(7100 - j["parity"]["index_spot"], abs=1e-6)
    assert client.get("/api/futures", params={"ticker": "NVDA"}).status_code == 404
    assert client.get("/api/futures", params={"ticker": "SPX", "override_price": -5}).status_code == 422


def test_map_is_unchanged_by_the_futures_view(client):
    before = client.get("/api/map", params={"ticker": "SPX"}).json()
    client.get("/api/futures", params={"ticker": "SPX", "override_price": 7100})
    after = client.get("/api/map", params={"ticker": "SPX"}).json()
    assert "futures" not in after and before["levels"] == after["levels"] and before["strikes"] == after["strikes"]
