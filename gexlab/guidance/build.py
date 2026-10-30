"""Deterministic trade guidance from one /api/map payload.

Grounding rule: every price in the output is either a level read from the map (call wall, put wall,
a gamma flip, a strike cluster, a 10:00 frozen level, spot) or a stated computation of map numbers
(a level plus or minus the ATR buffer, a fade zone edge at a wall plus or minus 0.25 ATR, capped at
spot). Each computation is listed in `computations` with its formula, so a reader (or a test) can
rebuild every number. Targets are map levels only, never spot, and a scenario survives only with a
reward to risk of at least config.GUIDANCE_MIN_RR from its entry's worst edge. A mixed (low
influence) map gets no scenarios at all: its levels are reference only. Nothing here is fitted or tuned on
outcomes: it is the textbook dealer-gamma reading of the map, untested until the Scorecard passes.

Inputs are the map payload plus three facts the map itself does not carry: minutes to today's
close when a 0DTE expiry is selected, the Scorecard status, and the strike range (percent of spot)
the trader is viewing.
"""
from __future__ import annotations

import math
from datetime import date

from gexlab import config

GUIDANCE_VERSION = "1"

# Knobs, each with the reason it holds that value.
ATR_BUFFER_MULT = 0.1      # Stops sit 0.1 ATR beyond the trigger level: wide enough that a one-tick poke through
                           # a level is not an invalidation, narrow enough to stay at the level (a tenth of a day).
NET_TO_GROSS_LOW = 0.10    # Below 10% net to gross the call and put sides offset more than 9 to 1, so the net that
                           # sets the regime is a small residual of two large opposite books: five times the 2% cut
                           # that config.NEUTRAL_FRAC already calls "neutral", read here as "mixed", not neutral.
CLUSTERS_TOP = 3           # Top 3 strikes by absolute net GEX inside the viewed range (spec of the feature).
DEFAULT_RANGE_PCT = 5.0    # The live map's middle range chip when the caller does not say which range is viewed.
MAX_SCENARIOS = 4          # At most 4 scenarios; fewer (or none) when the reward to risk filter drops them.
MAX_TARGETS = 2            # Two next levels in the trade direction: the first and the one behind it.
FADE_ZONE_ATR = 0.25       # A fade entry hugs the wall: wall to wall +- a quarter of a typical day toward spot
                           # but never past spot.
TARGET_GAP_ATR = 0.25      # A target must sit a quarter ATR beyond the entry's far edge: a level
                           # closer than that is noise inside the entry, not a place to take profit.
TARGET_GAP_SPOT_FRAC = 0.005  # Without an ATR the gap is 0.5% of spot.
NO_SETUPS = "No trade setups from this map right now"

FOOTNOTE = "For education only. Not financial advice."          # panel footnote and narrative
CHIP_UNTESTED = "Untested: no study tests these setups"         # a data-quality fact, shown next to the title.
                           # Always shown: the Scorecard measures the levels and the regime, never these entry, stop
                           # and target rules, so a passed Scorecard does not validate a setup (day-1 audit).

ENV_LABELS = {"trend": "Trend / acceleration", "mean_reversion": "Mean reversion / pinning",
              "mixed": "Mixed / low influence"}


# ---------------------------------------------------------------- formatting

def r2(x: float | None) -> float | None:
    return None if x is None else round(float(x), 2)


def is_index(ticker: str | None) -> bool:
    """SPX and NDX quote in index points: every PRICE is a whole number."""
    return ticker in config.CBOE_TICKERS


def rp(x: float | None, whole: bool = False) -> float | None:
    """A price (or a distance in price units) at the ticker's precision: whole index points for SPX and
    NDX, cents for everything else. Percentages, ratios and dollar GEX never go through this."""
    if x is None:
        return None
    if whole:   # half away from zero, as the web's toLocaleString does (Python's round() is half-even)
        v = float(x)
        return float(math.copysign(math.floor(abs(v) + 0.5), v)) if v else 0.0
    return round(float(x), 2)


def rp_away(x: float, direction: str, whole: bool = False) -> float:
    """A stop price rounded away from its trigger: up for a stop above (short), down for one below (long)."""
    if not whole:
        return round(float(x), 2)
    return float(math.ceil(x - 1e-9) if direction == "up" else math.floor(x + 1e-9))


def fp(x: float | None) -> str:
    """A price or number as text: 2 decimals at most, trailing zeros dropped, thousands separated."""
    if x is None:
        return "n/a"
    v = round(float(x), 2)
    s = f"{abs(v):,.2f}".rstrip("0").rstrip(".")
    return ("-" if v < 0 else "") + s


def fdate(iso: str | None) -> str | None:
    if not iso:
        return None
    d = date.fromisoformat(iso)
    return f"{d.strftime('%b')} {d.day}"


def band_text(pct: float, ticks: list[float]) -> str:
    ticks = sorted(ticks)
    if pct < ticks[0]:
        return f"below the {fp(ticks[0])}% tick"
    if pct >= ticks[-1]:
        return f"above the {fp(ticks[-1])}% tick"
    for lo, hi in zip(ticks, ticks[1:]):
        if lo <= pct < hi:
            return f"between the {fp(lo)}% and {fp(hi)}% ticks"
    return ""


def next_tick_below(pct: float, ticks: list[float]) -> float | None:
    below = [t for t in ticks if t <= pct]
    return max(below) if below else None


# ---------------------------------------------------------------- pieces

def _atr(m: dict, spot: float, comps: list[dict], whole: bool = False) -> dict | None:
    pa = (m.get("magnitude") or {}).get("per_atr") or {}
    if pa.get("atr_basis") == "own" and pa.get("atr_usd"):
        atr = rp(pa["atr_usd"], whole)
        basis = pa.get("atr_label") or f"{m['ticker']}'s 14-session ATR"
        formula = f"{basis}: {fp(atr)}"
    elif pa.get("atr_basis") == "etf" and pa.get("atr_pct"):
        atr = rp(pa["atr_pct"] * spot / 100.0, whole)
        basis = pa.get("atr_label") or "ATR% from the ETF"
        formula = f"{basis} {fp(pa['atr_pct'])}% x spot {fp(spot)} / 100 = {fp(atr)}"
    else:
        return None
    if not atr:
        return None
    raw = ATR_BUFFER_MULT * atr
    # Index points: the buffer rounds UP to a whole point, so a stop never sits closer than 0.1 ATR to its level.
    buf = float(max(1, math.ceil(raw - 1e-9))) if whole else r2(raw)
    bformula = f"{fp(ATR_BUFFER_MULT)} x ATR {fp(atr)} = {fp(buf)}" + (
        " (rounded up to a whole index point)" if whole and abs(buf - raw) > 1e-9 else "")
    zraw = FADE_ZONE_ATR * atr
    # Index points: the fade zone rounds DOWN to a whole point (it hugs the wall), at least 1.
    zw = float(max(1, math.floor(zraw + 1e-9))) if whole else r2(zraw)
    zformula = f"{fp(FADE_ZONE_ATR)} x ATR {fp(atr)} = {fp(zw)}" + (
        " (rounded down to a whole index point)" if whole and abs(zw - zraw) > 1e-9 else "")
    comps.append({"name": "ATR", "formula": formula, "value": atr})
    comps.append({"name": "buffer", "formula": bformula, "value": buf})
    comps.append({"name": "fade zone", "formula": zformula, "value": zw})
    return {"value": atr, "basis": basis, "pct": r2(pa.get("atr_pct")), "buffer": buf,
            "buffer_mult": ATR_BUFFER_MULT, "buffer_formula": bformula,
            "fade_zone": zw, "fade_zone_formula": zformula}


def _gap(atr: dict | None, spot: float, comps: list[dict], whole: bool = False) -> tuple[float, str]:
    """Minimum distance from the entry's far edge to a target: 0.25 ATR, else 0.5% of spot.
    Index points round UP (a target is never closer than the rule)."""
    if atr:
        raw, src = TARGET_GAP_ATR * atr["value"], f"{fp(TARGET_GAP_ATR)} x ATR {fp(atr['value'])}"
    else:
        raw, src = TARGET_GAP_SPOT_FRAC * spot, f"{fp(TARGET_GAP_SPOT_FRAC * 100)}% of spot {fp(spot)} (no ATR)"
    gap = float(math.ceil(raw - 1e-9)) if whole else r2(raw)
    formula = f"{src} = {fp(gap)}" + (" (rounded up to a whole index point)" if whole and abs(gap - raw) > 1e-9 else "")
    comps.append({"name": "target gap", "formula": formula, "value": gap})
    return gap, formula


def _side(dist: float) -> str:
    return "at" if abs(dist) < 0.005 else ("above" if dist > 0 else "below")


def _level(key: str, label: str, price: float, spot: float, whole: bool = False, **extra) -> dict:
    p = rp(price, whole)
    d = rp(p - spot, whole)
    return {"key": key, "label": label, "price": p, "distance": d,
            "distance_pct": r2(d / spot * 100.0) if spot else None, "side": _side(d), **extra}


def _clusters(m: dict, spot: float, range_pct: float, taken: set[float], whole: bool = False) -> list[dict]:
    lo, hi = spot * (1 - range_pct / 100.0), spot * (1 + range_pct / 100.0)
    rows = [s for s in m.get("strikes") or [] if lo <= s["strike"] <= hi and s.get("net_gex")]
    rows.sort(key=lambda s: (-abs(s["net_gex"]), s["strike"]))
    out = []
    for s in rows:
        p = rp(s["strike"], whole)
        if p in taken:
            continue
        b = r2(s["net_gex"] / 1e9)
        sign = "positive" if s["net_gex"] > 0 else "negative"
        out.append(_level("cluster", f"{sign} cluster", p, spot, whole, net_gex_billions=b, sign=sign))
        taken.add(p)
        if len(out) >= CLUSTERS_TOP:
            break
    return out


def _merge(levels: list[dict]) -> list[dict]:
    """One entry per price; labels of levels at the same price are joined."""
    by: dict[float, dict] = {}
    for lv in levels:
        if lv["price"] in by:
            by[lv["price"]]["label"] += " / " + lv["label"]
            by[lv["price"]].setdefault("also", []).append(lv["key"])
        else:
            by[lv["price"]] = dict(lv)
    return sorted(by.values(), key=lambda x: -x["price"])


# ---------------------------------------------------------------- scenarios

class _Ctx:
    def __init__(self, spot: float, tradable: list[dict], buf: float | None, flips: list[float],
                 whole: bool = False, zone_w: float | None = None, gap: float = 0.0,
                 pool: list[dict] | None = None):
        self.whole = whole
        self.spot = spot
        self.lv = tradable          # triggers and no-ATR stops: live levels only
        self.pool = pool if pool is not None else tradable   # targets: live levels plus the 10:00 frozen levels
        self.buf = buf
        self.zone_w = zone_w        # fade zone width (0.25 ATR), None without an ATR
        self.gap = gap              # a target sits at least this far beyond the entry's far edge
        self.flips = flips

    def above(self, p: float) -> list[dict]:
        return sorted([x for x in self.lv if x["price"] > p + 1e-9], key=lambda x: x["price"])

    def below(self, p: float) -> list[dict]:
        return sorted([x for x in self.lv if x["price"] < p - 1e-9], key=lambda x: -x["price"])

    def stop(self, lvl: dict, direction: str) -> dict | None:
        """Invalidation beyond the trigger level: level +- buffer, else the nearest level on that side."""
        p = lvl["price"]
        if self.buf:
            sp = rp_away(p + self.buf if direction == "up" else p - self.buf, direction, self.whole)
            return {"price": sp, "basis": f"{lvl['label']} {fp(p)} {'+' if direction == 'up' else '-'} buffer {fp(self.buf)}"}
        nxt = (self.above(p) if direction == "up" else self.below(p))
        if not nxt:
            return None
        return {"price": nxt[0]["price"], "basis": f"next level {'above' if direction == 'up' else 'below'}: "
                                                  f"{nxt[0]['label']} {fp(nxt[0]['price'])}"}

    def zone(self, lvl: dict, toward: str) -> dict:
        """Breakout entry zone: the trigger to trigger + buffer in the trade direction (zero width without ATR)."""
        p = lvl["price"]
        if not self.buf:
            return {"low": p, "high": p, "basis": f"at the {lvl['label']} {fp(p)}"}
        if toward == "up":
            return {"low": p, "high": rp(p + self.buf, self.whole), "basis": f"{lvl['label']} {fp(p)} to {fp(p)} + buffer {fp(self.buf)}"}
        return {"low": rp(p - self.buf, self.whole), "high": p, "basis": f"{lvl['label']} {fp(p)} - buffer {fp(self.buf)} to {fp(p)}"}

    def fade_zone(self, wall: dict, toward: str) -> dict:
        """Fade entry zone hugging the wall: wall to wall +- 0.25 ATR toward spot, never past spot."""
        p = wall["price"]
        if not self.zone_w:
            return {"low": p, "high": p, "basis": f"at the {wall['label']} {fp(p)}"}
        if toward == "up":
            hi = rp(p + self.zone_w, self.whole)
            cap = hi > self.spot
            hi = min(hi, self.spot)
            return {"low": p, "high": hi, "basis": f"{wall['label']} {fp(p)} to {fp(p)} + fade zone {fp(self.zone_w)}"
                                                  + (f", capped at spot {fp(self.spot)}" if cap else "")}
        lo = rp(p - self.zone_w, self.whole)
        cap = lo < self.spot
        lo = max(lo, self.spot)
        return {"low": lo, "high": p, "basis": f"{wall['label']} {fp(p)} - fade zone {fp(self.zone_w)} to {fp(p)}"
                                               + (f", capped at spot {fp(self.spot)}" if cap else "")}

    def targets(self, direction: str, edge: float, limit: float | None = None) -> list[dict]:
        """Map levels (never spot) at least one target gap beyond the entry's far edge, nearest first.
        `limit` bounds a fade at the opposite wall: mean reversion aims inside the walls."""
        if direction == "up":
            ok = [x for x in self.pool if x["price"] >= edge + self.gap - 1e-9
                  and (limit is None or x["price"] <= limit + 1e-9)]
            ok.sort(key=lambda x: x["price"])
        else:
            ok = [x for x in self.pool if x["price"] <= edge - self.gap + 1e-9
                  and (limit is None or x["price"] >= limit - 1e-9)]
            ok.sort(key=lambda x: -x["price"])
        ok = [x for x in ok if abs(x["price"] - self.spot) > 1e-9]
        return [{"price": x["price"], "label": x["label"]} for x in ok[:MAX_TARGETS]]


def _scn(sid, name, trigger, bias, entry, stop, targets, why, where=None) -> dict:
    s = {"id": sid, "name": name, "trigger": trigger, "bias": bias, "entry": entry, "stop": stop,
         "targets": targets, "why": why, "conviction": "normal", "rr": None, "rr_basis": None}
    if where:
        s["_where"] = where
    return s


def _breakdown(c: _Ctx) -> dict | None:
    below = c.below(c.spot)
    if not below:
        return None
    L = below[0]
    stop = c.stop(L, "up")
    if not stop:
        return None
    entry = c.zone(L, "down")
    tg = c.targets("down", entry["low"])
    return _scn("breakdown", f"Break below the {L['label']}",
                f"Price trades below the {L['label']} at {fp(L['price'])}"
                + (f" by the {fp(c.buf)} buffer" if c.buf else ""),
                "short", entry, stop, tg,
                "Negative gamma: the model has dealers selling into declines, so a break of a level tends to "
                "extend toward the next one" + (f" ({tg[0]['label']} {fp(tg[0]['price'])})." if tg else "."),
                f"on a break below the {L['label']}")


def _breakout(c: _Ctx) -> dict | None:
    above = c.above(c.spot)
    if not above:
        return None
    U = above[0]
    stop = c.stop(U, "down")
    if not stop:
        return None
    entry = c.zone(U, "up")
    tg = c.targets("up", entry["high"])
    return _scn("breakout", f"Break above the {U['label']}",
                f"Price trades above the {U['label']} at {fp(U['price'])}"
                + (f" by the {fp(c.buf)} buffer" if c.buf else ""),
                "long", entry, stop, tg,
                "Negative gamma: the model has dealers buying into rallies, so a break of a level tends to "
                "extend toward the next one" + (f" ({tg[0]['label']} {fp(tg[0]['price'])})." if tg else "."),
                f"on a break above the {U['label']}")


def _fade(c: _Ctx, wall: dict | None, kind: str, other: dict | None) -> dict | None:
    """Positive gamma: fade a wall back toward the levels inside the walls."""
    if not wall:
        return None
    if kind == "call" and not wall["price"] > c.spot:
        return None
    if kind == "put" and not wall["price"] < c.spot:
        return None
    lim = other["price"] if other else None
    if kind == "call":
        entry = c.fade_zone(wall, "down")
        stop = c.stop(wall, "up")
        tg = c.targets("down", entry["low"], lim if lim is not None and lim < entry["low"] else None)
        bias, verb = "short", "rallies into"
    else:
        entry = c.fade_zone(wall, "up")
        stop = c.stop(wall, "down")
        tg = c.targets("up", entry["high"], lim if lim is not None and lim > entry["high"] else None)
        bias, verb = "long", "drops into"
    if not stop:
        return None
    return _scn(f"fade_{kind}", f"Fade the {wall['label']}",
                f"Price {verb} the {wall['label']} at {fp(wall['price'])} and stalls there",
                bias, entry, stop, tg,
                "Positive gamma: the model has dealers selling rallies and buying dips, which tends to hold price "
                f"inside the walls and pull it back toward the levels between them. The {wall['label']} is the "
                f"largest {kind} GEX strike in range.", f"at the {wall['label']}")


def curve_sign(m: dict, price: float, direction: str) -> str | None:
    """Sign of the map's signed GEX curve just beyond `price` in `direction` (up | down), read from
    profile_curve: positive | negative | None (no curve, or no point beyond the price)."""
    pts = sorted(m.get("profile_curve") or [], key=lambda p: p["spot"])
    beyond = [p for p in pts if p["spot"] > price + 1e-9] if direction == "up" else \
        [p for p in reversed(pts) if p["spot"] < price - 1e-9]
    for p in beyond:
        if p["net_gex"]:
            return "positive" if p["net_gex"] > 0 else "negative"
    return None


def _flip_cross(c: _Ctx, regime: str, flip: dict | None, far: str | None = None) -> dict | None:
    """The regime-change scenario at the nearest gamma flip. `far` is the curve's sign just beyond the
    flip (curve_sign); a scenario is written only for the side the curve actually turns to, so crossing
    an overhead flip from a positive map reads as entering NEGATIVE gamma, never the damped side."""
    if not flip:
        return None
    f = flip["price"]
    if far is not None and far == regime:
        return None                        # no sign change across this point: nothing turns there
    if regime == "positive" and f < c.spot:
        stop = c.stop(flip, "up")
        if not stop:
            return None
        entry = c.zone(flip, "down")
        return _scn("flip_loss", "Loss of the gamma flip",
                    f"Price trades below the gamma flip at {fp(f)}" + (f" by the {fp(c.buf)} buffer" if c.buf else ""),
                    "short", entry, stop, c.targets("down", entry["low"]),
                    "Below the flip the model turns negative: dealer hedging switches from damping moves to adding "
                    "to them, so the pin reading no longer holds and declines can extend.",
                    "on a loss of the gamma flip")
    if regime == "positive" and f > c.spot:
        stop = c.stop(flip, "down")
        if not stop:
            return None
        entry = c.zone(flip, "up")
        return _scn("flip_break_up", "Break above the gamma flip",
                    f"Price trades above the gamma flip at {fp(f)}" + (f" by the {fp(c.buf)} buffer" if c.buf else ""),
                    "long", entry, stop, c.targets("up", entry["high"]),
                    "Above the flip the model turns negative: dealer hedging switches from damping moves to adding "
                    "to them, so a rally through it can extend.", "on a break above the gamma flip")
    if regime == "negative":
        side = "above" if f > c.spot else "below"
        return _scn("flip_cross", "Cross of the gamma flip",
                    f"Price crosses {side} the gamma flip at {fp(f)}", "flat", None, None, [],
                    "Across the flip the model turns positive: trend trades lose the hedge-flow tailwind and the "
                    "map reads as mean reversion. Stand aside from continuation trades there and reassess.")
    return None


def _range(c: _Ctx) -> dict | None:
    lo = c.below(c.spot)
    hi = c.above(c.spot)
    if not lo or not hi:
        return None
    return _scn("range", "No edge between levels",
                f"Price stays between the {lo[0]['label']} at {fp(lo[0]['price'])} and the {hi[0]['label']} at "
                f"{fp(hi[0]['price'])}", "flat", None, None, [],
                "Inside the nearest levels the map gives no trade: wait for price to reach a level.")


def rr_of(s: dict) -> float | None:
    """Reward to risk from the entry zone's worst edge (the one closest to the target) to target 1 vs to the stop."""
    if s["bias"] not in ("long", "short") or not s["entry"] or not s["stop"] or not s["targets"]:
        return None
    worst = s["entry"]["high"] if s["bias"] == "long" else s["entry"]["low"]
    risk = abs(worst - s["stop"]["price"])
    if risk <= 0:
        return None
    return abs(s["targets"][0]["price"] - worst) / risk


def fmt_rr(rr: float, passed: bool) -> float:
    """One decimal, except a failing ratio is never shown at or above the threshold by rounding."""
    v = round(rr, 1)
    if not passed and v >= config.GUIDANCE_MIN_RR:
        v = math.floor(rr * 100) / 100.0
    return v


def _vet(s: dict, c: _Ctx, whole: bool, notes: list[str]) -> dict | None:
    """Keep a traded scenario only with a map-level target and a reward to risk of at least GUIDANCE_MIN_RR."""
    where = s.pop("_where", None) or s["name"].lower()
    if s["bias"] == "flat":
        return s
    if not s["targets"]:
        notes.append(f"No setup {where}: no map level sits {fp(c.gap)} or more beyond the entry for a target")
        return None
    rr = rr_of(s)
    if rr is None:
        return None
    worst = s["entry"]["high"] if s["bias"] == "long" else s["entry"]["low"]
    t1, stop = s["targets"][0]["price"], s["stop"]["price"]
    ok = rr >= config.GUIDANCE_MIN_RR - 1e-9
    shown = fmt_rr(rr, ok)
    if not ok:
        notes.append(f"No setup {where}: reward smaller than risk ({fp(shown)} to 1)")
        return None
    s["rr"] = shown
    s["rr_basis"] = (f"reward {fp(rp(abs(t1 - worst), whole))} (entry edge {fp(worst)} to target {fp(t1)}) / "
                     f"risk {fp(rp(abs(worst - stop), whole))} (entry edge {fp(worst)} to stop {fp(stop)})")
    return s


def scenarios(env: str, regime: str, c: _Ctx, walls: dict, flip: dict | None,
              notes: list[str] | None = None, far: str | None = None) -> list[dict]:
    """Scenarios for a trend or mean-reversion map. A mixed (low influence) map gets none: the caller says so."""
    notes = notes if notes is not None else []
    out: list[dict | None]
    if env == "trend":
        out = [_breakdown(c), _breakout(c), _flip_cross(c, regime, flip, far)]
    elif env == "mean_reversion":
        out = [_fade(c, walls.get("call"), "call", walls.get("put")),
               _fade(c, walls.get("put"), "put", walls.get("call")),
               _flip_cross(c, regime, flip, far)]
    else:
        return []
    res = [v for v in (_vet(s, c, c.whole, notes) for s in out if s) if v]
    if not any(s["bias"] != "flat" for s in res):
        return []
    if env == "mean_reversion" and sum(1 for s in res if s["id"].startswith("fade")) < 2:
        r = _range(c)
        if r:
            res.append(r)
    return res[:MAX_SCENARIOS]


# ---------------------------------------------------------------- top level

def build(m: dict, *, zero_dte: dict | None = None, scorecard: dict | None = None,
          range_pct: float | None = None, signed_view: bool = True) -> dict:
    """The guidance object for one map payload (the signed map; `signed_view` is what the UI shows)."""
    t = m["ticker"]
    whole = is_index(t)
    spot = rp(m["spot"]["price"], whole)
    lv = m.get("levels") or {}
    mg = m.get("magnitude") or {}
    gross = mg.get("gross") or {}
    regime = lv.get("regime") or "neutral"
    no_quotes = (m.get("quotes") or {}).get("usable") is False    # the map's regime is null, not neutral
    range_pct = float(range_pct or DEFAULT_RANGE_PCT)
    comps: list[dict] = []
    feed = m.get("feed") or {}
    synthetic = feed.get("kind") == "demo"

    # ---- size relative to ADV
    ticks = list(mg.get("ticks_pct") or config.MAGNITUDE_TICKS)
    net = (m.get("totals") or {}).get("net_gex")
    net_b = r2(net / 1e9) if net is not None else None
    pct_adv = r2(mg.get("pct_of_adv"))
    pct_adv_atr = r2((mg.get("per_atr") or {}).get("pct_of_adv_per_atr"))
    ntg = gross.get("net_to_gross")
    ntg_pct = r2(ntg * 100.0) if ntg is not None else None
    band = band_text(mg["pct_of_adv"], ticks) if mg.get("pct_of_adv") is not None else None
    size_parts = [f"Net GEX is {fp(net_b)} billion per 1% move" if net_b is not None else "Net GEX is unavailable"]
    if pct_adv is not None:
        size_parts.append(f"{fp(pct_adv)}% of {t}'s average daily dollar volume ({band})")
    elif mg.get("adv_basis") == "none":
        size_parts.append(f"with no daily volume reference for {t} (index hedging trades mostly in futures)")
    if pct_adv_atr is not None:
        size_parts.append(f"{fp(pct_adv_atr)}% for a 1 ATR move")
    if ntg_pct is not None:
        size_parts.append(f"net to gross {fp(ntg_pct)}%")
    size_text = ", ".join(size_parts) + "."

    # ---- environment
    low_size = mg.get("pct_of_adv") is not None and mg["pct_of_adv"] < min(ticks)
    low_ntg = ntg is not None and ntg < NET_TO_GROSS_LOW
    reasons = []
    if no_quotes:
        reasons.append("the map has no usable quotes yet, so the regime is unknown")
    elif regime == "neutral":
        reasons.append("the regime is neutral: net GEX is under 2% of gross at spot")
    if low_size:
        reasons.append(f"net GEX is {fp(pct_adv)}% of daily volume, below the {fp(min(ticks))}% tick, so the modeled "
                       "hedge is usually lost in normal flow")
    if low_ntg:
        reasons.append(f"net to gross is {fp(ntg_pct)}%, under {fp(NET_TO_GROSS_LOW * 100)}%: calls and puts largely "
                       "offset, so the net is a small residual")
    influence_note = None
    if reasons:
        env = "mixed"
        why = "Mixed because " + "; ".join(reasons) + "."
        if low_size:
            basis = f"hedging is {fp(pct_adv)}% of daily dollar volume per 1% move"
        elif low_ntg:
            basis = f"net to gross is {fp(ntg_pct)}%, calls and puts largely offset"
        else:
            basis = "the regime is neutral, net GEX is under 2% of gross at spot"
        influence_note = f"GEX influence is low for {t} today ({basis}): levels are for reference only."
    elif regime == "negative":
        env = "trend"
        why = (f"Negative gamma: spot {fp(spot)} is on the side of the flip where the model has dealers trading with "
               f"the move, and the hedge is {fp(pct_adv)}% of daily volume." if pct_adv is not None else
               f"Negative gamma: spot {fp(spot)} is on the side of the flip where the model has dealers trading with "
               "the move.")
    else:
        env = "mean_reversion"
        why = (f"Positive gamma: the model has dealers trading against the move, and the hedge is {fp(pct_adv)}% of "
               "daily volume, large enough to matter." if pct_adv is not None else
               "Positive gamma: the model has dealers trading against the move.")

    # ---- ATR and the buffer
    atr = _atr(m, spot, comps, whole)

    # ---- levels
    cw, pw = lv.get("call_wall"), lv.get("put_wall")
    flips = sorted(lv.get("zero_gamma") or [])
    nz = lv.get("nearest_zero_gamma")
    tradable: list[dict] = []
    walls: dict[str, dict] = {}
    if cw is not None:
        walls["call"] = _level("call_wall", "call wall", cw, spot, whole)
        tradable.append(walls["call"])
    if pw is not None:
        walls["put"] = _level("put_wall", "put wall", pw, spot, whole)
        tradable.append(walls["put"])
    flip_lv = None
    for f in flips:
        x = _level("gamma_flip", "gamma flip", f, spot, whole, nearest=(nz is not None and r2(f) == r2(nz)))
        tradable.append(x)
        if x["nearest"]:
            flip_lv = x
    if flip_lv is None and nz is not None:
        flip_lv = _level("gamma_flip", "gamma flip", nz, spot, whole, nearest=True)
        tradable.append(flip_lv)
    taken = {x["price"] for x in tradable}
    tradable += _clusters(m, spot, range_pct, taken, whole)
    tradable = _merge(tradable)

    # 10:00 frozen levels (day-1 audit): only an ELIGIBLE capture is used, and only a comparable one feeds
    # targets and "moved" lines. The capture covers every expiry 0 to 7 DTE on its model version; a view
    # of fewer expiries or another model is a different map, so its 10:00 levels are reference only.
    frozen = m.get("frozen_today") or None
    frozen_levels, ref_levels, shifts, frozen_notes = [], [], [], []
    es = m.get("expiry_selection") or {}
    sel_all = es.get("all", True)
    if frozen and frozen.get("status") == "eligible" and not frozen.get("excluded"):
        same_model = frozen.get("model_version") in (None, m.get("model_version") or config.MODEL_VERSION)
        now_src = (m.get("oi_source") or {}).get("kind")
        same_src = frozen.get("oi_source") in (None, now_src) or now_src is None
        # The same expiries, compared as sets (an expiry that settled since 10:00 makes them differ).
        same_exps = (frozen.get("expiries") is None or es.get("expiries") is None
                     or set(frozen["expiries"]) == set(es["expiries"]))
        comparable = sel_all and same_model and same_src and same_exps
        for key, label, now_v in (("call_wall", "call wall", cw), ("put_wall", "put wall", pw),
                                  ("nearest_zero_gamma", "gamma flip", nz)):
            fv = frozen.get(key)
            if fv is None:
                continue
            if comparable:
                if now_v is None or rp(fv, whole) != rp(now_v, whole):
                    frozen_levels.append(_level(f"frozen_{key}", f"10:00 {label}", fv, spot, whole, frozen=True))
                    if now_v is not None:
                        shifts.append(f"The {label} moved from {fp(rp(fv, whole))} at 10:00 to "
                                      f"{fp(rp(now_v, whole))} now.")
            else:
                ref_levels.append(_level(f"frozen_{key}", f"10:00 {label} (all expiries)", fv, spot, whole,
                                         frozen=True, reference_only=True))
        if not comparable:
            frozen_notes.append("The 10:00 levels come from a different map (expiries, model or OI source) than "
                                "this view, so they are shown for reference only and are never a target.")
    elif frozen and frozen.get("excluded"):
        frozen_notes.append("The 10:00 capture is excluded from the studies, so its levels are not used.")
    elif frozen and frozen.get("status"):
        frozen_notes.append(f"The 10:00 capture is {frozen['status']}, so its levels are not used.")
    levels_out = sorted(tradable + frozen_levels + ref_levels, key=lambda x: -x["price"])

    # ---- map quality: no setups from a stale or failed map (day-1 audit)
    blocked = []
    if no_quotes:
        blocked.append("the map has no usable quotes yet")
    if feed.get("stale"):
        blocked.append(f"the feed is stale (no successful refresh in {config.STALE_AFTER_S // 60} min)")
    elif feed.get("market_open") and (feed.get("data_age_s") or 0) > config.GUIDANCE_MAX_DATA_AGE_S:
        blocked.append(f"the vendor data is {int(feed['data_age_s'] // 60)} min old")
    if (m.get("spot") or {}).get("basis") == "prior_close":
        blocked.append("the index value is the prior close until Cboe's first live value, about 09:45 ET")
    cv = m.get("coverage") or {}
    if cv.get("gate_pct") is not None and cv.get("eligible"):
        if cv.get("gex_pct") is None:
            blocked.append("the usable share of |GEX| near spot could not be computed")
        elif cv.get("gate_pass") is False or (cv.get("gate_pass") is None and cv["gex_pct"] < cv["gate_pct"]):
            blocked.append(f"only {fp(cv['gex_pct'])}% of the |GEX| within {fp(cv.get('oi_band_pct') or 5)}% of spot "
                           f"is usable (the capture gate needs {fp(cv['gate_pct'])}%)")
    ofr = m.get("oi_freshness") or {}
    if ofr.get("fresh") is False:
        blocked.append(f"open interest is not from {fdate(ofr.get('expected'))}, the previous session")

    # ---- scenarios (frozen levels can be a target, never a trigger or a stop)
    gap, _ = _gap(atr, spot, comps, whole)
    ctx = _Ctx(spot, [x for x in tradable], atr["buffer"] if atr else None, flips, whole,
               zone_w=atr["fade_zone"] if atr else None, gap=gap, pool=_merge(tradable + frozen_levels))
    setup_notes: list[str] = []
    far = None
    if flip_lv is not None:
        far = curve_sign(m, flip_lv["price"], "up" if flip_lv["price"] > spot else "down")
    if blocked:
        scen = []
        setup_notes.append(f"{NO_SETUPS}: " + "; ".join(blocked) + ". Levels are for reference only.")
    elif env == "mixed":
        scen = []
        setup_notes.append(influence_note)
    else:
        scen = scenarios(env, regime, ctx, {k: next((x for x in tradable if x["price"] == v["price"]), v)
                                            for k, v in walls.items()},
                         next((x for x in tradable if flip_lv and x["price"] == flip_lv["price"]), None),
                         setup_notes, far)
        if not scen:
            setup_notes.insert(0, NO_SETUPS)

    # ---- what changes this
    changes = []
    if flip_lv:
        other = "negative" if regime == "positive" else "positive"
        changes.append(f"Price crossing the gamma flip at {fp(flip_lv['price'])} ({fp(abs(flip_lv['distance']))} "
                       f"{flip_lv['side']}) turns the regime {other}." if regime in ("positive", "negative") else
                       f"Price moving away from the gamma flip at {fp(flip_lv['price'])} gives the regime a sign.")
    changes.append(f"A wall shifting on a refresh: the map refreshes every {config.REFRESH_MIN_S} s; "
                   "re-read the scenarios when the call or put wall moves.")
    changes += shifts
    if zero_dte and zero_dte.get("expiry"):
        rows = m.get("by_expiry") or []
        tot_abs = sum(abs(r.get("net_gex") or 0) for r in rows)
        today_row = next((r for r in rows if r.get("expiry") == zero_dte["expiry"]), None)
        share = r2(abs(today_row["net_gex"]) / tot_abs * 100.0) if (today_row and tot_abs) else None
        if share is not None:
            comps.append({"name": "0DTE share", "formula": f"|0DTE net GEX| / sum of |net GEX| by expiry x 100 = {fp(share)}",
                          "value": share})
        mins = zero_dte.get("minutes_to_close")
        changes.append("0DTE expiry at the close" + (f" ({mins} min left)" if mins is not None else "")
                       + (f": {fp(share)}% of the selected net GEX by size expires today and leaves the map." if share is not None
                          else ": today's options leave the map."))
    if m.get("oi_date"):
        changes.append(f"Next-day OI update: open interest is from {fdate(m['oi_date'])}; the next session's OI "
                       "can move the walls and the flip.")
    if mg.get("pct_of_adv") is not None:
        nt = next_tick_below(mg["pct_of_adv"], ticks)
        if nt is not None:
            changes.append(f"Size falling below the {fp(nt)}% tick (now {fp(pct_adv)}% of daily volume) weakens the reading.")
        else:
            changes.append(f"Size is already below the {fp(min(ticks))}% tick ({fp(pct_adv)}%); a rise above it "
                           "strengthens the reading.")
    changes.append(("The feed is stale now: no successful refresh in " if feed.get("stale") else
                    "A stale feed: no successful refresh in ") + f"{config.STALE_AFTER_S // 60} min voids these levels.")

    # ---- confidence
    conf = []
    if synthetic:
        conf.append("Synthetic fixture: demo data, not a live market.")
    if feed.get("label") and not synthetic:
        conf.append(f"Feed: {feed['label']}." if not feed.get("stale") else f"Feed is stale: {feed['label']}.")
    if m.get("oi_date"):
        behind = any("one session behind" in w for w in m.get("warnings") or [])
        conf.append(f"Open interest is from {fdate(m['oi_date'])}" + (", one session behind" if behind else "")
                    + ": positioning opened today is not in the map.")
    conf.append("Sign assumption: calls add gamma and puts subtract (dealers long calls, short puts). If dealers are "
                "positioned the other way, the regime and every scenario flip.")
    if not signed_view:
        conf.append("The unsigned view does not model direction, so this guidance uses the signed map.")
    cov = (m.get("coverage") or {}).get("pct")
    if cov is not None:
        conf.append(f"Built from {fp(cov)}% of the chain within 10% of spot.")
    conf += frozen_notes
    passed = bool(scorecard and scorecard.get("passed"))
    conf.append("Untested: the Scorecard measures the levels and the regime, not these entry, stop and target "
                "rules, so none of these ideas has a measured hit rate." if passed else
                "Untested: the Scorecard gates have not passed, so none of these ideas has a measured hit rate.")

    regime_summary = (f"{t} at {fp(spot)} has no usable quotes yet, so the regime is unknown" if no_quotes else
                      f"{t} at {fp(spot)} is in {regime} gamma")
    if flip_lv and flip_lv["side"] != "at" and not no_quotes:
        spot_side = "above" if flip_lv["side"] == "below" else "below"   # the flip is below spot: spot is above it
        regime_summary += f", {fp(abs(flip_lv['distance']))} {spot_side} the gamma flip at {fp(flip_lv['price'])}"
    regime_summary += "."

    return {
        "version": GUIDANCE_VERSION,
        "ticker": t,
        "as_of": m.get("as_of"),
        "valuation_ts": (m.get("valuation") or {}).get("ts"),
        "spot": spot,
        "signed_view": signed_view,
        "expiries": [r["expiry"] for r in m.get("by_expiry") or []],
        "range_pct": range_pct,
        "synthetic": synthetic,
        "feed": {"label": feed.get("label"), "stale": bool(feed.get("stale")), "market_open": feed.get("market_open")},
        "status": {"tested": False, "chip": CHIP_UNTESTED, "scorecard_passed": passed,
                   "scorecard_lead": (scorecard or {}).get("lead") if passed else None, "footnote": FOOTNOTE},
        "regime": {"regime": None if no_quotes else regime, "summary": regime_summary, "net_gex_billions": net_b, "pct_of_adv": pct_adv,
                   "pct_of_adv_per_atr": pct_adv_atr, "net_to_gross_pct": ntg_pct, "scale_band": band,
                   "ticks_pct": ticks, "size_text": size_text},
        "environment": {"kind": env, "label": ENV_LABELS[env], "why": why},
        "atr": atr,
        "levels": levels_out,
        "scenarios": scen,
        "setup_notes": setup_notes,
        "min_rr": config.GUIDANCE_MIN_RR,
        "target_gap": gap,
        "regime_change": changes,
        "confidence": conf,
        "computations": comps,
        "zero_dte": zero_dte,
    }


# ---------------------------------------------------------------- grounding helpers (tests, validator)

def map_prices(m: dict) -> set[float]:
    """Every price the map itself states: walls, flips, strikes, frozen levels, spot."""
    lv = m.get("levels") or {}
    w = is_index(m.get("ticker"))
    out = {rp(m["spot"]["price"], w)}
    for k in ("call_wall", "put_wall", "nearest_zero_gamma"):
        if lv.get(k) is not None:
            out.add(rp(lv[k], w))
    out |= {rp(x, w) for x in lv.get("zero_gamma") or []}
    out |= {rp(s["strike"], w) for s in m.get("strikes") or []}
    fr = m.get("frozen_today") or {}
    for k in ("call_wall", "put_wall", "nearest_zero_gamma"):
        if fr.get(k) is not None:
            out.add(rp(fr[k], w))
    return out


def guidance_prices(g: dict) -> list[float]:
    """Every price the guidance states (levels, entries, stops, targets)."""
    out = [lv["price"] for lv in g["levels"]]
    for s in g["scenarios"]:
        if s["entry"]:
            out += [s["entry"]["low"], s["entry"]["high"]]
        if s["stop"]:
            out.append(s["stop"]["price"])
        out += [t["price"] for t in s["targets"]]
    return out
