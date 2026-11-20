import { useState } from "react";
import { api } from "../api/client";
import type { FuturesView } from "../api/types";
import { dayShort, nyTime, price } from "../lib/format";
import type { StoredOverride } from "../lib/futures";
import { DEMO_LABEL, Icon, Panel, Skeleton } from "./ui";
import "./FuturesPanel.css";

// Futures view: the basis, where it came from, the contract, and the "type your price" override.
// Every number is from GET /api/futures; the panel only formats. Not a futures quote: labeled an estimate.

function signedPts(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "n/a";
  return `${v >= 0 ? "+" : "-"}${Math.abs(v).toFixed(digits)}`;
}

export default function FuturesPanel({
  ticker,
  f,
  error,
  loading,
  ov,
  onOverride,
}: {
  ticker: string;
  f: FuturesView | null;
  error: Error | null;
  loading: boolean;
  ov: StoredOverride | null;
  onOverride: (o: StoredOverride | null) => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const sym = f?.contract.symbol ?? (ticker === "NDX" || ticker === "QQQ" ? "/MNQ" : "/ES");
  const root = sym.replace(/[FGHJKMNQUVXZ]\d{2}$/, "");

  const submit = async () => {
    const v = Number(text.replace(/,/g, ""));
    if (!Number.isFinite(v) || v <= 0) {
      setErr(`Type the ${root} price you see, for example ${f?.parity?.forward ? price(f.parity.forward) : "7,800.25"}.`);
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const r = await api.futures(ticker, { overridePrice: v });
      const o = r.override;
      if (!o) throw new Error("the API returned no override");
      onOverride({
        price: o.price,
        basis: o.basis,
        indexValue: o.index_value,
        indexSource: o.index_source,
        delayed: o.delayed,
        label: o.label,
        at: o.at,
        symbol: r.contract.symbol,
      });
      setText("");
    } catch (e) {
      setErr(`Could not set your price: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const title = (
    <span className="f-title">
      Futures view <span className="f-chip">Estimate</span>
    </span>
  );

  if (!f) {
    return (
      <Panel title={title} className="futures-panel">
        {error ? <p className="f-small f-amber">Futures view unavailable: {error.message}</p> : loading ? <Skeleton h={60} /> : null}
      </Panel>
    );
  }

  const p = f.parity;
  const c = f.contract;
  const b = f.basis;
  const strikes = p?.strikes ?? [];
  const k0 = strikes.length ? strikes[0].strike : null;
  const k1 = strikes.length ? strikes[strikes.length - 1].strike : null;

  return (
    <Panel title={title} right={<span>{c.symbol}</span>} className="futures-panel">
      {f.demo && (
        <span className="demo-tag" role="note">
          <Icon name="info" size={13} />
          {DEMO_LABEL}
        </span>
      )}
      <div className="f-head">
        <div>
          <small>Basis</small>
          <span className={`f-basis ${b ? "" : "na"}`}>{b ? signedPts(b.value) : "Unavailable"}</span>
          <span className="f-src">{b ? (b.source === "override" ? "your price" : "parity estimate") : "no basis"}</span>
        </div>
        <div>
          <small>Contract</small>
          <span className="f-sym">{c.symbol}</span>
          <span className="f-src">
            {c.label}
            {c.also.length ? `, same price as ${c.also.join(", ")}` : ""}
          </span>
        </div>
      </div>

      {!f.available && f.reason && <p className="f-small f-amber">{f.reason}</p>}

      <dl className="kv f-kv">
        <dt>Parity</dt>
        <dd>
          {p && p.available ? (
            <>
              F {price(p.forward)} from {p.option_root} {dayShort(p.option_expiry)} AM options (median of {strikes.length} strikes, {k0 !== null && k1 !== null ? `${k0.toLocaleString("en-US")} to ${k1.toLocaleString("en-US")}` : ""}
              {p.spread !== null ? `, spread ${p.spread.toFixed(2)}` : ""}) minus {f.index} {price(p.index_spot)} {p.source === "alpaca" ? "from its options by put-call parity (Alpaca, live)" : "from the same Cboe file"}
              {p.index_time ? ` (${nyTime(p.index_time, true)} ET)` : ""}: {signedPts(p.basis_raw)}.
              {p.basis_window && p.basis_window.payloads > 1
                ? ` Shown: the median over the last ${p.basis_window.payloads} payloads (${Math.round(p.basis_window.seconds / 60)} min), ${signedPts(p.basis)}.`
                : ""}
            </>
          ) : (
            <>Unavailable{p?.reason ? `: ${p.reason}` : ""}.</>
          )}
        </dd>
        {f.ratio && (
          <>
            <dt>{f.ratio.etf} to {f.index}</dt>
            <dd>
              x {f.ratio.value.toFixed(4)}: {f.index} {price(f.ratio.index_price)} at {nyTime(f.ratio.index_time, true)} ET over the last {f.ratio.etf} trade {price(f.ratio.etf_price)}
              {f.ratio.etf_time ? ` at ${nyTime(f.ratio.etf_time, true)}` : ""}. Same instant, so the 15 minute delay drops out.
            </dd>
          </>
        )}
        <dt>Roll</dt>
        <dd>
          Expires {dayShort(c.expiry)} (AM settlement). Moves to {c.next?.symbol ?? "the next quarter"} on {dayShort(c.roll_date)}.
        </dd>
      </dl>

      <div className="f-ov">
        <label htmlFor="f-ov-input">Your {root} price</label>
        <div className="f-ov-row">
          <input
            id="f-ov-input"
            inputMode="decimal"
            value={text}
            placeholder={ov ? price(ov.price) : "as seen in your platform"}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit();
            }}
          />
          <button type="button" className="chip" onClick={submit} disabled={busy || !text.trim()}>
            {busy ? "Setting..." : "Use"}
          </button>
          {ov && (
            <button type="button" className="chip" onClick={() => onOverride(null)} title="Back to the parity estimate">
              Clear
            </button>
          )}
        </div>
        {ov && (
          <p className="f-small">
            {price(ov.price)} minus {f.index} {price(ov.indexValue)}
            {ov.at ? ` (${nyTime(ov.at, true)} ET)` : ""} = <b>{signedPts(ov.basis)}</b>. <span className={ov.delayed ? "f-amber" : "f-muted"}>{ov.label}.</span>
          </p>
        )}
        {f.check?.warning && (
          <p className="f-small f-amber f-warn">
            Your basis differs from the parity estimate by {signedPts(f.check.diff)} points (tolerance {f.check.tolerance}). Check the price, or the index delay.
          </p>
        )}
        {err && <p className="f-small f-amber">{err}</p>}
      </div>

      {f.warnings.filter((w) => !w.includes("from the parity estimate")).map((w, i) => (
        <p key={i} className="f-small f-amber">
          {w}
        </p>
      ))}
      <p className="f-foot">{f.label}. Futures = level{f.ratio ? ` x ${f.ratio.value.toFixed(4)}` : ""} + basis, rounded to the {f.tick} tick.</p>
    </Panel>
  );
}
