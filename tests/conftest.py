"""Shared test helpers for the public edition."""
from datetime import date, timedelta

import pytest

from gexlab import config as gconfig
from gexlab.engine.calendar import TradingCalendar


def pytest_configure(config):  # noqa: D103
    config.addinivalue_line("markers", "alpaca_index: run with SPX on Alpaca (config.ALPACA_INDEX)")


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch):
    """Tests never ask GitHub for releases; test_update_check turns it back on with an injected fetch."""
    monkeypatch.setenv("GEXLAB_NO_UPDATE_CHECK", "1")


@pytest.fixture(autouse=True)
def _index_source(request, monkeypatch):
    """Most tests were written when SPX came from Cboe and test that path with SPX payloads. They keep it.
    Tests marked `alpaca_index` run with SPX on Alpaca (config.ALPACA_INDEX, the default since 2026-11-20)."""
    if request.node.get_closest_marker("alpaca_index"):
        return
    monkeypatch.setattr(gconfig, "ALPACA_INDEX", {})
    monkeypatch.setattr(gconfig, "CBOE_TICKERS", {"SPX", "NDX"})


def exchange_cal(start=date(2026, 9, 1), end=date(2026, 11, 30), closes=None):
    """An exchange calendar shaped like Alpaca's /v2/calendar (weekdays, optional early closes)."""
    rows, d = [], start
    while d <= end:
        if d.weekday() < 5:
            rows.append({"date": d.isoformat(), "open": "09:30", "close": (closes or {}).get(d, "16:00")})
        d += timedelta(days=1)
    cal = TradingCalendar.from_alpaca(rows)
    cal.covered = (start, end)
    return cal
