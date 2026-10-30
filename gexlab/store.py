"""SQLite storage for Edge GEX Lab. One file, data/gexlab.db (gitignored).

Frozen captures are immutable: rows are inserted once and never updated, so a
level saved at 10:00 can never be rewritten by data that arrived later.

DEMO mode (GEXLAB_DEMO=1) never touches the research DB: the default path is
data/gexlab_demo.db (or GEXLAB_DEMO_DB), and GEXLAB_DB is ignored there.

Columns added after the first schema are applied by MIGRATIONS (additive
ALTER TABLE ADD COLUMN, nullable), so an existing data/gexlab.db upgrades in place.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("GEXLAB_DB", ROOT / "data" / "gexlab.db"))   # research DB at import time (legacy name)


def default_path() -> Path:
    """Read at call time so tests and the API see the current env. DEMO gets its own file."""
    if os.getenv("GEXLAB_DEMO", "0") == "1":
        return Path(os.getenv("GEXLAB_DEMO_DB") or ROOT / "data" / "gexlab_demo.db")
    return Path(os.getenv("GEXLAB_DB") or ROOT / "data" / "gexlab.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id              INTEGER PRIMARY KEY,
    ticker          TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('frozen','snapshot')),
    session_date    TEXT NOT NULL,            -- YYYY-MM-DD, New York
    requested_at    TEXT NOT NULL,            -- ISO UTC
    completed_at    TEXT,
    spot            REAL,
    spot_ts         TEXT,
    spot_source     TEXT,
    feed            TEXT,                     -- opra | indicative | cboe_delayed
    source_ts       TEXT,                     -- payload timestamp when the source supplies one (Cboe)
    model_version   TEXT NOT NULL,
    oi_date         TEXT,
    status          TEXT NOT NULL,            -- eligible | late | invalid
    status_reason   TEXT,
    eligible_count  INTEGER,
    valid_count     INTEGER,
    coverage_pct    REAL,
    net_gex         REAL,
    response_hash   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_frozen ON captures(ticker, session_date) WHERE kind = 'frozen';
CREATE INDEX IF NOT EXISTS ix_captures ON captures(ticker, session_date, kind);

CREATE TABLE IF NOT EXISTS capture_contracts (
    capture_id  INTEGER NOT NULL REFERENCES captures(id),
    symbol      TEXT NOT NULL,
    expiry      TEXT NOT NULL,
    settlement  TEXT NOT NULL,                -- AM | PM
    strike      REAL NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('call','put')),
    multiplier  INTEGER NOT NULL DEFAULT 100,
    oi          INTEGER,
    iv          REAL,
    iv_source   TEXT,                         -- indicative | solved_mid | cboe
    bid         REAL,
    ask         REAL,
    quote_ts    TEXT,
    gamma       REAL,
    gex         REAL,                         -- signed, dollars per 1% move
    valid       INTEGER NOT NULL,
    invalid_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_cc ON capture_contracts(capture_id);

CREATE TABLE IF NOT EXISTS capture_levels (
    capture_id  INTEGER NOT NULL REFERENCES captures(id),
    family      TEXT NOT NULL,                -- call_wall | put_wall | zero_gamma | round | prior_high | prior_low | prior_close | pm_high | pm_low
    price       REAL NOT NULL,
    value       REAL,                         -- GEX at the wall, or null
    is_nearest  INTEGER NOT NULL DEFAULT 0,
    definition_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cl ON capture_levels(capture_id);

CREATE TABLE IF NOT EXISTS bars (
    ticker      TEXT NOT NULL,
    ts          TEXT NOT NULL,                -- bar start, ISO UTC
    timeframe   TEXT NOT NULL,                -- 1Min | 1Day
    o REAL, h REAL, l REAL, c REAL, v REAL,
    feed        TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (ticker, ts, timeframe, feed)
);

CREATE TABLE IF NOT EXISTS placebos (
    capture_id  INTEGER NOT NULL REFERENCES captures(id),
    family      TEXT NOT NULL,
    level_price REAL NOT NULL,
    seed        INTEGER NOT NULL,
    price       REAL NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS touch_events (
    capture_id  INTEGER NOT NULL REFERENCES captures(id),
    family      TEXT NOT NULL,
    level_price REAL NOT NULL,
    is_placebo  INTEGER NOT NULL,
    touch_ts    TEXT,
    approach    TEXT,
    gap_through INTEGER NOT NULL DEFAULT 0,
    reaction_atr REAL,
    excluded_reason TEXT,
    protocol_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_behavior (
    ticker      TEXT NOT NULL,
    session_date TEXT NOT NULL,
    capture_id  INTEGER REFERENCES captures(id),
    regime      TEXT,
    trend_efficiency REAL, range_atr REAL, realized_vol REAL,
    vwap_crosses INTEGER, close_location REAL, variance_ratio REAL,
    baseline_prior_range_atr REAL, baseline_open_vol REAL,
    protocol_version TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (ticker, session_date, protocol_version)
);

CREATE TABLE IF NOT EXISTS recaps (
    ticker      TEXT NOT NULL,
    session_date TEXT NOT NULL,
    model_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    text        TEXT NOT NULL,
    source      TEXT NOT NULL,                -- ai | template
    model       TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (ticker, session_date, model_version)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- History audit fixes (additive). Fetched-through coverage per (ticker, timeframe, session date):
-- a 1-minute cache is final only when it was fetched through the exchange close.
CREATE TABLE IF NOT EXISTS bar_coverage (
    ticker          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    session_date    TEXT NOT NULL,
    feed            TEXT,
    fetched_from    TEXT NOT NULL,            -- ISO UTC request start
    fetched_through TEXT NOT NULL,            -- ISO UTC request end (clipped to now minus the SIP delay)
    session_close   TEXT,                     -- HH:MM exchange close the fetch was sized for
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (ticker, timeframe, session_date)
);

-- Immutable comparator preparation: the exact ATR and comparator inputs used for a capture,
-- committed (with the placebo sets) before any touch or behavior outcome is computed.
CREATE TABLE IF NOT EXISTS capture_inputs (
    capture_id       INTEGER PRIMARY KEY REFERENCES captures(id),
    protocol_version TEXT NOT NULL,
    definition_version TEXT NOT NULL,
    atr              REAL,
    inputs_json      TEXT NOT NULL,
    prepared_at      TEXT NOT NULL
);

-- One row per placebo set; the primary key stops a second process inserting a duplicate set.
CREATE TABLE IF NOT EXISTS placebo_sets (
    capture_id       INTEGER NOT NULL REFERENCES captures(id),
    family           TEXT NOT NULL,
    level_price      REAL NOT NULL,
    seed             INTEGER NOT NULL,
    n                INTEGER NOT NULL,
    protocol_version TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (capture_id, family)
);

-- Outcome of the explicit processing step per session: complete sessions may enter gates,
-- excluded ones never do (reason says why). No row = not processed yet (or a transient failure).
CREATE TABLE IF NOT EXISTS session_processing (
    ticker           TEXT NOT NULL,
    session_date     TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    capture_id       INTEGER,
    status           TEXT NOT NULL,           -- complete | excluded
    reason           TEXT,
    session_close    TEXT,                    -- HH:MM exchange close used
    calendar_source  TEXT,
    bars_through     TEXT,
    missing_minutes  INTEGER,
    processed_at     TEXT NOT NULL,
    PRIMARY KEY (ticker, session_date, protocol_version)
);

-- Captures left out of the studies by a declared policy, never by relabeling: the capture keeps its
-- status, digest and rows. One row per capture; the reason and policy say why (day-1 audit, 2026-10-19).
CREATE TABLE IF NOT EXISTS analysis_exclusions (
    capture_id       INTEGER PRIMARY KEY REFERENCES captures(id),
    reason           TEXT NOT NULL,
    policy           TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

-- Stored replay analysis, written only by explicit processing; GET /api/replay and /api/recap read it.
CREATE TABLE IF NOT EXISTS replays (
    ticker           TEXT NOT NULL,
    session_date     TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    processed_at     TEXT NOT NULL,
    PRIMARY KEY (ticker, session_date, protocol_version)
);
"""


# (table, column, type): additive and nullable only. Never rename or drop here.
MIGRATIONS = [
    ("captures", "input_digest", "TEXT"),          # sha256 of the canonical calculation inputs (see engine/digest.py)
    ("captures", "calendar_source", "TEXT"),       # alpaca | alpaca_cache | weekday_fallback
    ("capture_contracts", "oi_date", "TEXT"),      # per-contract open_interest_date
    ("capture_contracts", "iv_shared", "INTEGER"), # 1 when the IV in use was copied from the strike pair
    ("capture_contracts", "iv_own", "REAL"),       # the contract's own IV before sharing
    ("capture_contracts", "iv_own_source", "TEXT"),
    ("capture_contracts", "iv_donor", "TEXT"),     # donor symbol when shared
    ("capture_contracts", "iv_donor_ts", "TEXT"),  # donor quote time when shared
    ("bars", "vw", "REAL"),                        # provider bar VWAP (Alpaca `vw`); null for old rows
    ("placebos", "idx", "INTEGER"),                # draw index inside its set; unique per (capture, family)
    ("captures", "gross_gex", "REAL"),             # sum of |GEX| over valid contracts: the regime's neutral band
    ("captures", "invalid_counts", "TEXT"),        # JSON {reason: n} over the contracts inside the coverage band
    ("capture_contracts", "iv_ts", "TEXT"),        # quote time of the IV in use (null = unknown age)
    ("capture_contracts", "oi_basis", "TEXT"),     # reported | zero_never_traded | zero_vendor_null
    ("captures", "gate_version", "TEXT"),          # eligibility gate that judged the row; NULL = 1.0 (contract count)
    ("captures", "oi_coverage_pct", "REAL"),       # gate 1.1 number (diagnostic since 1.2): % of OI within +-5% valid
    ("captures", "gex_coverage_pct", "REAL"),      # gate 1.2 number: % of estimated |GEX| within +-5% that is valid
    ("captures", "oi_source", "TEXT"),             # cboe | alpaca: open interest vendor (model 1.1); NULL = not recorded
                                                   # (rows before 2026-10-19 ~12:00 ET: SPY/QQQ Alpaca, SPX/NDX Cboe)
    ("captures", "oi_label", "TEXT"),              # e.g. "OI: Cboe (prior session, inferred)" / "OI: Alpaca (Oct 15)"
    ("capture_contracts", "oi_source", "TEXT"),    # cboe | alpaca per contract (a contract Cboe lacks keeps Alpaca OI)
    ("captures", "oi_basis", "TEXT"),              # inferred | matches_alpaca | reported: how the OI date was set (gate 1.3)
    ("captures", "oi_expected_date", "TEXT"),      # the previous completed session the OI must be from (gate 1.3)
    ("captures", "oi_stale_pct", "REAL"),          # % of band OI dated other than oi_expected_date (gate 1.3: <= 1%)
    ("captures", "oi_dates", "TEXT"),              # JSON {oi_date: band OI}, per contract, so no date hides another
    ("captures", "gex_left_out", "INTEGER"),       # band contracts with no donor IV, charged at the bound (gate 1.3)
    ("captures", "gex_charged_pct", "REAL"),       # % of the band |GEX| total that is that charge
    ("session_processing", "study_eligible", "INTEGER"),  # 1 = counts toward the studies under the declared cohort
    ("session_processing", "study_reasons", "TEXT"),      # why it does not count (comma separated), null when it does
    ("captures", "index_ts", "TEXT"),              # Cboe index captures: the index value's own market-data time
                                                   # (last_trade_time); NULL before 2026-10-29 and for Alpaca feeds
]

# Indexes that depend on migrated columns: created after MIGRATIONS ran.
POST_MIGRATION_SQL = [
    # NULL idx (rows from before this column) never collide, so the index applies to old databases too.
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_placebos ON placebos(capture_id, family, idx)",
]


def migrate(conn: sqlite3.Connection) -> None:
    for table, col, typ in MIGRATIONS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    for sql in POST_MIGRATION_SQL:
        conn.execute(sql)
    conn.commit()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


@contextmanager
def session(path: Path | str | None = None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
