"""Trading calendar, settlement times and time to expiry (model v1.0).

T = calendar seconds from as_of to settlement / (365 * 86400).
PM settlement: the session close on the expiration date (16:00 ET, or the early
close the exchange calendar reports). AM settlement: 09:30 ET on the expiration
date. A contract with T <= 0 is expired and excluded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from gexlab import config

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
SECONDS_PER_YEAR = 365.0 * 86400.0


@dataclass
class Session:
    date: date
    open: time
    close: time


@dataclass
class TradingCalendar:
    """Sessions keyed by date. Built from Alpaca /v2/calendar, or weekdays as a labeled fallback.

    source: alpaca (fetched now) | alpaca_cache (validated copy from disk) | weekday_fallback
    (no exchange calendar: holidays and early closes unknown, so frozen captures are invalid).
    """
    sessions: dict[date, Session] = field(default_factory=dict)
    source: str = "alpaca"
    covered: tuple[date, date] | None = None
    fetched_at: str | None = None   # ISO UTC of the vendor fetch, for provenance

    @classmethod
    def from_alpaca(cls, rows: list[dict]) -> "TradingCalendar":
        sessions = {}
        for r in rows:
            d = date.fromisoformat(r["date"])
            sessions[d] = Session(d, _parse_hhmm(r["open"]), _parse_hhmm(r["close"]))
        cov = (min(sessions), max(sessions)) if sessions else None
        return cls(sessions, "alpaca", cov)

    @classmethod
    def weekdays(cls, start: date, end: date) -> "TradingCalendar":
        """Fallback when no calendar is reachable (DEMO, offline tests). No holidays known."""
        sessions = {}
        d = start
        while d <= end:
            if d.weekday() < 5:
                sessions[d] = Session(d, time(9, 30), time(16, 0))
            d += timedelta(days=1)
        return cls(sessions, "weekday_fallback", (start, end))

    @property
    def exchange(self) -> bool:
        """True when this is a real exchange calendar (live or a validated disk copy)."""
        return self.source in ("alpaca", "alpaca_cache")

    def covers(self, start: date, end: date) -> bool:
        return bool(self.covered) and self.covered[0] <= start and self.covered[1] >= end

    def to_json(self) -> dict:
        return {"source": "alpaca", "fetched_at": self.fetched_at,
                "covered": [self.covered[0].isoformat(), self.covered[1].isoformat()] if self.covered else None,
                "sessions": [{"date": d.isoformat(), "open": s.open.strftime("%H:%M"),
                              "close": s.close.strftime("%H:%M")} for d, s in sorted(self.sessions.items())]}

    @classmethod
    def from_json(cls, j: dict) -> "TradingCalendar":
        """Rebuild a saved exchange calendar. Raises ValueError on anything malformed."""
        if j.get("source") != "alpaca" or not j.get("covered") or not j.get("sessions"):
            raise ValueError("not a saved exchange calendar")
        cal = cls.from_alpaca(j["sessions"])
        cal.covered = (date.fromisoformat(j["covered"][0]), date.fromisoformat(j["covered"][1]))
        if not all(cal.covered[0] <= d <= cal.covered[1] for d in cal.sessions):
            raise ValueError("sessions outside the covered range")
        cal.source, cal.fetched_at = "alpaca_cache", j.get("fetched_at")
        return cal

    def provenance(self) -> dict:
        return {"source": self.source, "fetched_at": self.fetched_at,
                "covered": [self.covered[0].isoformat(), self.covered[1].isoformat()] if self.covered else None}

    def is_session(self, d: date) -> bool:
        if self.covered and (d < self.covered[0] or d > self.covered[1]):
            return d.weekday() < 5      # outside loaded range: best effort
        return d in self.sessions

    def close_time(self, d: date) -> time:
        s = self.sessions.get(d)
        return s.close if s else config.DEFAULT_SETTLE_PM

    def sessions_between(self, start: date, end: date) -> list[date]:
        return sorted(d for d in self.sessions if start <= d <= end)

    def prev_session(self, d: date) -> date:
        x = d - timedelta(days=1)
        for _ in range(15):
            if self.is_session(x):
                return x
            x -= timedelta(days=1)
        return x

    def latest_session_on_or_before(self, d: date) -> date:
        return d if self.is_session(d) else self.prev_session(d)

    def market_open(self, now_utc: datetime) -> bool:
        ny = now_utc.astimezone(NY)
        s = self.sessions.get(ny.date())
        if not s:
            return False
        return s.open <= ny.time().replace(microsecond=0) < s.close

    def open_dt(self, d: date) -> datetime | None:
        """Session open of day d (09:30 ET) as an aware UTC datetime; None when d is not a session."""
        s = self.sessions.get(d)
        return datetime.combine(d, s.open, NY).astimezone(UTC) if s else None

    def close_dt(self, d: date) -> datetime:
        """Session close of day d (16:00 ET or the early close) as an aware UTC datetime."""
        return datetime.combine(d, self.close_time(d), NY).astimezone(UTC)

    def last_close(self, now_utc: datetime) -> datetime:
        """The most recent regular-session close at or before now (today's once it has passed)."""
        d = now_utc.astimezone(NY).date()
        for _ in range(16):
            if self.is_session(d) and self.close_dt(d) <= now_utc:
                return self.close_dt(d)
            d -= timedelta(days=1)
        raise ValueError("no session close in the last 15 days")


def valuation_time(cal: "TradingCalendar | None", now: datetime,
                   quote_time: datetime | None = None) -> datetime:
    """The one clock T is measured from (model v1.0 valuation rule).

    Regular hours (or no calendar): the option quote time, never later than now (no quote
    time: now). Market closed (after hours, overnight, weekends, holidays): the last
    regular-session close (16:00 ET or the early close), or the quote time when the quotes
    are older than that close. Outside the session the quotes and IVs are the close's;
    measuring T from the wall clock shrinks T over a weekend while the IVs stay Friday's,
    which overstates near-the-money gamma (verified against Cboe's published gamma).
    """
    if cal is None or cal.market_open(now):
        return min(quote_time, now) if quote_time is not None else now
    close = cal.last_close(now)
    return min(quote_time, close) if quote_time is not None else close


def predates_session(cal: "TradingCalendar | None", value_ts: datetime | None, now: datetime) -> bool:
    """True when the market is open at now and value_ts is earlier than today's session open: a prior session's
    value served during today's session. Cboe's delayed payload carries the prior close as the index value until
    its first live value, about 09:45 ET (seen 2026-10-29: last_trade_time Oct 28 16:14:59 at 09:39 ET)."""
    if cal is None or value_ts is None or not cal.market_open(now):
        return False
    opened = cal.open_dt(now.astimezone(NY).date())
    return opened is not None and value_ts < opened


def valuation_label(valuation: datetime, at_close: bool = True) -> str:
    """'Valued at Fri Oct 16 close' (or '... 15:45 ET' when the data predate the close), New York time."""
    v = valuation.astimezone(NY)
    when = "close" if at_close else v.strftime("%H:%M") + " ET"
    return f"Valued at {v.strftime('%a')} {v.strftime('%b')} {v.day} {when}"


def _parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")[:2]
    return time(int(hh), int(mm))


def settlement_dt(expiry: date, settlement: str, cal: TradingCalendar | None = None) -> datetime:
    """Settlement instant as an aware UTC datetime."""
    if settlement == "AM":
        t = config.DEFAULT_SETTLE_AM
    else:
        t = cal.close_time(expiry) if cal else config.DEFAULT_SETTLE_PM
    return datetime.combine(expiry, t, NY).astimezone(UTC)


def time_to_expiry(as_of: datetime, expiry: date, settlement: str,
                   cal: TradingCalendar | None = None) -> float:
    """Years of calendar time to settlement. <= 0 means expired."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone aware")
    secs = (settlement_dt(expiry, settlement, cal) - as_of).total_seconds()
    return secs / SECONDS_PER_YEAR


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    first_fri = d + timedelta(days=(4 - d.weekday()) % 7)
    return first_fri + timedelta(days=14)


def expiry_kind(expiry: date) -> str:
    """monthly = third Friday, weekly = any other Friday, daily = other days."""
    if expiry == third_friday(expiry.year, expiry.month):
        return "monthly"
    if expiry.weekday() == 4:
        return "weekly"
    return "daily"


def ny_today(now_utc: datetime) -> date:
    return now_utc.astimezone(NY).date()


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
