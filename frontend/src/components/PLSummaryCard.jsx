import { useState, useEffect, memo } from "react";
import { TrendingUp, TrendingDown, RotateCcw, CandlestickChart, Activity } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import EquityChart from "./EquityChart";

const TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h", "1d"];
const MODES = ["paper", "live", "all"];
const BOOKS = [["all", "all"], ["scalp", "scalp"], ["hunt", "hunt"], ["runner", "runner"], ["rh_pons", "rh"]];
const ls = (k, d) => localStorage.getItem(k) || d;

function Chip({ active, onClick, children, testid }) {
  return (
    <button type="button" onClick={onClick} data-testid={testid}
      className={`px-1.5 py-0.5 border uppercase text-[9px] font-mono transition-colors ${active ? "border-blue-500 text-blue-300 bg-blue-950/40" : "border-neutral-800 text-neutral-500 hover:text-neutral-300"}`}>
      {children}
    </button>
  );
}

function PLSummaryCard({ pl, status, onReset }) {
  const [confirming, setConfirming] = useState(false);
  const [resetting, setResetting] = useState(false);
  // Equity chart of OUR trading (closed fills walked in time + unrealised mark of open slots, refreshed ~5 s).
  const [view, setView] = useState(() => ls("ui.pl.view", "line"));          // line | wicks
  const [tf, setTf] = useState(() => ls("ui.pl.tf2", "15m"));
  const [mode, setMode] = useState(() => ls("ui.pl.mode", "all"));
  const [book, setBook] = useState(() => ls("ui.pl.book", "all"));
  const [eq, setEq] = useState(null);
  const pick = (k, set) => (v) => { set(v); localStorage.setItem(k, v); };
  useEffect(() => {
    let alive = true;
    const load = () => api.plEquity(tf, mode, book).then((d) => alive && setEq(d)).catch(() => {});
    load();
    const id = setInterval(load, 15000);
    return () => { alive = false; clearInterval(id); };
  }, [tf, mode, book, pl?.cumulative_usd]);

  const daily = pl?.daily_pnl_usd ?? 0;
  const cum = pl?.cumulative_usd ?? 0;
  const positive = daily >= 0;
  const last = eq?.equity_usd ?? 0;
  const lastPct = eq?.base_usd ? (last / eq.base_usd) * 100 : null;
  const hasData = (eq?.points?.length || 0) > 0;

  const doReset = async () => {
    setResetting(true);
    try {
      const res = await api.paperReset();
      toast.success(`Cleared ${res.deleted_trades} paper trades · kill-switch reset`);
      setConfirming(false);
      onReset && onReset();
    } catch (e) {
      const msg = e?.response?.data?.detail || e.message || "reset failed";
      toast.error(msg);
    } finally {
      setResetting(false);
    }
  };

  return (
    <div className="control-card flex flex-col gap-3" data-testid="pl-summary-card">
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-500">P/L Today</span>
        <div className="flex items-center gap-2">
          {positive ? <TrendingUp className="w-3 h-3 text-emerald-500" /> : <TrendingDown className="w-3 h-3 text-red-500" />}
          <button
            onClick={() => setConfirming(true)}
            title="Clear paper trades + reset 1d/7d view (live trades preserved on-chain)"
            data-testid="reset-paper-btn"
            className="text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-500 hover:text-amber-400 disabled:opacity-30 disabled:cursor-not-allowed inline-flex items-center gap-1"
          >
            <RotateCcw className="w-3 h-3" /> Reset
          </button>
        </div>
      </div>
      <div className={`text-3xl font-mono font-semibold ${positive ? "text-emerald-400" : "text-red-400"}`} data-testid="daily-pnl">
        {positive ? "+" : ""}${daily.toFixed(2)}
      </div>
      <div className="text-xs font-mono text-neutral-400">
        7-day cumulative: <span className={cum >= 0 ? "text-emerald-400" : "text-red-400"} data-testid="cumulative-pnl">
          {cum >= 0 ? "+" : ""}${cum.toFixed(2)}
        </span>
      </div>

      {/* equity chart controls */}
      <div className="flex flex-wrap items-center gap-1" data-testid="pl-timeframes">
        <Chip active={view === "line"} onClick={pick("ui.pl.view", setView).bind(null, "line")} testid="pl-view-line"><Activity className="w-3 h-3 inline -mt-0.5" /> line</Chip>
        <Chip active={view === "wicks"} onClick={pick("ui.pl.view", setView).bind(null, "wicks")} testid="pl-view-wicks"><CandlestickChart className="w-3 h-3 inline -mt-0.5" /> wicks</Chip>
        <span className="w-px h-3 bg-neutral-800 mx-1" />
        {TIMEFRAMES.map((t) => <Chip key={t} active={tf === t} onClick={() => pick("ui.pl.tf2", setTf)(t)} testid={`pl-tf-${t}`}>{t}</Chip>)}
        <span className="w-px h-3 bg-neutral-800 mx-1" />
        {MODES.map((m) => <Chip key={m} active={mode === m} onClick={() => pick("ui.pl.mode", setMode)(m)} testid={`pl-mode-${m}`}>{m}</Chip>)}
        <span className="w-px h-3 bg-neutral-800 mx-1" />
        {BOOKS.map(([b, label]) => <Chip key={b} active={book === b} onClick={() => pick("ui.pl.book", setBook)(b)} testid={`pl-book-${b}`}>{label}</Chip>)}
        <span className={`ml-auto font-mono text-xs ${last >= 0 ? "text-emerald-400" : "text-red-400"}`} data-testid="equity-last" title="equity now = realised + unrealised mark of open slots">
          {last >= 0 ? "+" : "-"}${Math.abs(last).toFixed(2)}{lastPct != null && <span className="text-neutral-500"> · {lastPct >= 0 ? "+" : ""}{lastPct.toFixed(2)}%</span>}
          {eq?.open_mark_usd ? <span className="text-neutral-600" data-testid="equity-open-mark"> (open {eq.open_mark_usd >= 0 ? "+" : "-"}${Math.abs(eq.open_mark_usd).toFixed(2)})</span> : null}
        </span>
      </div>
      <div className="-mx-1" data-testid="pl-sparkline">
        {hasData ? (
          <EquityChart data={eq} view={view} tf={tf} height={280} />
        ) : (
          <div className="h-[280px] flex items-center justify-center text-[10px] uppercase tracking-[0.2em] text-neutral-600">
            no closed {mode === "all" ? "" : mode + " "}trades{book !== "all" ? ` in ${book}` : ""} yet
          </div>
        )}
      </div>

      {confirming && (
        <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4" data-testid="reset-confirm-dialog">
          <div className="w-full max-w-sm control-card">
            <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-amber-400 mb-3">
              <RotateCcw className="w-3 h-3" /> Reset Paper Mode
            </div>
            <p className="text-sm text-neutral-300 mb-2">
              This will <span className="text-amber-400">clear the 1d/7d PnL view</span>:
            </p>
            <ul className="text-xs font-mono text-neutral-400 list-disc list-inside space-y-1 mb-4">
              <li>Paper trades → <span className="text-red-400">deleted</span></li>
              <li>Live trade history → <span className="text-neutral-300">hidden from view (preserved on-chain)</span></li>
              <li>Daily P/L → $0.00</li>
              <li>Kill-switch → reset</li>
              <li>Re-entry watchlist → cleared</li>
            </ul>
            <p className="text-[11px] text-neutral-500 mb-4">
              Your <span className="text-emerald-400">live trade rows stay in the DB</span> for audit.
              Active live positions keep running. The counters and 7-day chart simply start fresh from now.
            </p>
            <div className="flex gap-2">
              <button
                onClick={() => setConfirming(false)}
                data-testid="reset-cancel-btn"
                className="flex-1 px-3 py-2 border border-neutral-700 text-neutral-300 hover:bg-neutral-900 font-mono text-xs uppercase tracking-[0.15em]"
              >
                Cancel
              </button>
              <button
                onClick={doReset}
                disabled={resetting}
                data-testid="reset-confirm-btn"
                className="flex-1 px-3 py-2 border border-amber-700 text-amber-200 bg-amber-950 hover:bg-amber-900 font-mono text-xs uppercase tracking-[0.15em] disabled:opacity-40"
              >
                {resetting ? "Resetting…" : "Reset Now"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default memo(PLSummaryCard);
