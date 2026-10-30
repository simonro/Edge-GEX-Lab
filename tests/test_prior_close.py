"""Cboe before about 09:45 ET: the payload still carries the prior session's index value (seen 2026-10-29 at 09:39 ET:
last_trade_time Oct 28 16:14:59, SPX 7,744 while SPY put SPX near 7,701). The map labels it the prior close, the
Futures view computes no basis from it, Vol Drift never reads or anchors on it, and guidance shows no setups.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timezone

import pytest

import test_futures as TF
from gexlab import config, futures as F, service
from gexlab.engine import gex as G
from gexlab.engine.calendar import NY, predates_session
from gexlab.guidance import build as GB
from gexlab.models import Chain, Contract, Spot
from gexlab.sources import cboe
from conftest import exchange_cal

UTC = timezone.utc
CAL = exchange_cal()
WED, THU = date(2026, 10, 28), date(2026, 10, 29)
EXP = date(2026, 10, 30)                                          # Friday weekly, PM settled


def ny(d: date, h: int, m: int = 0, s: int = 0) -> datetime:
    return datetime.combine(d, time(h, m, s), NY).astimezone(UTC)


PRIOR = ny(WED, 16, 14, 59)          # Cboe's stamp on the prior session's closing value
PAYLOAD = ny(THU, 9, 38, 21)         # the file the map downloaded at 09:38
NOW = ny(THU, 9, 39)
LIVE_IDX = ny(THU, 9, 30, 23)        # the first same-day index value (payload stamped 09:45:25)


def _cboe(ts: datetime, price: float, index_time: datetime | None) -> bytes:
    opts = []
    for k in (7650.0, 7700.0, 7733.0, 7750.0, 7800.0):
        for cp in ("C", "P"):
            opts.append({"option": f"SPXW{EXP:%y%m%d}{cp}{int(k * 1000):08d}", "open_interest": 1000, "iv": 0.15,
                         "bid": 10.0, "ask": 10.4})
    data = {"current_price": price, "options": opts}
    if index_time is not None:
        data["last_trade_time"] = index_time.astimezone(NY).strftime("%Y-%m-%dT%H:%M:%S")
    return json.dumps({"timestamp": ts.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"), "data": data}).encode()


# ----------------------------------------------------------------------------- detection

def test_predates_session_only_while_the_market_is_open():
    assert predates_session(CAL, PRIOR, NOW)                       # 09:39 with yesterday's value
    assert not predates_session(CAL, LIVE_IDX, ny(THU, 9, 46))     # a same-day value after the open
    assert not predates_session(CAL, ny(THU, 9, 30), NOW)          # stamped at the open itself
    assert not predates_session(CAL, PRIOR, ny(THU, 9, 20))        # pre-market: the close label already applies
    assert not predates_session(CAL, PRIOR, ny(WED, 18, 0))        # after the close
    assert not predates_session(None, PRIOR, NOW)
    assert not predates_session(CAL, None, NOW)


def test_map_spot_is_labeled_the_prior_close_with_the_index_time():
    ch = cboe.parse_chain("SPX", _cboe(PAYLOAD, 7743.98, PRIOR), NOW, CAL)
    assert ch.spot.basis == "prior_close" and ch.spot.source == "cboe" and ch.spot.price == 7743.98
    assert ch.spot.ts == PRIOR                                     # the index's own time, not the download time
    assert ch.source_ts == PAYLOAD and ch.valuation == PAYLOAD     # the payload time stays for the feed and T
    assert ch.warnings[0].startswith("SPX is the prior close") and "09:45 ET" in ch.warnings[0]


def test_same_day_index_and_closed_market_keep_their_labels():
    after = cboe.parse_chain("SPX", _cboe(ny(THU, 9, 45, 25), 7704.10, LIVE_IDX), ny(THU, 9, 46), CAL)
    assert after.spot.basis == "payload" and not any("prior close" in w for w in after.warnings)
    pre = cboe.parse_chain("SPX", _cboe(ny(THU, 9, 15), 7743.98, PRIOR), ny(THU, 9, 16), CAL)
    assert pre.spot.basis == "regular_close"                       # pre-market reads as the close, as before
    none = cboe.parse_chain("SPX", _cboe(PAYLOAD, 7743.98, None), NOW, CAL)
    assert none.spot.basis == "payload"                            # no index time: nothing to judge by


def _levels(zg, nearest):
    fields = {f: None for f in G.Levels.__dataclass_fields__}
    fields.update(call_wall=7750.0, put_wall=7700.0, zero_gamma=zg, nearest_zero_gamma=nearest, regime="positive",
                  net_gex=1.0, gross_gex=2.0)
    return G.Levels(**fields)


def test_lead_names_the_prior_close():
    lv = _levels([7733.0], 7733.0)
    s = service.lead_sentence("SPX", 7743.98, lv, 94.2, prior_close=True)
    assert s.startswith("SPX at its prior close (7,744) sits above the gamma flip")
    assert "first live SPX value arrives about 09:45 ET" in s
    assert service.lead_sentence("SPX", 7743.98, lv, 94.2).startswith("SPX at 7,744 sits")


def test_no_dashes_in_the_new_text():
    lv = _levels([], None)
    texts = [cboe.PRIOR_CLOSE_WARNING.format(t="SPX"), service.lead_sentence("SPX", 7744.0, lv, 90.0, True)]
    assert not any(chr(0x2013) in t or chr(0x2014) in t for t in texts)


# ----------------------------------------------------------------------------- Futures view

@pytest.fixture
def futures_reset(monkeypatch):
    F._PARSED.clear()
    F._RATIO_CACHE.clear()
    F._BASIS_HIST.clear()
    monkeypatch.setattr(config, "FUTURES_RATE", TF.R)
    yield
    F._BASIS_HIST.clear()


def test_parity_is_unavailable_on_the_prior_close_and_stays_out_of_the_history(futures_reset):
    body = TF._payload(spot=7744.0, ts=PAYLOAD, index_time=PRIOR)
    out = F.build("SPX", now=NOW, cal=CAL, market_open=True, index_body=body)
    par = out["parity"]
    assert par["prior_close"] is True and par["available"] is False and par["basis"] is None
    assert "prior close" in par["reason"] and "09:45 ET" in par["reason"]
    assert not out["available"] and "Type your /ES price" in out["reason"]
    assert F._BASIS_HIST == {}                                     # never smoothed into a later basis
    # The first same-day payload starts a clean window.
    later = F.build("SPX", now=ny(THU, 9, 46), cal=CAL, market_open=True,
                    index_body=TF._payload(spot=7704.0, ts=ny(THU, 9, 45, 25), index_time=LIVE_IDX))
    assert later["available"] and later["parity"]["prior_close"] is False
    assert later["parity"]["basis_window"]["payloads"] == 1


def test_override_still_works_on_the_prior_close(futures_reset):
    body = TF._payload(spot=7744.0, ts=PAYLOAD, index_time=PRIOR)
    out = F.build("SPX", now=NOW, cal=CAL, market_open=True, index_body=body, override_price=7760.0)
    assert out["available"] and out["basis"]["source"] == "override"
    assert out["override"]["basis"] == pytest.approx(16.0) and "prior close" in out["override"]["label"]
    assert out["check"]["diff"] is None
    # With Alpaca keys the index value is live: last ETF trade times the ratio priced at the prior close (16:00).
    calls = []
    live = F.build("SPX", now=NOW, cal=CAL, market_open=True, index_body=body, override_price=7735.0,
                   trade_at=TF._trade_at(772.31, calls), latest_trade=lambda s: Spot(768.04, NOW, "alpaca_sip"))
    o = live["override"]
    assert o["index_source"] == "live_etf_ratio" and o["delayed"] is False
    assert calls[0][1] == ny(WED, 16, 0)
    assert o["index_value"] == pytest.approx(768.04 * 7744.0 / 772.31)


def test_etf_ratio_pairs_a_closing_index_stamp_with_the_regular_close(futures_reset):
    assert F.pair_time(PRIOR, CAL) == ny(WED, 16, 0)
    assert F.pair_time(ny(THU, 11, 44, 31), CAL) == ny(THU, 11, 44, 31)
    assert F.pair_time(PRIOR, None) == PRIOR
    calls = []
    out = F.build("SPY", now=ny(WED, 18, 0), cal=CAL, market_open=False,
                  index_body=TF._payload(spot=7744.0, ts=ny(WED, 16, 15), index_time=PRIOR),
                  trade_at=TF._trade_at(772.31, calls))
    assert calls == [("SPY", ny(WED, 16, 0), config.FUTURES_RATIO_MAX_LAG_S)]
    assert out["ratio"]["value"] == pytest.approx(7744.0 / 772.31) and out["ratio"]["lag_s"] == 2.0


# ----------------------------------------------------------------------------- Vol Drift and guidance

def test_vol_drift_never_reads_or_anchors_on_the_prior_close(monkeypatch, tmp_path):
    monkeypatch.delenv("GEXLAB_DEMO", raising=False)
    for d in (service._VOL_FIRST, service._VOL_REC, service._VOL_PIN):
        d.clear()
    cs = [Contract(symbol="", root="SPXW", expiry=date(2026, 11, 2), settlement="PM", strike=k, type=t,
                   iv=0.15, iv_source="cboe", iv_own=0.15, iv_own_source="cboe", iv_ts=PAYLOAD)
          for k in (7700.0, 7740.0, 7750.0, 7800.0) for t in ("call", "put")]
    ch = Chain(ticker="SPX", as_of=NOW, spot=Spot(7743.98, PRIOR, "cboe", basis="prior_close"), feed="cboe_delayed",
               contracts=cs, source_ts=PAYLOAD, valuation_ts=PAYLOAD)
    vd = service.vol_drift("SPX", ch, CAL, NOW, True, "positive", db_path=tmp_path / "empty.db")
    assert not vd["available"] and vd["current"] is None and vd["reference"] is None
    assert "09:45 ET" in vd["reason"] and "prior close" in vd["reason"]
    assert service._VOL_FIRST == {} and service._VOL_PIN == {}
    for d in (service._VOL_FIRST, service._VOL_REC, service._VOL_PIN):
        d.clear()


def test_guidance_shows_no_setups_on_the_prior_close():
    m = json.loads((config.FIXTURES_DIR / "map_SPX.json").read_text(encoding="utf-8"))
    m["spot"]["basis"] = "prior_close"
    g = GB.build(m)
    assert g["scenarios"] == []
    assert any(n.startswith(GB.NO_SETUPS) and "prior close" in n for n in g["setup_notes"])
