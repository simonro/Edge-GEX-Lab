"""Vol Drift and DEX: context metrics on the Live Map (display only).

Every chain here is synthetic with a known answer: IVs set by hand (or prices built from a known IV and solved
back), Black-Scholes deltas computed directly. No network.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta, timezone

import pytest

from gexlab import config, service, store
from gexlab.engine import bsm
from gexlab.engine import dex as DX
from gexlab.engine import voldrift as VD
from gexlab.engine.calendar import NY, TradingCalendar
from gexlab.engine.iv import implied_volatility
from gexlab.models import Chain, Contract, Spot

UTC = timezone.utc
DEFAULT_SESSIONS = config.VOL_DRIFT_MIN_FULL_SESSIONS     # read before any test patches it
BANNED = re.compile(r"\b(buy|sell|bullish|bearish|signal|entry|should)\b", re.I)
DASHES = re.compile("[" + chr(0x2013) + chr(0x2014) + "]")


def cal(holidays=(), start=date(2026, 10, 1), end=date(2026, 12, 31)) -> TradingCalendar:
    rows, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            rows.append({"date": d.isoformat(), "open": "09:30", "close": "16:00"})
        d += timedelta(days=1)
    c = TradingCalendar.from_alpaca(rows)
    c.covered = (start, end)
    return c


CAL = cal()
WED = date(2026, 10, 28)
THU = date(2026, 10, 29)


def ny(d: date, h: int, m: int = 0) -> datetime:
    return datetime.combine(d, time(h, m), NY).astimezone(UTC)


def k(strike, typ, expiry=THU, iv=None, settlement="PM", **kw) -> Contract:
    return Contract(symbol=f"X{expiry:%y%m%d}{typ[0].upper()}{int(strike * 1000):08d}", root="X", expiry=expiry,
                    settlement=settlement, strike=float(strike), type=typ, iv=iv, **kw)


def smile(strikes, expiry=THU, base=0.20, slope=0.01, atm=100.0, **kw):
    """Calls and puts sharing a linear IV in strike: IV(K) = base + slope * (K - atm)."""
    return [k(s, t, expiry, base + slope * (s - atm), **kw) for s in strikes for t in ("call", "put")]


@pytest.fixture(autouse=True)
def _live_mode(monkeypatch):
    """Live mode, and a one-session rule so the hand-built chains can stay on a Wed / Thu pair. The 3-session default
    is tested on its own below and in the demo API tests."""
    monkeypatch.delenv("GEXLAB_DEMO", raising=False)
    monkeypatch.setattr(config, "VOL_DRIFT_MIN_FULL_SESSIONS", 1)
    for d in (service._VOL_FIRST, service._VOL_REC, service._VOL_PIN):
        d.clear()
    yield
    for d in (service._VOL_FIRST, service._VOL_REC, service._VOL_PIN):
        d.clear()


# ---------------------------------------------------------------- ATM interpolation

def test_atm_iv_interpolates_linearly_between_bracketing_strikes():
    cs = smile([97, 98, 99, 100, 101, 102, 103])
    res, why = VD.atm_iv(cs, 100.4, THU, "PM")
    assert why is None and (res.k_lo, res.k_hi) == (100.0, 101.0)
    assert res.iv == pytest.approx(0.204, abs=1e-12)        # 0.20 + 0.4 * (0.21 - 0.20)
    res, _ = VD.atm_iv(cs, 100.0, THU, "PM")                 # spot on a strike: that strike's IV
    assert (res.k_lo, res.k_hi) == (100.0, 100.0) and res.iv == pytest.approx(0.20)


def test_atm_iv_recovers_a_known_iv_from_prices():
    """Mids priced at a known 23% IV and solved back with the app's own solver give 23% at the money."""
    S, T, sigma = 701.3, 2.0 / 365.0, 0.23
    cs = []
    for s in range(690, 712):
        for t in ("call", "put"):
            price = bsm.option_price(S, s, T, 0.0, sigma, t)
            cs.append(k(s, t, iv=implied_volatility(price, S, s, T, 0.0, t)))
    res, _ = VD.atm_iv(cs, S, THU, "PM")
    assert res.iv == pytest.approx(sigma, abs=1e-5)


def test_atm_iv_averages_call_and_put_and_uses_each_contracts_own_iv():
    cs = smile([98, 99, 101, 102])
    cs += [k(100, "call", iv=0.20), k(100, "put", iv=0.22)]
    res, _ = VD.atm_iv(cs, 100.0, THU, "PM")
    assert res.iv == pytest.approx(0.21)                     # both usable: their average
    # The put's IV was copied from the call (strike-pair sharing): its own 0.22 is what counts.
    cs[-1] = k(100, "put", iv=0.20, iv_shared=True, iv_own=0.22)
    assert VD.atm_iv(cs, 100.0, THU, "PM")[0].iv == pytest.approx(0.21)
    # A shared IV with no own IV behind it is not a quote of that contract: only the call counts.
    cs[-1] = k(100, "put", iv=0.20, iv_shared=True, iv_own=None)
    assert VD.atm_iv(cs, 100.0, THU, "PM")[0].iv == pytest.approx(0.20)
    # An own IV outside the usable range (a bad print) is ignored the same way.
    cs[-1] = k(100, "put", iv=0.001)
    assert VD.atm_iv(cs, 100.0, THU, "PM")[0].iv == pytest.approx(0.20)


def test_atm_iv_uses_only_the_requested_expiry_and_root():
    cs = smile([99, 100, 101], base=0.20) + smile([99, 100, 101], base=0.40, settlement="AM")
    cs += smile([99, 100, 101], expiry=WED, base=0.90)
    assert VD.atm_iv(cs, 100.0, THU, "PM")[0].iv == pytest.approx(0.20)
    assert VD.atm_iv(cs, 100.0, THU, "AM")[0].iv == pytest.approx(0.40)


# ---------------------------------------------------------------- unavailable cases

def test_atm_iv_unavailable_never_guesses():
    assert VD.atm_iv(smile([99, 100, 101], expiry=WED), 100.0, THU, "PM") == (None, "no_contracts")
    few = [k(100, "call", iv=0.2), k(101, "call", iv=0.2), k(99, "put", iv=0.2)]          # 3 usable < 4
    assert VD.atm_iv(few, 100.0, THU, "PM") == (None, "too_few_quotes")
    unusable = smile([99, 101]) + [k(100, "call", iv=None), k(100, "put", iv=None)]
    assert VD.atm_iv(unusable, 100.0, THU, "PM")[0].iv == pytest.approx(0.20)               # 99 and 101 bracket
    above = smile([101, 102, 103])                                                          # nothing below spot
    assert VD.atm_iv(above, 100.0, THU, "PM") == (None, "no_bracket")
    far = smile([90, 94, 106, 110])                                                         # quotes, but not within 5%
    assert VD.atm_iv(far, 100.0, THU, "PM") == (None, "too_few_quotes")
    gap = smile([94, 96, 97, 106])                                                          # no strike above within 5%
    assert VD.atm_iv(gap, 100.0, THU, "PM") == (None, "no_bracket")


# ---------------------------------------------------------------- expiry selection

def test_target_expiry_skips_0dte_and_picks_the_next_full_day():
    cs = smile([100], expiry=WED) + smile([100], expiry=THU) + smile([100], expiry=date(2026, 10, 30))
    assert VD.target_expiry(cs, WED, CAL) == (THU, "PM")
    fri, mon = date(2026, 10, 30), date(2026, 11, 2)
    cs = smile([100], expiry=fri) + smile([100], expiry=mon)
    assert VD.target_expiry(cs, fri, CAL) == (mon, "PM")       # over the weekend: Monday settles after its close


def test_target_expiry_falls_back_to_the_nearest_listed_qualifying_expiry():
    fri = date(2026, 10, 30)
    cs = smile([100], expiry=WED) + smile([100], expiry=fri)   # weeklies only: no Thursday listed
    assert VD.target_expiry(cs, WED, CAL) == (fri, "PM")
    assert VD.target_expiry(smile([100], expiry=WED), WED, CAL) is None   # only 0DTE: nothing qualifies


def test_target_expiry_am_settlement_and_holidays():
    tf = date(2026, 11, 20)                                    # third Friday
    am_pm = smile([100], expiry=tf, settlement="AM") + smile([100], expiry=tf)
    assert VD.target_expiry(am_pm, date(2026, 11, 19), CAL) == (tf, "PM")   # PM root wins on the same date
    am_only = smile([100], expiry=tf, settlement="AM") + smile([100], expiry=date(2026, 11, 23))
    # AM settles at 09:30 Friday, before Friday's close: not a full day left
    assert VD.target_expiry(am_only, date(2026, 11, 19), CAL) == (date(2026, 11, 23), "PM")
    hol = cal(holidays={THU})                                  # Thursday closed: the next session is Friday
    cs = smile([100], expiry=THU) + smile([100], expiry=date(2026, 10, 30))
    assert VD.target_expiry(cs, WED, hol) == (date(2026, 10, 30), "PM")


def test_default_needs_three_full_sessions():
    assert DEFAULT_SESSIONS == 3
    mon, tue = date(2026, 11, 2), date(2026, 11, 3)
    cs = (smile([100], expiry=WED) + smile([100], expiry=THU) + smile([100], expiry=date(2026, 10, 30))
          + smile([100], expiry=mon) + smile([100], expiry=tue))
    # Wednesday: Thursday, Friday and Monday must be full sessions, so the Monday expiry is the first that qualifies
    assert VD.target_expiry(cs, WED, CAL, min_sessions=3) == (mon, "PM")
    hol = cal(holidays={date(2026, 10, 30)})                  # a Friday holiday pushes it to Tuesday
    assert VD.target_expiry(cs, WED, hol, min_sessions=3) == (tue, "PM")
    fri_only = smile([100], expiry=date(2026, 10, 30)) + smile([100], expiry=date(2026, 11, 6))
    # A Friday-only stock on a Wednesday: next Friday is beyond the 0 to 7 day chain, so nothing in it qualifies
    assert VD.target_expiry(fri_only[:2], WED, CAL, min_sessions=3) is None


def test_session_day_off_hours_maps_to_the_last_session():
    assert VD.session_day(CAL, ny(date(2026, 10, 31), 12)) == date(2026, 10, 30)   # Saturday -> Friday
    assert VD.session_day(CAL, ny(WED, 10)) == WED


# ---------------------------------------------------------------- threshold labels

def test_flat_band_is_relative_to_the_iv_level():
    assert (config.VOL_DRIFT_FLAT_MIN_PTS, config.VOL_DRIFT_FLAT_REL) == (0.5, 0.05)
    assert VD.flat_band(8.0) == pytest.approx(0.5)            # floor: 5% of 8 is 0.4
    assert VD.flat_band(10.0) == pytest.approx(0.5)
    assert VD.flat_band(19.0) == pytest.approx(0.95)
    assert VD.flat_band(42.0) == pytest.approx(2.1)


@pytest.mark.parametrize("chg,ref,label", [
    (0.5, 10.0, "rising"), (0.46, 10.0, "rising"), (0.44, 10.0, "flat"), (-0.5, 10.0, "falling"),
    (0.9, 19.0, "flat"), (1.0, 19.0, "rising"), (-0.96, 19.0, "falling"),
    (2.0, 42.0, "flat"), (2.1, 42.0, "rising"), (-2.04, 42.0, "flat"), (-2.06, 42.0, "falling"), (-2.2, 42.0, "falling"), (0.0, 30.0, "flat")])
def test_change_label_reads_the_displayed_change(chg, ref, label):
    assert VD.change_label(chg, VD.flat_band(ref)) == label


def test_pairing_text_is_plain_and_descriptive():
    assert VD.pairing("negative", "rising").startswith("Negative gamma and rising IV")
    assert VD.pairing(None, "rising") is None and VD.pairing("positive", None) is None
    for text in VD.PAIRINGS.values():
        assert not BANNED.search(text) and not DASHES.search(text)


# ---------------------------------------------------------------- reference selection (service)

def chain_at(iv_base: float, at: datetime, spot: float = 100.0, iv_ts="at") -> Chain:
    ts = at if iv_ts == "at" else iv_ts
    cs = smile([96, 97, 98, 99, 100, 101, 102, 103, 104], base=iv_base, iv_ts=ts, iv_source="solved_mid")
    cs += smile([96, 98, 100, 102, 104], expiry=WED, base=0.50, iv_ts=ts)   # 0DTE, never measured
    return Chain(ticker="SPY", as_of=at, spot=Spot(spot, at, "alpaca_sip"), feed="opra", contracts=cs,
                 valuation_ts=at)


def store_capture(path, at: datetime, iv_base: float, status="eligible", kind="snapshot", ticker="SPY",
                  day=WED, expiry=THU):
    with store.session(path) as conn:
        cid = conn.execute("INSERT INTO captures (ticker, kind, session_date, requested_at, completed_at, spot, spot_ts, "
                           "model_version, status) VALUES (?,?,?,?,?,?,?,?,?)",
                           (ticker, kind, day.isoformat(), at.isoformat(), at.strftime("%Y-%m-%dT%H:%M:%SZ"), 100.0,
                            at.strftime("%Y-%m-%dT%H:%M:%SZ"), "1.1", status)).lastrowid
        for c in smile([98, 99, 100, 101, 102], expiry=expiry, base=iv_base):
            conn.execute("INSERT INTO capture_contracts (capture_id, symbol, expiry, settlement, strike, type, iv, "
                         "iv_shared, iv_own, valid) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (cid, c.symbol, c.expiry.isoformat(), c.settlement, c.strike, c.type, c.iv, 0, c.iv, 1))


def test_recorder_snapshot_is_the_reference(tmp_path):
    db = tmp_path / "r.db"
    store_capture(db, ny(WED, 9, 45), 0.30, status="invalid")             # skipped: invalid
    store_capture(db, ny(WED, 10, 0), 0.18, kind="frozen", status="late")  # first usable capture
    store_capture(db, ny(WED, 10, 15), 0.25)
    now = ny(WED, 11, 30)
    vd = service.vol_drift("SPY", chain_at(0.20, now), CAL, now, True, "negative", db_path=db)
    assert vd["available"] and vd["expiry"] == THU.isoformat() and vd["settlement"] == "PM"
    assert vd["current"]["iv_pct"] == pytest.approx(20.0)
    ref = vd["reference"]
    assert ref["source"] == "recorder" and ref["kind"] == "frozen" and ref["ts"] == "2026-10-28T14:00:00Z"
    assert ref["iv_pct"] == pytest.approx(18.0)
    assert vd["change_pts"] == pytest.approx(2.0) and vd["label"] == "rising"
    assert vd["pairing"].startswith("Negative gamma and rising IV") and vd["tested"] is False
    assert "Untested" in vd["chip"]


def test_recorder_reference_must_be_on_the_measured_expiry(tmp_path):
    db = tmp_path / "r.db"
    store_capture(db, ny(WED, 9, 45), 0.18, expiry=date(2026, 10, 30))    # another expiry: not comparable
    now = ny(WED, 11, 30)
    vd = service.vol_drift("SPY", chain_at(0.20, now), CAL, now, True, "positive", db_path=db)
    assert vd["reference"]["source"] == "app" and vd["reference"]["iv_pct"] == pytest.approx(20.0)


def test_first_of_session_reference_when_nothing_is_recorded(tmp_path):
    db = tmp_path / "empty.db"
    t1, t2 = ny(WED, 10, 7), ny(WED, 13, 0)
    vd = service.vol_drift("NVDA", chain_at(0.40, t1), CAL, t1, True, "positive", db_path=db)
    assert vd["reference"]["source"] == "app" and vd["reference"]["ts"] == "2026-10-28T14:07:00Z"
    assert vd["change_pts"] == 0.0 and vd["label"] == "flat"
    vd = service.vol_drift("NVDA", chain_at(0.385, t2), CAL, t2, True, "positive", db_path=db)
    assert vd["reference"]["ts"] == "2026-10-28T14:07:00Z" and vd["reference"]["iv_pct"] == pytest.approx(40.0)
    assert vd["threshold_pts"] == pytest.approx(2.0)          # 5% of a 40% reference
    assert vd["change_pts"] == pytest.approx(-1.5) and vd["label"] == "flat"
    vd = service.vol_drift("NVDA", chain_at(0.375, t2), CAL, t2, True, "positive", db_path=db)
    assert vd["change_pts"] == pytest.approx(-2.5) and vd["label"] == "falling"
    assert vd["pairing"].startswith("Positive gamma and falling IV")


def test_no_reference_before_the_open_or_with_the_market_closed(tmp_path):
    db = tmp_path / "empty.db"
    pre = ny(WED, 9, 20)                                         # data valued before the open: never the reference
    vd = service.vol_drift("NVDA", chain_at(0.40, pre), CAL, pre, True, "positive", db_path=db)
    assert vd["available"] and vd["reference"] is None and "session" in vd["reference_reason"]
    assert vd["change_pts"] is None and vd["label"] is None and vd["pairing"] is None
    close = ny(WED, 16, 0)                                       # app started after the close: no reading of the day
    vd = service.vol_drift("NVDA", chain_at(0.40, close), CAL, ny(WED, 18, 0), False, "positive", db_path=db)
    assert vd["reference"] is None and vd["label"] is None


def test_vol_drift_unavailable_when_the_target_expiry_lacks_quotes(tmp_path):
    now = ny(WED, 11, 0)
    ch = chain_at(0.20, now)
    for c in ch.contracts:
        if c.expiry == THU:
            c.iv = None                                          # Thursday unquoted; Friday is NOT used instead
    ch.contracts += smile([98, 99, 100, 101, 102], expiry=date(2026, 10, 30), base=0.22)
    vd = service.vol_drift("SPY", ch, CAL, now, True, "negative", db_path=tmp_path / "e.db")
    assert vd["available"] is False and vd["expiry"] == THU.isoformat() and vd["current"] is None
    assert "too few usable quotes" in vd["reason"] and vd["label"] is None


# ---------------------------------------------------------------- DEX

def dex_contract(strike, typ, oi, iv=0.2, T=3 / 365, valid=True) -> Contract:
    c = k(strike, typ, iv=iv, oi=oi)
    c.T, c.valid = T, valid
    return c


def test_dex_sign_and_totals_on_a_synthetic_chain():
    S, T, iv = 100.0, 3 / 365, 0.2
    cs = [dex_contract(100, "call", 10), dex_contract(100, "put", 20), dex_contract(95, "put", 5),
          dex_contract(105, "call", 7, valid=False), dex_contract(110, "call", 0)]
    d = DX.compute(cs, S)
    dc = bsm.delta(S, 100, T, 0.0, iv, "call")
    dp = bsm.delta(S, 100, T, 0.0, iv, "put")
    dp95 = bsm.delta(S, 95, T, 0.0, iv, "put")
    assert dc > 0 > dp and dp95 < 0
    call_abs = dc * 10 * 100 * S
    put_abs = (-dp * 20 + -dp95 * 5) * 100 * S
    assert d.call_abs == pytest.approx(call_abs) and d.put_abs == pytest.approx(put_abs)
    # Net = call delta exposure minus put delta exposure: a put's negative delta counts negative, no extra flip.
    assert d.net == pytest.approx(call_abs - put_abs) and d.net < 0       # this chain is put heavy
    assert d.contracts == 4                                     # the invalid one is out; zero OI counts, adds nothing
    assert DX.contract_dex(dp, 20, 100, S) == pytest.approx(dp * 20 * 100 * S) and dp * 20 * 100 * S < 0
    call_heavy = DX.compute([dex_contract(100, "call", 50), dex_contract(100, "put", 5)], S)
    assert call_heavy.net == pytest.approx(call_heavy.call_abs - call_heavy.put_abs) and call_heavy.net > 0


def test_dex_payload_unavailable_without_usable_quotes():
    ch = chain_at(0.2, ny(WED, 11, 0))
    out = service._dex([], 100.0, ch, False)
    assert out["available"] is False and out["net_usd"] is None and out["call_usd"] is None
    assert "open interest" in out["caveat"] and "not a live flow" in out["caveat"]


# ---------------------------------------------------------------- API (demo fixtures)

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GEXLAB_DEMO", "1")
    monkeypatch.setattr(config, "VOL_DRIFT_MIN_FULL_SESSIONS", DEFAULT_SESSIONS)
    from fastapi.testclient import TestClient
    from gexlab import api
    service._CHAINS.clear()
    service._LEVEL_CACHE.clear()
    return TestClient(api.app, base_url="http://127.0.0.1")


@pytest.mark.parametrize("ticker,label", [("SPY", "falling"), ("QQQ", "rising"), ("SPX", "flat"),
                                          ("NDX", "rising"), ("NVDA", "flat")])
def test_demo_map_carries_vol_drift_and_dex(client, ticker, label):
    m = client.get("/api/map", params={"ticker": ticker}).json()
    vd, dx = m["vol_drift"], m["dex"]
    assert vd["available"] and vd["reference"]["source"] == "demo" and vd["label"] == label
    # Demo session Fri Oct 16: Mon, Tue and Wed must be full sessions, so Wed Oct 21 (NVDA lists Fridays: Oct 23)
    assert vd["expiry"] == ("2026-10-23" if ticker == "NVDA" else "2026-10-21")
    assert vd["reference"]["ts"] == "2026-10-16T13:45:00Z"
    assert 5 < vd["current"]["iv_pct"] < 80
    assert vd["threshold_pts"] == pytest.approx(round(max(0.5, 0.05 * vd["reference"]["iv_pct"]), 2))
    assert dx["available"] and dx["call_usd"] > 0 and dx["put_usd"] > 0 and "hedge_usd" not in dx
    assert dx["net_usd"] == pytest.approx(dx["call_usd"] - dx["put_usd"])
    for text in (vd["method"], vd["reference_method"], vd["clock_note"], vd["chip"], vd["pairing"] or "",
                 dx["method"], dx["caveat"]):
        assert not BANNED.search(text) and not DASHES.search(text), text


def test_demo_dex_follows_the_selected_expiries(client):
    one = client.get("/api/map", params={"ticker": "SPY", "expiries": "2026-10-16"}).json()["dex"]
    all_ = client.get("/api/map", params={"ticker": "SPY"}).json()["dex"]
    assert 0 < one["contracts"] < all_["contracts"] and one["call_usd"] < all_["call_usd"]
    vd_one = client.get("/api/map", params={"ticker": "SPY", "expiries": "2026-10-16"}).json()["vol_drift"]
    assert vd_one["expiry"] == "2026-10-21"                     # Vol Drift ignores the expiry selection


# ---------------------------------------------------------------- audit fixes (ChatGPT review, 2026-10-28)

def test_expiry_is_pinned_for_the_session(tmp_path):
    db = tmp_path / "e.db"
    t1, t2 = ny(WED, 10, 30), ny(WED, 11, 30)
    ch = chain_at(0.20, t1)
    ch.contracts += smile([98, 99, 100, 101, 102], expiry=date(2026, 10, 30), base=0.30, iv_ts=t1)
    assert service.vol_drift("SPY", ch, CAL, t1, True, "positive", db_path=db)["expiry"] == THU.isoformat()
    later = chain_at(0.20, t2)
    later.contracts = [c for c in later.contracts if c.expiry != THU]           # Thursday drops out of a response
    later.contracts += smile([98, 99, 100, 101, 102], expiry=date(2026, 10, 30), base=0.30, iv_ts=t2)
    vd = service.vol_drift("SPY", later, CAL, t2, True, "positive", db_path=db)
    assert vd["expiry"] == THU.isoformat() and vd["available"] is False and "not in the chain" in vd["reason"]


def test_label_reads_the_unrounded_band(tmp_path):
    """Reference 18.04%: band 0.902 points. A displayed +0.9 is under it (flat); the shown band rounds to 0.90."""
    db = tmp_path / "r.db"
    store_capture(db, ny(WED, 9, 45), 0.1804)
    now = ny(WED, 11, 0)
    vd = service.vol_drift("SPY", chain_at(0.1894, now), CAL, now, True, "negative", db_path=db)
    assert vd["threshold_pts"] == pytest.approx(0.9) and vd["change_pts"] == pytest.approx(0.9)
    assert vd["label"] == "flat"
    vd = service.vol_drift("SPY", chain_at(0.1714, now), CAL, now, True, "negative", db_path=db)
    assert vd["change_pts"] == pytest.approx(-0.9) and vd["label"] == "flat"


def test_unknown_age_reading_never_anchors_the_session(tmp_path):
    db = tmp_path / "e.db"
    t1, t2 = ny(WED, 10, 0), ny(WED, 10, 30)
    vd = service.vol_drift("NVDA", chain_at(0.40, t1, iv_ts=None), CAL, t1, True, "positive", db_path=db)
    assert vd["available"] and vd["reference"] is None                       # shown now, but no reference from it
    vd = service.vol_drift("NVDA", chain_at(0.42, t2), CAL, t2, True, "positive", db_path=db)
    assert vd["reference"]["iv_pct"] == pytest.approx(42.0) and vd["reference"]["data_ts"] == "2026-10-28T14:30:00Z"
    early = chain_at(0.40, ny(WED, 10, 45), iv_ts=ny(WED, 9, 20))           # quotes stamped before the open
    service._VOL_FIRST.clear()
    vd = service.vol_drift("NVDA", early, CAL, ny(WED, 10, 45), True, "positive", db_path=db)
    assert vd["reference"] is None


def test_change_withheld_when_the_current_chain_predates_the_reference(tmp_path):
    db = tmp_path / "r.db"
    store_capture(db, ny(WED, 9, 45), 0.22)
    at = ny(WED, 9, 44)                                                       # an older cached chain
    vd = service.vol_drift("SPY", chain_at(0.20, at), CAL, ny(WED, 9, 46), True, "negative", db_path=db)
    assert vd["reference"]["source"] == "recorder" and vd["change_pts"] is None and vd["label"] is None
    assert "older than the reference" in vd["reference_reason"]


def test_unavailable_on_the_weekday_fallback_calendar(tmp_path):
    fb = TradingCalendar.weekdays(date(2026, 10, 1), date(2026, 12, 31))
    now = ny(WED, 11, 0)
    vd = service.vol_drift("SPY", chain_at(0.20, now), fb, now, True, "negative", db_path=tmp_path / "e.db")
    assert vd["available"] is False and "calendar" in vd["reason"]


def test_off_hours_indicative_iv_is_not_measured(tmp_path):
    close = ny(WED, 16, 0)
    ch = chain_at(0.20, close)
    for c in ch.contracts:
        c.iv_source = "indicative"
    vd = service.vol_drift("SPY", ch, CAL, ny(WED, 20, 0), False, "negative", db_path=tmp_path / "e.db")
    assert vd["available"] is False and "too few usable quotes" in vd["reason"]
    vd = service.vol_drift("SPY", chain_at(0.20, close), CAL, ny(WED, 20, 0), False, "negative",
                           db_path=tmp_path / "e.db")
    assert vd["available"] and vd["current"]["iv_pct"] == pytest.approx(20.0)  # solved closing quotes still count


def test_flat_pairing_never_claims_zero_change():
    for (reg, lab), text in VD.PAIRINGS.items():
        if lab == "flat":
            assert "flat band" in text and "no change" not in text


def test_rendered_ui_text_keeps_the_vocabulary_rule():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "web" / "src" / "screens" / "LiveMap.tsx").read_text(encoding="utf-8")
    i, j = src.index("// ---------- DEX: one context line"), src.index("const WEEKDAY = new Intl.DateTimeFormat(")
    assert not BANNED.search(src[i:j]) and not DASHES.search(src[i:j])


def test_first_reading_with_quotes_stamped_after_the_fetch_start_still_shows_its_change(tmp_path):
    """Live regression (NVDA, 2026-10-28): a quote stamped a second after the fetch began made the first reading
    look newer than its own chain, so no change was shown at all."""
    at = ny(WED, 11, 31)
    ch = chain_at(0.42, at, iv_ts=at + timedelta(seconds=1))
    vd = service.vol_drift("NVDA", ch, CAL, at, True, "positive", db_path=tmp_path / "e.db")
    assert vd["reference"]["source"] == "app" and vd["change_pts"] == 0.0 and vd["label"] == "flat"
