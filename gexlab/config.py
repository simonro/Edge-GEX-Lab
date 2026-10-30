"""Every knob Edge GEX Lab uses, each with the reason it holds that value.

Change behavior here, not in the modules. Values that come from the frozen
model v1.0 spec are marked SPEC.
"""
from __future__ import annotations

import os
from datetime import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- identity
APP_VERSION = "1.2.0"            # The public release this build is. Bump it before tagging a release: the update check
                                 # (gexlab/update_check.py) compares it with GitHub's latest release tag.
UPDATE_REPO = "simonro/Edge-GEX-Lab"  # Where the update check looks for releases (the public repo).
UPDATE_EVERY_H = 24              # At most one update check a day; the answer is cached in CACHE_DIR.
UPDATE_TIMEOUT_S = 3             # The check gives up after 3 s, silently.
MODEL_VERSION = "1.1"           # Model/input version, stored on every capture. 1.0 = SPEC math with Alpaca OI for
                                 # SPY, QQQ and stocks. 1.1 (2026-10-19, Cboe OI switch): same gamma math, but open
                                 # interest for Alpaca-quoted tickers now comes from Cboe (OI_SOURCE). Inputs changed,
                                 # so the version moved; the capture gate stays 1.2.
GATE_VERSION = "1.3"             # Capture eligibility gate. 1.3 (2026-10-19 evening, day-1 audit): 1.2 plus
                                 # (a) open interest must be from the previous completed session (OI_STALE_MAX_FRAC),
                                 # (b) a band contract with OI whose |GEX| cannot be estimated (no donor IV) fails the
                                 # capture instead of being dropped from the denominator (no safe upper bound exists for
                                 # a missing near-expiry contract), (c) one quote clock: a quote is "future" only when it
                                 # is stamped after it was received (the fetch: the time it arrived; the gate: the capture's
                                 # completion), and at the close only within CLOSED_QUOTE_AFTER_CLOSE_S of the close.
                                 # History: 1.2 (2026-10-19 11:15 ET).
                                 # 1.0 gated on the CONTRACT count within +-10%; 1.1 (2026-10-19, 10:15 snapshot on)
                                 # on OPEN INTEREST within +-5% (GATE_OI_*); 1.2 on the estimated ABSOLUTE GEX within
                                 # +-5% (GATE_GEX_MIN_PCT). Stored on every new capture (captures.gate_version); rows
                                 # with no value were judged by 1.0, and every row keeps the status its own gate gave
                                 # it. The GEX math is unchanged, so MODEL_VERSION stays 1.0, and the study protocol
                                 # (touches, placebo seeds) is unchanged, so the study protocol version stays 1.0.
PINNED_TICKERS = ["SPY", "QQQ", "SPX", "NDX"]  # SPEC: the four pinned chips.
CBOE_TICKERS = {"SPX", "NDX"}    # SPEC: Alpaca has no SPX/NDX quotes, so these come from Cboe.
CBOE_ROOTS = {"SPX": {"SPX": "AM", "SPXW": "PM"},
              "NDX": {"NDX": "AM", "NDXP": "PM"}}  # SPEC: monthly roots settle AM, weeklies PM.

# ---------------------------------------------------------------- modes
def is_demo() -> bool:
    """SPEC: GEXLAB_DEMO=1 serves fixtures, no network. Read live so tests can toggle it."""
    return os.getenv("GEXLAB_DEMO", "0") == "1"


FIXTURES_DIR = ROOT / "fixtures"             # Synthetic inputs for DEMO mode and tests.
DEMO_DB_DEFAULT = ROOT / "data" / "gexlab_demo.db"  # DEMO writes never touch the research DB (GEXLAB_DEMO_DB overrides).
CACHE_DIR = ROOT / "data" / "cache"          # Gitignored (data/); holds the Cboe payload cache and asset list.
API_PORT = int(os.getenv("GEXLAB_PORT") or 8240)  # API port, 8240 by default. launch.bat reads the same GEXLAB_PORT.

# ---------------------------------------------------------------- Alpaca
ALPACA_DATA_BASE = "https://data.alpaca.markets"  # Market data host (REST only, never the websocket).
ALPACA_TRADING_BASES = {"paper": "https://paper-api.alpaca.markets",
                        "live": "https://api.alpaca.markets"}  # SPEC: ALPACA_MODE picks the trading base.
CONTRACTS_PAGE_LIMIT = 10000     # Alpaca maximum per page for /v2/options/contracts; fewest requests.
SNAPSHOTS_PAGE_LIMIT = 1000      # Alpaca maximum per page for option snapshots.
FEED_LABELS = {
    "opra": "OPRA quotes, refreshed 60s",
    "indicative": "Indicative quotes, modified; trades delayed 15m",
    "cboe_delayed": "Cboe delayed quotes (about 15 min)",
    "demo": "Synthetic fixture",
}                                # SPEC: labels exactly as written.
FEED_REDETECT_S = 3600           # Re-test OPRA entitlement hourly so an upgraded plan is picked up without a restart.
ASSET_CACHE_S = 86400            # SPEC: asset list for search is cached daily.
CALENDAR_CACHE_S = 21600         # Calendar changes rarely; 6 h keeps early closes current without spamming.
CALENDAR_RETRY_S = 300           # After a failed calendar fetch, retry in 5 min instead of trusting a fallback for 6 h.
CALENDAR_DISK_CACHE = ROOT / "data" / "cache" / "calendar_alpaca.json"  # Last validated exchange calendar, survives restarts.
CONTRACTS_CACHE_S = 1800         # OI moves once a day; 30 min avoids refetching thousands of contracts every refresh.

# ---------------------------------------------------------------- rate limits
RATE_CAP_PER_MIN = 60            # SPEC: app-level cap on Alpaca requests per minute.
RATE_RESERVE_FOR_RECORDER = 15   # SPEC "recorder has priority": the live map may use at most 45 of the 60.
RATE_DB = ROOT / "data" / "ratelimit.sqlite"  # Shared across the API and recorder processes.
HTTP_TIMEOUT_S = 30              # Snapshots pages can be slow; long enough not to fail on a healthy API.
HTTP_MAX_RETRIES = 4             # 429/5xx backoff attempts before giving up on a page.
HTTP_BACKOFF_BASE_S = 1.5        # Exponential backoff base (1.5, 3, 6, 12 s) unless Retry-After says otherwise.

# ---------------------------------------------------------------- Cboe
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"  # SPEC source. Indexes take a
                                 # leading underscore (_SPX, _NDX); ETFs and stocks do not (SPY, QQQ, AAPL).
CBOE_INDEX_SYMBOLS = {"SPX": "_SPX", "NDX": "_NDX"}  # Cboe payload symbol for each index we serve.
CBOE_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36 GEXLab/1.0 (personal research, polite)")
                                 # SPEC: needs a browser User-Agent; we also identify ourselves.
CBOE_CACHE_S = 60                # SPEC: at most one fetch per ticker per 60 s (disk cache, shared by processes).
CBOE_TIMESTAMP_TZ = "UTC"        # Verified 2026-10-17: payload "13:32:46" arrived at 09:33 ET, so it is UTC.
CBOE_MAX_PAYLOAD_AGE_S = 300     # Frozen capture: the payload must be generated within 5 min of the run's completion.
CBOE_OPEN_GAP_MIN = 30           # Verified 2026-10-20: the 09:42 ET payload (about 09:27 market time) had bid 0, ask 0 and
                                 # iv 0 on every SPXW contract; by 09:45 ET 144 of 339 were usable. A chain with no quotes
                                 # this soon after the open is labeled as the delayed feed's first minutes, not a market state.
CBOE_DELAY_MIN = 15              # Cboe's public quotes run about 15 minutes behind the market (named in the lead).
CBOE_FIRST_QUOTES_MIN = 20       # Minutes after the open by which the delayed payload has carried quotes every day so far
                                 # (2026-10-20 to 10-23: the 09:45 payload was stamped 09:42 to 09:44 ET, about 09:27
                                 # market time, and had none; by 09:48 SPX was usable).
CBOE_LOCK_TIMEOUT_S = 120        # Max wait for the cross-process Cboe cache lock (a 13 MB fetch can take a while).
OI_SOURCE = "cboe"               # Open interest for Alpaca-quoted tickers (SPY, QQQ, stocks): "cboe" or "alpaca".
                                 # Verified 2026-10-19 11:50 ET: Alpaca still served OI dated Thu Oct 15 for QQQ Oct 19
                                 # (calls 50,757 / puts 73,657 at strikes 700 to 750) while Cboe carried Friday's
                                 # positioning (157,975 / 141,447). Quotes, spot and IV stay on Alpaca; only OI moves.
CBOE_OI_CACHE_S = 600            # OI changes once a day, so an OI-only payload is reused for 10 min (>= the 60 s floor).
CBOE_OI_MIN_MATCH = 0.90         # Cboe must list at least 90% of the Alpaca contracts (by OCC symbol) or the whole
                                 # chain falls back to Alpaca OI, labeled. Below that the payload is not the same chain.
CBOE_OI_BUDGET_S = 25           # End-to-end budget for the OI overlay (cache lock wait plus the HTTP fetch). The spot is
                                 # already read, so a slow Cboe falls back to Alpaca OI (labeled, and failed by the OI
                                 # freshness gate if that OI is old) before the 60 s spot-age gate can fail the capture.
CBOE_CHAIN_BUDGET_S = 75         # End-to-end budget for a recorder index chain (SPX, NDX): lock wait plus a 7 to 13 MB fetch.
                                 # Four tickers in sequence still finish inside the 10:00 to 10:05 frozen window.
CBOE_FAIL_COOLDOWN_S = 60        # After a Cboe failure, dashboard requests for that payload wait this long (or Retry-After,
                                 # capped at CBOE_FAIL_COOLDOWN_MAX_S) before asking again; shared across processes on disk.
                                 # Recorder runs ignore the cooldown (a missed 10:00 capture cannot be redone) but keep
                                 # their own budget.
CBOE_FAIL_COOLDOWN_MAX_S = 300
CBOE_OI_SAME_MAX_FRAC = 0.01     # When at most 1% of the contracts both feeds report differ, Cboe carries Alpaca's
                                 # OI vintage and takes Alpaca's dated OI date; above it Cboe is the newer file and its
                                 # date is inferred as the most recent completed session before the payload day.

# ---------------------------------------------------------------- window and refresh
WINDOW_DAYS = 7                  # SPEC: expiries 0 to 7 calendar days.
REFRESH_MIN_S = 60               # SPEC: live map refresh at most every 60 s; chain cache TTL.
STALE_AFTER_S = 180              # SPEC: no successful refresh in 3 min means stale.
FETCH_STRIKE_BAND = 0.20         # Fetch strikes within +-20% so the +-10% zero-gamma sweep sees edge gamma.
DISPLAY_STRIKE_BAND = 0.10       # SPEC: strikes in /api/map limited to spot +-10%.
WALL_STRIKE_BAND = 0.10          # Walls chosen inside the displayed band so a wall is always on the chart.
SWEEP_BAND = 0.10                # SPEC: zero-gamma sweep within +-10% of spot.
PROFILE_POINTS = 201             # SPEC: profile_curve about 200 points (201 includes spot at the center).
SWEEP_STEP = {"SPY": 0.01, "QQQ": 0.01, "SPX": 0.5, "NDX": 1.0}  # SPEC grid steps.
STOCK_STEP_FRAC = 0.01           # SPEC: "0.01 * price rounded" for stocks (literal reading, about 20 grid points).
SWEEP_CHUNK = 256                # Grid points per numpy chunk; bounds memory near 256 x contracts floats.

# ---------------------------------------------------------------- IV and quality
WIDE_SPREAD_FRAC = 0.50          # Mid unusable when (ask - bid) / mid exceeds 50%: the mid is not a price there.
IV_MIN = 0.01                    # An IV below 1% is a bad print, not a market; treated as invalid.
IV_MAX = 5.0                     # Matches the solver ceiling (500%); above it the solve is meaningless.
NEUTRAL_FRAC = 0.02              # Regime "neutral" when |net GEX| is under 2% of gross GEX at spot.
COVERAGE_STRIKE_BAND = 0.10     # Coverage counts OI > 0 contracts within +-10%, the band the map and sweep describe.
OI_MISSING_MAX_FRAC = 0.50       # More than half the chain with no OI record means the OI feed failed: invalid.
QUALITY_MIN_PCT = 95.0           # Gate 1.0 (diagnostic since 1.1): share of CONTRACTS within +-10% with usable OI and IV.
                                 # Day 1 showed why it cannot gate: SPY 10:00 failed at 91.7% while 99.9% of its
                                 # estimated |GEX| was valid; the misses were 0DTE tail puts quoted 0.01 x 0.02
                                 # (a one-tick spread is 67% of mid) with negligible gamma. Still reported.
GATE_OI_BAND = 0.05              # Gate band (1.1 and 1.2): +-5% of spot. The walls, the flip and the reactions the studies
                                 # measure sit here (an ATR is about 1%); beyond it 0-7 DTE gamma is tiny.
GATE_OI_MIN_PCT = 95.0           # Gate 1.1 (diagnostic reference since 1.2): valid contracts held >= 95% of the open
                                 # interest inside GATE_OI_BAND. Still stored on every capture (oi_coverage_pct).
                                 # Day 1 showed why it cannot gate: SPY 10:30 OI 93.38% with |GEX| 99.53%, QQQ 10:45
                                 # OI 94.40% with |GEX| 98.25%. The OI misses were 0DTE tail puts quoted 0.01 x 0.02:
                                 # real open interest, almost no gamma.
GATE_OI_UNKNOWN_WEIGHT = "median"  # A contract inside the band with NO OI record is invalid, and its unknown OI is
                                 # weighted as the median OI of the band's known contracts (it cannot be ignored,
                                 # and treating it as zero would hide a broken OI feed).
GATE_GEX_MIN_PCT = 98.0          # Gate 1.2: valid contracts must hold >= 98% of the estimated ABSOLUTE GEX inside
                                 # GATE_OI_BAND, on the unrounded ratio. |GEX| weights each miss by the gamma
                                 # exposure it hides, which is what the walls and the flip are built from. An invalid
                                 # contract's gamma uses the IV of the nearest valid strike of the same expiry and
                                 # type, else of the same expiry, else it is left out and counted. Day 1 (2026-10-19)
                                 # measured 98.25% to 99.97% on every capture, those failing gate 1.1 included, so 98
                                 # passes healthy chains and still fails one missing a real slice of gamma. No
                                 # computable share (null) = invalid.
OI_STALE_MAX_FRAC = 0.01         # Gate 1.3: at most 1% of the open interest inside GATE_OI_BAND may sit on contracts whose
                                 # OI is not dated the previous completed session (older, undated or mixed sources). Day 1
                                 # showed why: QQQ's 10:00 capture used Alpaca OI dated Oct 15 on Oct 19 (two sessions old)
                                 # and still passed. Cboe does not publish a date, so its date is INFERRED from the payload
                                 # time (research policy, declared here): a payload stamped on the session day carries the
                                 # previous session's OI. A payload with no time, or a time in the future, is not used.
SPOT_MAX_AGE_S = 60              # SPEC: spot age <= 60 s, measured at completion AND against the valuation time.
CLOCK_SKEW_S = 2                 # A vendor timestamp later than our completion clock by more than this is "future": rejected.
QUOTE_SPOT_MAX_SKEW_S = 60       # An OPRA quote more than 60 s from the spot trade is not solved against that spot.
CLOSED_QUOTE_AFTER_CLOSE_S = 960  # Market closed: the spot is the session close, and an OPRA closing quote may be stamped up to
                                 # 16 min after it (ETF and index options trade until 16:15 ET; verified 2026-10-17: SPY
                                 # quotes at 16:14:59). Such a quote is solved at the close; one older than 60 s before it is not.
CLOSE_BAR_LOOKBACK_MIN = 15      # Fallback close (no daily bar): the last 1-minute bar ending at or before the close,
                                 # searched over the final 15 minutes of the session.
INDICATIVE_IV_MAX_AGE_S = 300    # An indicative IV is used only if its quote is at most 5 min older than the valuation
                                 # time (the same tolerance the Cboe payload gets). Older = "iv_stale"; no quote time =
                                 # "iv_time_unknown". Frozen and snapshot captures exclude both (never shared either);
                                 # the live map shows unknown-age IV with a label and, outside market hours, stale IV too.
OI_NULL_AMBIGUOUS_MAX_FRAC = 0.50  # Alpaca returns open_interest null, never 0, for a series with no OI on the OI date
                                 # (verified 2026-10-17: 0 explicit zeros in 3,060 SPY and QQQ contracts). Null is read
                                 # as zero only while the OI feed is healthy: of the series that traded on or before the
                                 # OI date, at most this share may lack a record. Above it the feed is treated as broken
                                 # and the nulls stay unknown (they count against coverage).

# ---------------------------------------------------------------- option settlement (America/New_York)
DEFAULT_SETTLE_PM = time(16, 0)  # SPEC: PM settlement 16:00 ET (early-close aware via the calendar).
DEFAULT_SETTLE_AM = time(9, 30)  # SPEC: AM settlement 09:30 ET.

# ---------------------------------------------------------------- AI-free copy guard
BANNED_WORDS = ("buy", "sell", "bullish", "bearish", "signal", "entry", "should")  # Brand rules 2 and 4.

# ---------------------------------------------------------------- net GEX magnitude reference
ADV_SESSIONS = 20                # Average daily dollar volume over the last 20 completed regular sessions: one trading
                                 # month, long enough to smooth one heavy day, short enough to track a regime change.
ADV_LOOKBACK_DAYS = 45           # Calendar days of daily bars requested to be sure 20 sessions are inside (holidays).
ADV_FEED = "sip"                 # Dollar volume must be consolidated. IEX alone is a few percent of volume, so an IEX
                                 # ADV would overstate the share roughly 30x: no SIP, no ADV (null with a reason).
ADV_CACHE_FILE = ROOT / "data" / "cache" / "adv_daily.json"  # One entry per symbol, valid for one completed session.
ADV_RETRY_S = 300                # After a failed ADV fetch, retry in 5 min, never on every 60 s map refresh.
_NO_REF = "No free volume reference: index hedging trades mostly in ES and NQ futures"
ADV_NO_REFERENCE = {"SPX": _NO_REF, "NDX": _NO_REF}  # No share volume, and the hedge trades mostly in futures we have
                                 # no free data for. Scaling SPY/QQQ dollar volume by the price ratio is a units
                                 # error, so no share of ADV is shown (decision 2026-10-17, PROPOSED, supersedes the proxy).
MAGNITUDE_MOVES = (0.5, 1.0, 2.0)  # Hedge-flow rows in the method drawer: linear in the move, labeled as such.
MAGNITUDE_TICKS = (1, 2, 5, 10)  # Neutral reference ticks on the scale, percent of ADV. Descriptive, no colors.
PERCENTILE_MIN_HISTORY = 20      # No percentile until 20 eligible frozen captures exist; fewer is not a distribution.
ATR_SESSIONS = 14                # Typical day = simple mean true range of the last 14 completed regular sessions (the
                                 # common ATR length). Needs 15 daily bars: the first one only supplies a prior close.
ATR_CACHE_FILE = ROOT / "data" / "cache" / "atr_daily.json"  # Same daily cache rule as ADV: one entry per completed session.
ATR_ADJUSTMENT = "split"         # A split inside the window would read as a huge true range on raw bars. Split adjusted
                                 # prices are right for a range; dividends are left alone (ADV stays raw dollars).
ATR_FROM_ETF = {"SPX": "SPY", "NDX": "QQQ"}  # Alpaca has no index bars. ATR% is a percentage, so the ETF's ATR% is a
                                 # fair stand in for the index's (no units problem, unlike dollar volume). Labeled.

# ---------------------------------------------------------------- context metrics (display only)
# Vol Drift and DEX sit beside the levels on the Live Map. Neither touches the GEX math, the levels, the gate, the
# recorder captures or the studies.
VOL_DRIFT_MIN_FULL_SESSIONS = 3  # The expiry measured is the nearest one that settles at or after the close of the
                                 # third session after the current one: at least three full trading days left
                                 # (decided 2026-10-28). 0DTE IV is distorted late in the day, and on a 1-day expiry the clock
                                 # itself (calendar T, quiet overnight hours) moves IV through the session; three
                                 # sessions dilute that. Friday-only stocks have no such expiry inside the 0 to 7 day
                                 # window on Wednesdays and Thursdays: unavailable then.
VOL_DRIFT_BAND = 0.05            # Both strikes bracketing spot must lie within 5% of spot, and the quotes counted toward
                                 # VOL_DRIFT_MIN_USABLE are those inside the same band.
VOL_DRIFT_MIN_USABLE = 4         # Fewer usable IVs (calls plus puts) inside the band on that expiry: unavailable, never
                                 # a guess. Four is two strikes with both sides, the least that brackets spot.
VOL_DRIFT_FLAT_MIN_PTS = 0.5     # Flat band, relative to the IV level (decided 2026-10-28): flat while the change, as
VOL_DRIFT_FLAT_REL = 0.05        # displayed (0.1 vol point), is below max(0.5 vol point, 5% of the reference IV);
                                 # rising or falling at or beyond it. SPY at 19% IV: 0.95 point; NVDA at 42%: 2.1
                                 # points. Round starting values, not tuned on any outcome.
VOL_DRIFT_DEMO_REF = FIXTURES_DIR / "vol_ref.json"   # Synthetic 09:45 references for demo mode.

# ---------------------------------------------------------------- trade guidance
GUIDANCE_MAX_DATA_AGE_S = 300   # Market open: no setups when the vendor's data time (spot trade, or the Cboe payload) is
                                 # more than 5 min old, even if the last fetch "succeeded" (day-1 verification).
GUIDANCE_MIN_RR = 1.0            # A scenario is shown only when the reward from the entry zone's worst edge to target 1
                                 # is at least the risk from that edge to the stop. Below 1 to 1 the
                                 # idea needs a hit rate above 50% to break even, which nothing here has measured.

# ---------------------------------------------------------------- futures view (display transform only)
# The Live Map's Futures view relabels levels as /ES or /MNQ equivalents. It never touches the GEX math, the levels,
# the recorder, the frozen captures or the studies: every map number stays in index or ETF units, and the UI adds
# the basis (and, for SPY and QQQ, the ETF to index ratio) at display time. gexlab/futures.py has the method.
FUTURES = {                      # Index -> the futures it settles: /ES and /MES settle to the SPX special opening
                                 # quotation on the quarterly expiry, /NQ and /MNQ to the NDX one, the same settlement
                                 # as the AM settled monthly index options (root SPX, root NDX; not SPXW or NDXP).
    "SPX": {"root": "ES", "symbol": "/ES", "also": ("/MES",), "option_root": "SPX",
            "tolerance": 3.0},   # Override vs parity warning, in index points (3 ES points).
    "NDX": {"root": "MNQ", "symbol": "/MNQ", "also": ("/NQ",), "option_root": "NDX",
            "tolerance": 12.0},  # 3 ES points scaled by the NDX/SPX price ratio: 29,844 / 7,751 = 3.85 in the Cboe
                                 # payloads of 2026-10-21 11:59 ET, so 11.6, rounded up to 12.
}
ETF_INDEX = {"SPY": "SPX", "QQQ": "NDX"}  # ETF levels convert to index points by the index/ETF price ratio first.
FUTURES_MONTHS = {3: "H", 6: "M", 9: "U", 12: "Z"}  # Quarterly cycle and the exchange month codes.
FUTURES_ROLL_DAYS = 4            # Roll rule: the front contract switches to the next quarter ON the roll date, CME's
                                 # customary U.S. equity index roll: the Monday before the third Friday, 4 calendar days
                                 # earlier (cmegroup.com/trading/equity-index/rolldates.html, read 2026-10-25). Counted
                                 # from the third Friday, so a holiday that moves the expiry to Thursday does not move it.
FUTURES_RATE = 0.0418            # r in the parity forward F = K + e^(rT) (C - P): the 13 week Treasury bill, coupon
                                 # equivalent, 4.18% on 2026-10-20 (home.treasury.gov, Daily Treasury Bill Rates).
                                 # Strikes are picked nearest the forward, where C - P is near 0, so r barely matters:
                                 # on the Dec 2026 chains at 11:59 ET 2026-10-21, r = 0 instead moved F by 0.02 pt (SPX)
                                 # and 0.10 pt (NDX).
FUTURES_RATE_SOURCE = "13 week Treasury bill, coupon equivalent, 4.18% on 2026-10-20 (US Treasury)"
FUTURES_PARITY_STRIKES = 6       # Strikes used: the 6 nearest the forward with usable call AND put mids (mid_usable,
                                 # the same check the map uses). Median of the per-strike forwards.
FUTURES_PARITY_MIN = 3           # Fewer usable strike pairs than this: the parity basis is unavailable, never a guess.
FUTURES_PARITY_WARN_FRAC = 0.0005  # Warn when the per-strike forwards spread more than 0.05% of spot (SPX near 3.9 pt,
                                 # NDX near 15 pt). Measured 2026-10-21 11:59 ET: SPX 0.07 pt, NDX 1.10 pt.
FUTURES_TICK = 0.25              # /ES, /MES, /NQ and /MNQ all tick 0.25 index points.
FUTURES_RATIO_MAX_LAG_S = 90     # SPY/QQQ ratio: the ETF trade used must print within 90 s before the index value's time.
FUTURES_BASIS_WINDOW_S = 300     # The parity basis shown is the median over the Cboe payloads of the last 5 minutes (by payload
                                 # time), not one file. Measured 2026-10-21: two NDX payloads a minute apart (12:14:59 and
                                 # 12:15:59 ET) gave 187.86 and 193.26, because NDX option spreads run 30 to 50 points and the
                                 # per-strike forwards bounce about 4 points; SPX strikes agreed within 0.3. The true basis
                                 # only decays with carry (about 0.7 point a day on Dec SPX), so 5 minutes loses nothing real.
