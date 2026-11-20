import { Component, useCallback, useEffect, useState, type ReactNode } from "react";
import { API_HOST, MOCK, api } from "./api/client";
import { DemoContext, MetaContext, PresentContext, type TopMeta } from "./components/ui";
import UpdateBanner from "./components/UpdateBanner";
import { useAsync, useHashRoute } from "./lib/hooks";
import LiveMap from "./screens/LiveMap";

const NAV = [
  { path: "/live", label: "Live map" },
];

const PRESENT_KEY = "gexlab.present";

function readPresent(): boolean {
  try {
    return window.localStorage.getItem(PRESENT_KEY) === "1";
  } catch {
    return false;
  }
}

function writePresent(on: boolean) {
  try {
    window.localStorage.setItem(PRESENT_KEY, on ? "1" : "0");
  } catch {
    /* storage blocked (private window, preview): the mode still works for this page view */
  }
}

/** Presentation layout toggle. Keyboard P toggles it unless the user is typing in a field. */
function usePresentMode(): [boolean, () => void] {
  const [present, setPresent] = useState(readPresent);
  const toggle = useCallback(() => {
    setPresent((p) => {
      writePresent(!p);
      return !p;
    });
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "p" && e.key !== "P") return;
      if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.isContentEditable || ["INPUT", "SELECT", "TEXTAREA"].includes(t.tagName))) return;
      if (document.getElementById("root")?.hasAttribute("inert")) return; // a modal drawer is open
      e.preventDefault();
      toggle();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggle]);
  return [present, toggle];
}

function Mark() {
  return (
    <svg className="logo-mark" viewBox="0 0 18 18" aria-hidden>
      <rect x="9" y="2" width="8" height="3" fill="#78D202" />
      <rect x="9" y="7.5" width="4.5" height="3" fill="#78D202" />
      <rect x="2" y="7.5" width="7" height="3" fill="#7E23D3" />
      <rect x="4.5" y="13" width="4.5" height="3" fill="#7E23D3" />
      <rect x="8.5" y="0.5" width="1" height="17" fill="#FFFFFF" />
    </svg>
  );
}

class Boundary extends Component<{ children: ReactNode; resetKey: string }, { err: Error | null; key: string }> {
  state = { err: null as Error | null, key: this.props.resetKey };
  static getDerivedStateFromError(err: Error) {
    return { err };
  }
  static getDerivedStateFromProps(p: { resetKey: string }, s: { err: Error | null; key: string }) {
    return p.resetKey !== s.key ? { err: null, key: p.resetKey } : null;
  }
  render() {
    if (this.state.err)
      return (
        <div className="state" role="alert">
          <span className="kicker">Screen error</span>
          <h2>This screen hit an error while drawing.</h2>
          <p>{this.state.err.message}</p>
          <p>Switch screens or reload the page. If it repeats, the API may be returning a shape this UI does not expect.</p>
        </div>
      );
    return this.props.children;
  }
}

export default function App() {
  const { path, params, go } = useHashRoute();
  const [meta, setMeta] = useState<TopMeta>({ node: null, progress: null });
  const health = useAsync(() => api.health(), []);
  const demo = MOCK || health.data?.demo === true;
  const [present, togglePresent] = usePresentMode();

  // No ticker in the URL: SPY. Since Cboe blocked its free feed (2026-11-20) every map needs Alpaca keys, so a
  // keyless install opens on SPY and its error says where the keys go.
  const ticker = (params.get("ticker") || "SPY").toUpperCase();
  const awaitingDefault = !params.get("ticker") && health.loading;
  const route = NAV.some((n) => n.path === path) ? path : "/live";

  const setTicker = useCallback((t: string) => go(route, { ticker: t }), [go, route]);

  return (
    <DemoContext.Provider value={demo}>
      <PresentContext.Provider value={present}>
        <MetaContext.Provider value={setMeta}>
          <div className={`app ${present ? "present" : ""}`}>
            <header className="topbar">
              <span className="logo">
                <Mark />
                <span>
                  <em>Edge</em> GEX Lab
                </span>
              </span>
              <nav className="nav" aria-label="Screens">
                {NAV.map((n) => (
                  <a key={n.path} href={`#${n.path}${n.path === "/scorecard" ? "" : `?ticker=${ticker}`}`} aria-current={route === n.path ? "page" : undefined}>
                    {n.label}
                  </a>
                ))}
              </nav>
              <div className="meta">{meta.node}</div>
              <button
                type="button"
                className={`chip present-btn ${present ? "on" : ""}`}
                aria-pressed={present}
                onClick={togglePresent}
                title="Presentation layout for filming: larger type, key values above the chart, secondary panels collapsed. Shortcut: P"
              >
                Present <kbd>P</kbd>
              </button>
              {meta.progress != null && <span className="topbar-progress" style={{ transform: `scaleX(${meta.progress})` }} aria-hidden />}
            </header>

            <main className="main">
              <UpdateBanner hidden={present} />
              <Boundary resetKey={route}>
                {route === "/live" && !awaitingDefault && <LiveMap key="live" initialTicker={ticker} onTicker={setTicker} />}
              </Boundary>
            </main>

            <footer className="footer">
              {demo && <span className="badge-synth">Synthetic fixture</span>}
              <span>Research tool. Describes where option gamma sits under a stated assumption. No forecasts.</span>
              <span className="spacer" />
              {health.data && (
                <span>
                  API {health.data.status} · rate {health.data.rate.used_last_min}/{health.data.rate.cap} per min
                </span>
              )}
              {health.error && !MOCK && <span className="footer-err">API offline on {API_HOST}</span>}
              <span>Times in New York</span>
              <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer" style={{ color: "inherit" }}>
                Charts: TradingView Lightweight Charts
              </a>
            </footer>
          </div>
        </MetaContext.Provider>
      </PresentContext.Provider>
    </DemoContext.Provider>
  );
}
