"""DEX: delta exposure from open interest. A context line for positioning size only. Display only.

DEX per contract = delta * OI * multiplier * S, in dollars of the underlying (decided 2026-10-28).
  delta: Black-Scholes, r = q = 0, the same IV and the same valuation clock (T) as the GEX of that contract.
  A call's delta is positive and a put's negative, with no extra sign flip, so net DEX = call delta exposure minus
  put delta exposure and reads positive (call delta exposure larger) or negative (put delta exposure larger). The unsigned call and put totals show
  each side's size.

Open interest is the previous session's, so this sizes yesterday's positioning at today's price and IV. It is
not a live flow scoreboard. Only valid contracts (usable OI and IV) count, as in GEX.
"""
from __future__ import annotations

from dataclasses import dataclass

from gexlab.engine import bsm
from gexlab.models import Contract


@dataclass
class Dex:
    net: float          # sum of delta * OI * multiplier * S: call delta exposure minus put delta exposure
    call_abs: float     # sum of |delta| * OI * multiplier * S over calls
    put_abs: float      # same over puts
    contracts: int      # valid contracts counted


def contract_dex(delta: float, oi: float, multiplier: float, spot: float) -> float:
    return delta * oi * multiplier * spot


def compute(contracts: list[Contract], spot: float) -> Dex:
    net = call_abs = put_abs = 0.0
    n = 0
    for c in contracts:
        if not c.valid:
            continue
        n += 1
        if not c.oi or c.T is None or c.T <= 0:
            continue                       # confirmed zero OI carries no delta; finalize() already dropped expiries
        d = bsm.delta(spot, c.strike, c.T, 0.0, c.iv, c.type)
        size = abs(d) * c.oi * c.multiplier * spot
        net += contract_dex(d, c.oi, c.multiplier, spot)
        if c.type == "call":
            call_abs += size
        else:
            put_abs += size
    return Dex(net, call_abs, put_abs, n)
