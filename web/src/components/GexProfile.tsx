import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { GexMap, StrikeRow } from "../api/types";
import { distText, gex, int, px, price as fmtPrice, spotRef, strike as fmtStrike } from "../lib/format";
import { futPx, toFut, type FutConv } from "../lib/futures";
import { useMeasure } from "../lib/hooks";

export interface ProfileLevels {
  call_wall: number | null;
  put_wall: number | null;
  nearest_zero_gamma: number | null;
  zero_gamma: number[];
}

interface Props {
  map: GexMap;
  levels: ProfileLevels;
  rangePct: number;
  /** Draw "Net GEX at hypothetical prices" in its own panel, aligned on the same price scale. */
  showCurve: boolean;
  /** The wall and flip lines are the 10:00 capture, not the current chain. Each tag says so. */
  frozen: boolean;
  height: number;
  present: boolean;
  /** Futures view: level tags show the futures equivalent (the strike axis stays in index or ETF units). */
  fut?: FutConv | null;
}

const C = {
  callWall: "#78D202",
  putWall: "#9B4BE8",
  zero: "#B57BF0",
  spot: "#FFFFFF",
  axis: "#2a2235",
  grid: "#130f19",
  muted: "#8A8F98",
  muted2: "#5c5966",
};
// Ordinary bars. Brighter than the first build so they read on a phone; walls keep the full accent.
const BARS = {
  normal: { call: "#5B990A", put: "#6C2AAE" },
  present: { call: "#67AD0C", put: "#7A37C2" },
};
const FONT = {
  normal: { strike: 11, tagLabel: 9.5, tagValue: 11.5, axis: 10.5, top: 10, faint: 10, tagH: 19, minLabelPx: 17 },
  present: { strike: 16, tagLabel: 13.5, tagValue: 17, axis: 15, top: 13.5, faint: 13, tagH: 30, minLabelPx: 26 },
};

const NICE = [1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000];

function niceStep(minPx: number, pxPerUnit: number, base: number): number {
  for (const n of NICE) {
    const s = n * base;
    if (s * pxPerUnit >= minPx && Math.abs(s / base - Math.round(s / base)) < 1e-9) return s;
  }
  return base * 1000;
}

function isMultiple(v: number, step: number) {
  const r = v / step;
  return Math.abs(r - Math.round(r)) < 1e-6;
}

type Tag = {
  key: string;
  y: number;
  label: string;
  value: string;
  fill: string;
  ink: string;
  stroke?: string;
  line: string;
  dash?: string;
  w: number;
  split?: { fill: string; ink: string }; // two-tone tag: label on `fill`, value on `split.fill`
};

export default function GexProfile({ map, levels, rangePct, showCurve, frozen, height, present, fut = null }: Props) {
  const [wrapRef, size] = useMeasure<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null); // index into visible strikes
  const [curveHover, setCurveHover] = useState<number | null>(null); // index into visible curve points
  const [mouse, setMouse] = useState<{ x: number; y: number } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  const F = present ? FONT.present : FONT.normal;
  const BAR = present ? BARS.present : BARS.normal;

  // grow-in animation: bars grow from the axis when the ticker or sign mode changes
  const growKey = `${map.ticker}|${map.signed}`;
  const [grown, setGrown] = useState(false);
  useEffect(() => {
    setGrown(false);
    const t = window.setTimeout(() => setGrown(true), 40);
    return () => window.clearTimeout(t);
  }, [growKey]);

  const spot = map.spot.price;
  const W = Math.max(320, size.width);
  const H = height;
  const L = present ? 84 : 62; // strike labels live left of L
  const T = present ? 40 : 30;
  const B = H - (present ? 48 : 36); // x axis below

  const lo = spot * (1 - rangePct / 100);
  const hi = spot * (1 + rangePct / 100);
  const y = (p: number) => T + ((hi - p) / (hi - lo)) * (B - T);
  const pxPerUnit = (B - T) / (hi - lo);
  const inRange = (p: number | null): p is number => p !== null && p >= lo && p <= hi;

  // ---- level tags (built first: their width sets the right margin)
  const tags: Tag[] = [];
  const at10 = (frozen ? " 10:00" : "") + (fut ? ` ${fut.root}` : "");
  const fw = (v: number, plain: string) => (fut ? futPx(toFut(v, fut)) : plain);
  const shared = levels.call_wall !== null && levels.call_wall === levels.put_wall;
  if (shared && inRange(levels.call_wall)) {
    tags.push({
      key: "cpw",
      y: y(levels.call_wall),
      label: `CALL + PUT WALLS${at10}`,
      value: fw(levels.call_wall, fmtStrike(levels.call_wall)),
      fill: C.callWall,
      ink: "#000",
      line: C.callWall,
      w: 1.2,
      split: { fill: C.putWall, ink: "#fff" },
    });
  } else {
    if (inRange(levels.call_wall))
      tags.push({ key: "cw", y: y(levels.call_wall), label: `CALL WALL${at10}`, value: fw(levels.call_wall, fmtStrike(levels.call_wall)), fill: C.callWall, ink: "#000", line: C.callWall, w: 1 });
    if (inRange(levels.put_wall))
      tags.push({ key: "pw", y: y(levels.put_wall), label: `PUT WALL${at10}`, value: fw(levels.put_wall, fmtStrike(levels.put_wall)), fill: C.putWall, ink: "#fff", line: C.putWall, w: 1 });
  }
  if (map.signed && inRange(levels.nearest_zero_gamma))
    tags.push({ key: "zg", y: y(levels.nearest_zero_gamma), label: `GAMMA FLIP${at10}`, value: fw(levels.nearest_zero_gamma, px(levels.nearest_zero_gamma, map.ticker)), fill: "#000", ink: C.zero, stroke: C.zero, line: C.zero, dash: "2 3", w: 1.5 });
  if (spot >= lo && spot <= hi) tags.push({ key: "sp", y: y(spot), label: fut ? `SPOT ${fut.root}` : "SPOT", value: fw(spot, px(spot, map.ticker)), fill: C.spot, ink: "#000", line: C.spot, dash: "5 4", w: 1.2 });

  const labelW = (t: Tag) => t.label.length * 0.68 * F.tagLabel;
  const valueW = (t: Tag) => t.value.length * 0.62 * F.tagValue;
  const tagW = Math.max(present ? 230 : 150, ...tags.map((t) => 9 + labelW(t) + 16 + valueW(t) + 10));

  // ---- horizontal layout: [strike labels][bars][gap][curve panel][tags]
  const curve = useMemo(() => map.profile_curve.filter((p) => p.spot >= lo && p.spot <= hi), [map.profile_curve, lo, hi]);
  const curveOn = showCurve && map.signed && curve.length > 1;
  const avail = W - L - tagW - 16;
  const curveW = curveOn ? Math.round(Math.min(present ? 400 : 340, Math.max(present ? 260 : 200, avail * 0.27))) : 0;
  const gapC = curveOn ? 36 : 0;
  const R = W - tagW - 16 - curveW - gapC; // right edge of the bars
  const CL = R + gapC;
  const CR = CL + curveW;
  const TR = curveOn ? CR : R; // tags start right of TR
  const cx = L + (R - L) / 2;
  const half = (R - L) / 2 - 8;

  const rows: StrikeRow[] = useMemo(
    () => map.strikes.filter((s) => s.strike >= lo && s.strike <= hi).sort((a, b) => b.strike - a.strike),
    [map.strikes, lo, hi],
  );

  const minGap = useMemo(() => {
    let g = Infinity;
    for (let i = 1; i < rows.length; i++) g = Math.min(g, Math.abs(rows[i - 1].strike - rows[i].strike));
    return Number.isFinite(g) ? g : 1;
  }, [rows]);
  const rowPx = minGap * pxPerUnit;
  const barH = Math.max(1.5, Math.min(present ? 26 : 22, rowPx * 0.74));

  const maxAbs = useMemo(() => {
    let m = 0;
    for (const r of rows) m = Math.max(m, Math.abs(r.call_gex), Math.abs(r.put_gex));
    return m || 1;
  }, [rows]);
  const sx = (v: number) => (Math.abs(v) / maxAbs) * half;

  const labelStep = niceStep(F.minLabelPx, pxPerUnit, minGap);

  // ---- curve panel: its own dollar axis, zero in the middle, green above zero, purple below
  const curveMax = useMemo(() => curve.reduce((m, p) => Math.max(m, Math.abs(p.net_gex)), 0) || 1, [curve]);
  const ccx = CL + curveW / 2;
  const chalf = curveW / 2 - 10;
  const cxOf = (v: number) => ccx + (v / curveMax) * chalf;
  const curvePath = curveOn ? curve.map((p, i) => `${i ? "L" : "M"}${cxOf(p.net_gex).toFixed(1)} ${y(p.spot).toFixed(1)}`).join(" ") : "";

  // ---- bars x axis ticks. Unsigned: both sides are sizes, so both sides print positive.
  const ticks = half < 170 ? [-1, 0, 1] : [-1, -0.5, 0, 0.5, 1];
  const tickLabel = (k: number) => (k === 0 ? "0" : map.signed ? gex(k * maxAbs, { sign: true }) : gex(Math.abs(k) * maxAbs));

  // ---- tag collision avoidance
  const placed = [...tags].sort((a, b) => a.y - b.y);
  const tagH = F.tagH;
  const pos: Record<string, number> = {};
  for (let i = 0; i < placed.length; i++) {
    let ty = placed[i].y;
    if (i > 0) ty = Math.max(ty, pos[placed[i - 1].key] + tagH + 3);
    pos[placed[i].key] = ty;
  }
  for (let i = placed.length - 1; i >= 0; i--) {
    const limit = i === placed.length - 1 ? B : pos[placed[i + 1].key] - tagH - 3;
    pos[placed[i].key] = Math.min(pos[placed[i].key], limit);
  }

  const otherZeros = map.signed ? levels.zero_gamma.filter((z) => z !== levels.nearest_zero_gamma && inRange(z)) : [];

  // ---- hover: the bars area reads strikes, the curve panel reads the hypothetical-price curve
  function clear() {
    setHover(null);
    setCurveHover(null);
    setMouse(null);
  }
  function onMove(e: React.MouseEvent<SVGSVGElement>) {
    const rect = svgRef.current!.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    if (my < T - 6 || my > B + 6) return clear();
    const p = hi - ((my - T) / (B - T)) * (hi - lo);
    if (curveOn && mx >= CL - 4 && mx <= CR + 4) {
      let best = 0;
      for (let i = 1; i < curve.length; i++) if (Math.abs(curve[i].spot - p) < Math.abs(curve[best].spot - p)) best = i;
      setHover(null);
      setCurveHover(best);
      setMouse({ x: mx, y: my });
      return;
    }
    if (mx < 0 || mx > R + 4 || rows.length === 0) return clear();
    let best = 0;
    for (let i = 1; i < rows.length; i++) if (Math.abs(rows[i].strike - p) < Math.abs(rows[best].strike - p)) best = i;
    setCurveHover(null);
    setHover(best);
    setMouse({ x: mx, y: my });
  }
  function onKey(e: React.KeyboardEvent<SVGSVGElement>) {
    if (!rows.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const nearestSpot = rows.reduce((bi, r, i) => (Math.abs(r.strike - spot) < Math.abs(rows[bi].strike - spot) ? i : bi), 0);
      const cur = hover ?? nearestSpot;
      const next = Math.max(0, Math.min(rows.length - 1, cur + (e.key === "ArrowDown" ? 1 : -1)));
      setCurveHover(null);
      setHover(next);
      setMouse(null);
    } else if (e.key === "Escape") clear();
  }

  const hv = hover !== null ? rows[hover] : null;
  const ch = curveHover !== null ? curve[curveHover] : null;
  const tipX = mouse ? mouse.x : cx;
  const tipY = hv ? y(hv.strike) : ch ? y(ch.spot) : 0;
  const flipTip = tipX > W - 300;
  const sideNote = map.signed ? "" : " (SIZE)";
  const capY = B + (present ? 30 : 23);

  return (
    <div className="profile-wrap" ref={wrapRef} style={{ height: H }}>
      {size.width > 0 && (
        <svg
          ref={svgRef}
          className="profile-svg"
          width={W}
          height={H}
          viewBox={`0 0 ${W} ${H}`}
          role="img"
          aria-label={`${map.ticker} gamma exposure by strike${curveOn ? ", with net GEX at hypothetical prices in a separate panel" : ""}. Use the up and down arrow keys to read each strike.`}
          tabIndex={0}
          onMouseMove={onMove}
          onMouseLeave={clear}
          onKeyDown={onKey}
          onBlur={() => setHover(null)}
        >
          <defs>
            <clipPath id={`cr${uid}`}>
              <rect x={ccx} y={T - 4} width={Math.max(0, CR - ccx)} height={B - T + 8} />
            </clipPath>
            <clipPath id={`cl${uid}`}>
              <rect x={CL} y={T - 4} width={Math.max(0, ccx - CL)} height={B - T + 8} />
            </clipPath>
          </defs>

          {/* axis captions */}
          <text x={L - 12} y={T - 16} textAnchor="end" fill={C.muted2} fontSize={F.top} fontFamily="JetBrains Mono" letterSpacing="0.1em">
            STRIKE
          </text>
          <text x={L} y={T - 16} fill={C.muted} fontSize={F.top} fontFamily="JetBrains Mono" letterSpacing="0.1em">
            {map.signed ? "PUTS -" : `PUTS${sideNote}`}
          </text>
          <text x={R} y={T - 16} textAnchor="end" fill={C.muted} fontSize={F.top} fontFamily="JetBrains Mono" letterSpacing="0.1em">
            {map.signed ? "+ CALLS" : `CALLS${sideNote}`}
          </text>

          {/* strike grid + labels */}
          {rows
            .filter((r) => isMultiple(r.strike, labelStep))
            .map((r) => (
              <g key={`g${r.strike}`}>
                <line x1={L} x2={R} y1={y(r.strike)} y2={y(r.strike)} stroke={C.grid} />
                <text
                  x={L - 12}
                  y={y(r.strike) + F.strike * 0.36}
                  textAnchor="end"
                  fill={present ? "#B9BCC4" : C.muted}
                  fontSize={F.strike}
                  fontFamily="JetBrains Mono"
                  style={{ fontVariantNumeric: "tabular-nums" }}
                >
                  {fmtStrike(r.strike)}
                </text>
              </g>
            ))}

          {/* hover band */}
          {hv && <rect x={L} y={y(hv.strike) - Math.max(rowPx, 8) / 2} width={R - L} height={Math.max(rowPx, 8)} fill="#ffffff" opacity={0.045} />}

          {/* bars */}
          <g>
            {rows.map((r) => {
              const yy = y(r.strike) - barH / 2;
              const wall = levels.call_wall === r.strike;
              return (
                <rect
                  key={`c${r.strike}`}
                  className="bar"
                  x={cx}
                  y={yy}
                  height={barH}
                  rx={barH > 6 ? 1.5 : 0}
                  fill={wall ? C.callWall : BAR.call}
                  style={{ width: grown ? sx(r.call_gex) : 0 }}
                  opacity={hv && hv.strike !== r.strike ? 0.7 : 1}
                />
              );
            })}
          </g>
          <g transform={`translate(${cx} 0) scale(-1 1)`}>
            {rows.map((r) => {
              const yy = y(r.strike) - barH / 2;
              const wall = levels.put_wall === r.strike;
              return (
                <rect
                  key={`p${r.strike}`}
                  className="bar"
                  x={0}
                  y={yy}
                  height={barH}
                  rx={barH > 6 ? 1.5 : 0}
                  fill={wall ? C.putWall : BAR.put}
                  style={{ width: grown ? sx(r.put_gex) : 0 }}
                  opacity={hv && hv.strike !== r.strike ? 0.7 : 1}
                />
              );
            })}
          </g>

          {/* zero axis */}
          <line x1={cx} x2={cx} y1={T - 6} y2={B + 4} stroke={C.axis} />

          {/* separate panel: net GEX at hypothetical prices. Own dollar axis, same vertical price scale. */}
          {curveOn && (
            <g>
              <rect x={CL} y={T - 6} width={curveW} height={B - T + 12} fill="#08060c" stroke={C.axis} />
              <text x={CL} y={T - 16} fill={C.muted} fontSize={F.top} fontFamily="JetBrains Mono" letterSpacing="0.04em">
                NET GEX AT HYPOTHETICAL PRICES
              </text>
              <line x1={ccx} x2={ccx} y1={T - 6} y2={B + 6} stroke={C.axis} />
              <g className="curve" fill="none" strokeWidth={present ? 2.4 : 1.8} strokeLinejoin="round">
                <path d={curvePath} stroke={C.callWall} clipPath={`url(#cr${uid})`} />
                <path d={curvePath} stroke={C.zero} clipPath={`url(#cl${uid})`} />
              </g>
              {ch && <circle cx={cxOf(ch.net_gex)} cy={y(ch.spot)} r={present ? 5 : 3.5} fill="#fff" />}
              {[-1, 0, 1].map((k) => (
                <g key={`ct${k}`}>
                  <line x1={ccx + k * chalf} x2={ccx + k * chalf} y1={B + 6} y2={B + 10} stroke={C.axis} />
                  <text
                    x={ccx + k * chalf}
                    y={capY}
                    textAnchor={k === -1 ? "start" : k === 1 ? "end" : "middle"}
                    fill={C.muted}
                    fontSize={F.axis}
                    fontFamily="JetBrains Mono"
                  >
                    {k === 0 ? "0" : gex(k * curveMax, { sign: true })}
                  </text>
                </g>
              ))}
            </g>
          )}

          {/* faint secondary gamma flips */}
          {otherZeros.map((z) => (
            <g key={`z${z}`}>
              <line x1={L} x2={TR} y1={y(z)} y2={y(z)} stroke={C.zero} strokeDasharray="2 4" opacity={0.35} />
              <text x={L + 4} y={y(z) - 4} fill={C.zero} opacity={0.6} fontSize={F.faint} fontFamily="JetBrains Mono">
                gamma flip {fut ? `${fut.root} ${futPx(toFut(z, fut))}` : fmtPrice(z)}
              </text>
            </g>
          ))}

          {/* level lines across the bars and the curve panel, then right tags */}
          {tags.map((t) => (
            <g key={t.key} className="lvl" style={{ transform: `translateY(${t.y}px)` }}>
              <line x1={L} x2={TR + 6} y1={0} y2={0} stroke={t.line} strokeWidth={t.w} strokeDasharray={t.dash} opacity={t.key === "sp" || t.key === "zg" ? 0.95 : 0.6} />
              {t.split && <line x1={L} x2={TR + 6} y1={0} y2={0} stroke={t.split.fill} strokeWidth={t.w} strokeDasharray="6 6" opacity={0.9} />}
            </g>
          ))}
          {tags.map((t) => {
            const ty = pos[t.key];
            const x0 = TR + 10;
            const w = W - TR - 12;
            const splitX = x0 + 9 + labelW(t) + 8;
            return (
              <g key={`tag${t.key}`} className="lvl" style={{ transform: `translateY(${ty}px)` }}>
                {Math.abs(ty - t.y) > 1 && <path d={`M${TR + 6} ${t.y - ty} L${TR + 10} 0`} stroke={t.line} strokeWidth={1} fill="none" opacity={0.6} />}
                <rect x={x0} y={-tagH / 2} width={w} height={tagH} rx={2} fill={t.fill} stroke={t.stroke} />
                {t.split && <rect x={splitX} y={-tagH / 2} width={Math.max(0, x0 + w - splitX)} height={tagH} fill={t.split.fill} />}
                <text x={x0 + 8} y={F.tagLabel * 0.36} fill={t.ink} opacity={0.85} fontSize={F.tagLabel} fontWeight={700} letterSpacing="0.08em" fontFamily="JetBrains Mono">
                  {t.label}
                </text>
                <text
                  x={W - 9}
                  y={F.tagValue * 0.36}
                  textAnchor="end"
                  fill={t.split ? t.split.ink : t.ink}
                  fontSize={F.tagValue}
                  fontWeight={700}
                  fontFamily="JetBrains Mono"
                  style={{ fontVariantNumeric: "tabular-nums" }}
                >
                  {t.value}
                </text>
              </g>
            );
          })}

          {/* bars x axis */}
          <line x1={L} x2={R} y1={B + 6} y2={B + 6} stroke={C.axis} />
          {ticks.map((k) => (
            <g key={k}>
              <line x1={cx + k * half} x2={cx + k * half} y1={B + 6} y2={B + 10} stroke={C.axis} />
              <text
                x={cx + k * half}
                y={capY}
                textAnchor={k === -1 ? "start" : k === 1 ? "end" : "middle"}
                fill={C.muted}
                fontSize={F.axis}
                fontFamily="JetBrains Mono"
              >
                {tickLabel(k)}
              </text>
            </g>
          ))}
        </svg>
      )}

      {hv && (
        <div
          className="tip"
          style={{
            left: flipTip ? undefined : tipX + 18,
            right: flipTip ? W - tipX + 18 : undefined,
            top: Math.max(0, Math.min(H - 170, tipY - 20)),
          }}
        >
          <div className="t">
            Strike {fmtStrike(hv.strike)}
            <span>{distText(hv.strike, spot, map.ticker, spotRef(map.spot.basis))}</span>
          </div>
          <div className="row">
            <span>Call GEX</span>
            <span className="g">{gex(hv.call_gex, { sign: map.signed })}</span>
          </div>
          <div className="row">
            <span>{map.signed ? "Put GEX" : "Put GEX (size)"}</span>
            <span className="p">{map.signed ? gex(hv.put_gex, { sign: true }) : gex(Math.abs(hv.put_gex))}</span>
          </div>
          <div className="row">
            <span>{map.signed ? "Net" : "Total absolute"}</span>
            <span>{map.signed ? gex(hv.net_gex, { sign: true }) : gex(Math.abs(hv.call_gex) + Math.abs(hv.put_gex))}</span>
          </div>
          <div className="row">
            <span>OI calls / puts</span>
            <span>
              {int(hv.call_oi)} / {int(hv.put_oi)}
            </span>
          </div>
        </div>
      )}
      {ch && (
        <div
          className="tip"
          style={{
            left: flipTip ? undefined : tipX + 18,
            right: flipTip ? W - tipX + 18 : undefined,
            top: Math.max(0, Math.min(H - 120, tipY - 20)),
          }}
        >
          <div className="t">
            If price were {fmtPrice(ch.spot)}
            <span>{distText(ch.spot, spot, map.ticker, spotRef(map.spot.basis))}</span>
          </div>
          <div className="row">
            <span>Net GEX, recalculated</span>
            <span className={ch.net_gex >= 0 ? "g" : "p"}>{gex(ch.net_gex, { sign: true })}</span>
          </div>
          <div className="row">
            <span>IV, OI and time</span>
            <span>held fixed</span>
          </div>
        </div>
      )}
    </div>
  );
}
