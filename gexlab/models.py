"""Plain data shapes shared by sources, engine, recorder and API."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

# OCC-style symbol: ROOT + yymmdd + C/P + strike * 1000 (8 digits). Alpaca and Cboe both use it.
_OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


@dataclass(frozen=True)
class OccSymbol:
    root: str
    expiry: date
    type: str          # call | put
    strike: float


def parse_occ(symbol: str) -> OccSymbol:
    """Parse 'SPXW261029C07460000' or 'SPY261019P00755000'. Raises ValueError if malformed."""
    m = _OCC_RE.match(symbol.strip().replace(" ", ""))
    if not m:
        raise ValueError(f"not an OCC option symbol: {symbol!r}")
    root, ymd, cp, strike = m.groups()
    exp = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    return OccSymbol(root, exp, "call" if cp == "C" else "put", int(strike) / 1000.0)


@dataclass
class Contract:
    symbol: str
    root: str
    expiry: date
    settlement: str                 # AM | PM
    strike: float
    type: str                       # call | put
    multiplier: int = 100
    oi: int | None = None
    oi_date: str | None = None      # the vendor's open_interest_date for THIS contract (Cboe: inferred)
    oi_basis: str | None = None     # reported | zero_never_traded | zero_traded_after_oi_date | zero_vendor_null
    oi_source: str | None = None    # cboe | alpaca: which vendor's open interest this contract carries
    bid: float | None = None
    ask: float | None = None
    quote_ts: datetime | None = None
    iv: float | None = None
    iv_source: str | None = None    # solved_mid | indicative | cboe | demo (method of the IV in use)
    iv_ts: datetime | None = None   # market-data time of the quote the IV in use came from (None = unknown age)
    iv_shared: bool = False         # True when iv was copied from the other member of the strike pair
    iv_own: float | None = None     # this contract's own IV before sharing (None when it had none)
    iv_own_source: str | None = None
    iv_donor: str | None = None     # symbol whose IV is in use when iv_shared
    iv_donor_ts: datetime | None = None  # the donor's IV time (its iv_ts)
    last_trade_date: str | None = None   # vendor close_price_date (Alpaca); decodes a null OI record
    gamma: float | None = None
    gex: float | None = None        # signed, dollars per 1% move
    T: float | None = None          # years to settlement at as_of
    valid: bool = False
    invalid_reason: str | None = None


@dataclass
class Spot:
    price: float
    ts: datetime
    source: str                     # alpaca_sip | alpaca_iex | cboe | demo
    basis: str = "last_trade"       # last_trade (in session) | regular_close (market closed: the session's
                                    # official close, never an extended-hours print) | payload (Cboe current_price)
                                    # | prior_close (Cboe, market open but the index value predates today's open;
                                    # ts is then the index's own time, see cboe.parse_chain)


@dataclass
class Chain:
    """One fetched option chain for the 0 to 7 DTE window, ready for the engine."""
    ticker: str
    as_of: datetime                 # request (wall clock) time of the fetch
    spot: Spot
    feed: str                       # opra | indicative | cboe_delayed | demo
    contracts: list[Contract]
    oi_date: str | None = None
    source_ts: datetime | None = None   # payload timestamp when the source provides one (Cboe)
    complete: bool = True               # every page fetched
    raw_hash: str | None = None         # sha256 over raw response bodies, in fetch order
    warnings: list[str] = field(default_factory=list)
    requests: int = 0                   # vendor requests this fetch cost
    oi_source: str | None = None        # cboe | alpaca (demo: None): the open interest vendor for the chain
    oi_info: dict = field(default_factory=dict)  # OI provenance: label, basis, matched counts, fallback reason
    valuation_ts: datetime | None = None  # the one clock T is measured from (see calendar.valuation_time);
                                          # None = as_of (demo fixtures, older callers)
    index_ts: datetime | None = None    # Cboe index chains: the index value's own market-data time (Cboe's
                                        # last_trade_time, about 15 min before source_ts); None for other feeds

    @property
    def valuation(self) -> datetime:
        """Valuation time: T for every contract is settlement minus this. as_of stays the request time."""
        return self.valuation_ts or self.as_of
