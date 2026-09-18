import { memo, useEffect, useState } from "react";
import { MoreHorizontal } from "lucide-react";
import { api } from "@/lib/api";
import EquityChart from "../EquityChart";

const BOOKS = [["all", "ALL", "border-neutral-700 text-neutral-200"], ["scalp", "SCALP", "border-sky-700 text-sky-300"], ["hunt", "HUNT", "border-emerald-600 text-emerald-300"],
  ["runner", "RUNNER", "border-cyan-600 text-cyan-300"], ["rh_pons", "RH PONS", "border-lime-600 text-lime-300"]];
const TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h", "1d"];
const MODES = ["paper", "live", "all"];
const ls = (k, d) => localStorage.getItem(k) || d;

function Chip({ active, onClick, children, cls = "border-neutral-800 text-neutral-500", testid }) {
  return (
    <button type="button" onClick={onClick} data-testid={testid}
      className={`px-2 py-0.5 border font-mono text-[10px] tracking-[0.1em] transition-colors duration-100 ${active ? cls + " bg-neutral-900" : "border-neutral-800 text-neutral-600 hover:text-neutral-300"}`}>{children}</button>
  );
}

function EquityPanel({ refreshKey }) {
  const [view, setView] = useState(() => ls("ui.pl.view", "line"));
  const [tf, setTf] = useState(() => ls("ui.pl.tf2", "15m"));
  const [mode, setMode] = useState(() => ls("ui.pl.mode", "all"));
  const [book, setBook] = useState(() => ls("ui.pl.book", "all"));
  const [more, setMore] = useState(false);
  const [eq, setEq] = useState(null);
  const pick = (k, set) => (v) => { set(v); localStorage.setItem(k, v); };
  useEffect(() => {
    let alive = true;
    const load = () => api.plEquity(tf, mode, book).then((d) => alive && setEq(d)).catch(() => {});
    load();
    const id = setInterval(load, 15000);
    return () => { alive = false; clearInterval(id); };
  }, [tf, mode, book, refreshKey]);
  const last = eq?.equity_usd ?? 0;
  const lastPct = eq?.base_usd ? (last / eq.base_usd) * 100 : null;
  const hasData = (eq?.points?.length || 0) > 0;
  return (
    <div className="control-card h-full flex flex-col !p-0" data-testid="pl-summary-card">
      <div className="px-3 py-2 border-b border-neutral-800 flex items-center gap-2 flex-wrap">
        <span className="text-[11px] font-mono tracking-[0.2em] text-neutral-200 mr-auto">EQUITY
          <span className={`ml-3 text-xs ${last >= 0 ? "text-emerald-300" : "text-red-300"}`} data-testid="equity-last">{last >= 0 ? "+" : "-"}${Math.abs(last).toFixed(2)}{lastPct != null && <span className="text-neutral-500"> · {lastPct >= 0 ? "+" : ""}{lastPct.toFixed(1)}%</span>}</span>
        </span>
        {BOOKS.map(([b, label, cls]) => <Chip key={b} active={book === b} cls={cls} onClick={() => pick("ui.pl.book", setBook)(b)} testid={`pl-book-${b}`}>{label}</Chip>)}
        <button type="button" onClick={() => setMore((m) => !m)} data-testid="equity-more" className={`p-1 border ${more ? "border-neutral-600 text-neutral-200" : "border-neutral-800 text-neutral-500 hover:text-neutral-200"}`} title="timeframe · mode · style">
          <MoreHorizontal className="w-3.5 h-3.5" />
        </button>
      </div>
      {more && (
        <div className="px-3 py-1.5 border-b border-neutral-800 flex items-center gap-1 flex-wrap" data-testid="pl-timeframes">
          {TIMEFRAMES.map((t) => <Chip key={t} active={tf === t} cls="border-neutral-600 text-neutral-200" onClick={() => pick("ui.pl.tf2", setTf)(t)} testid={`pl-tf-${t}`}>{t}</Chip>)}
          <span className="w-px h-3 bg-neutral-800 mx-1" />
          {MODES.map((m) => <Chip key={m} active={mode === m} cls="border-neutral-600 text-neutral-200" onClick={() => pick("ui.pl.mode", setMode)(m)} testid={`pl-mode-${m}`}>{m}</Chip>)}
          <span className="w-px h-3 bg-neutral-800 mx-1" />
          <Chip active={view === "line"} cls="border-neutral-600 text-neutral-200" onClick={() => pick("ui.pl.view", setView)("line")} testid="pl-view-line">line</Chip>
          <Chip active={view === "wicks"} cls="border-neutral-600 text-neutral-200" onClick={() => pick("ui.pl.view", setView)("wicks")} testid="pl-view-wicks">wicks</Chip>
        </div>
      )}
      <div className="flex-1 px-1 py-1" data-testid="pl-sparkline">
        {hasData ? <EquityChart data={eq} view={view} tf={tf} height={300} />
          : <div className="h-[300px] flex items-center justify-center text-[10px] uppercase tracking-[0.2em] text-neutral-600">no closed {mode === "all" ? "" : mode + " "}trades{book !== "all" ? ` in ${book}` : ""} yet</div>}
      </div>
    </div>
  );
}

export default memo(EquityPanel);
