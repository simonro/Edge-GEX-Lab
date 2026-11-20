import type { GexMap, LiveSpot } from "../api/types";

export type LiveReading = LiveSpot & { price: number; regime: NonNullable<LiveSpot["regime"]> };

/** SPX/NDX: the live estimate from SPY/QQQ when the map carries a reading at it, else null. */
export function liveOf(m: GexMap | null | undefined): LiveReading | null {
  const l = m?.live_spot;
  return l && l.available && typeof l.price === "number" && l.regime ? (l as LiveReading) : null;
}

/** The spot the page measures from: the live estimate when there is one, else the map's own spot. */
export function spotOf(m: GexMap): number {
  return liveOf(m)?.price ?? m.spot.price;
}
