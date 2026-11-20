"""Futures view on Alpaca data (SPX since Cboe's block, 2026-11-20): live parity spot + quarterly AM option quotes."""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

import pytest

from gexlab import config, futures as F
from gexlab.engine import bsm
from gexlab.engine.calendar import NY, TradingCalendar, time_to_expiry
from gexlab.models import Spot

UTC = timezone.utc
CAL = TradingCalendar.weekdays(date(2026, 9, 1), date(2027, 7, 31))
NOW = datetime(2026, 11, 20, 10, 30, 0, tzinfo=NY).astimezone(UTC)
DEC = date(2026, 12, 18)
R, Q, IV, S = 0.04, 0.012, 0.16, 7118.0


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    F._RATIO_CACHE.clear()
    F._BASIS_HIST.clear()
    monkeypatch.setattr(config, "FUTURES_RATE", R)


def snaps(spot=S, root="SPX", half=0.10):
    T = time_to_expiry(NOW, DEC, "AM")
    out = {}
    for i in range(-12, 13):
        k = spot - spot % 5 + 5 * i
        for cp, px in (("C", bsm.call_price(spot, k, T, R, IV, Q)), ("P", bsm.put_price(spot, k, T, R, IV, Q))):
            out[f"{root}{DEC:%y%m%d}{cp}{int(k * 1000):08d}"] = {"latestQuote": {"bp": px - half, "ap": px + half}}
    out[f"SPXW{DEC:%y%m%d}C{int(spot - spot % 5) * 1000:08d}"] = {"latestQuote": {"bp": 1.0, "ap": 999.0}}
    return out


def payload():
    return F.alpaca_payload("SPX", Spot(S, NOW, "alpaca_parity", basis="parity"), snaps())


def test_payload_keeps_only_the_am_root_and_is_live():
    p = payload()
    assert p.live and p.price == S and p.index_time == NOW
    assert list(p.pairs_by_expiry) == [DEC] and len(p.pairs_by_expiry[DEC]) == 25


def test_basis_is_the_true_forward_minus_spot():
    out = F.build("SPX", now=NOW, cal=CAL, market_open=True, payload=payload())
    T = time_to_expiry(NOW, DEC, "AM")
    assert out["available"] and out["basis"]["source"] == "parity"
    assert out["parity"]["forward"] == pytest.approx(S * math.exp((R - Q) * T), abs=0.01)
    assert out["convert"]["basis"] == pytest.approx(out["parity"]["forward"] - S, abs=0.01)


def test_spy_ratio_pairs_with_the_parity_time():
    seen = {}

    def trade_at(sym, at, lookback):
        seen["at"] = at
        return Spot(711.0, at, "alpaca_sip")
    out = F.build("SPY", now=NOW, cal=CAL, market_open=True, payload=payload(), trade_at=trade_at,
                  latest_trade=lambda s: Spot(711.5, NOW, "alpaca_sip"))
    assert out["available"] and seen["at"] == NOW
    assert out["ratio"]["value"] == pytest.approx(S / 711.0) and "put-call parity" in out["ratio"]["note"]


def test_override_uses_the_live_index():
    out = F.build("SPX", now=NOW, cal=CAL, market_open=True, payload=payload(), override_price=7160.0,
                  trade_at=lambda *a: None, latest_trade=lambda s: None)
    o = out["override"]
    assert o["index_source"] == "alpaca_parity" and o["delayed"] is False and o["basis"] == pytest.approx(42.0)
