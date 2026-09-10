import { useState, useEffect, memo } from "react";
import { LineChart, Line, BarChart, Bar, Cell, YAxis, ResponsiveContainer, Tooltip } from "recharts";
import { TrendingUp, TrendingDown, RotateCcw, BarChart3, Activity } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

const TIMEFRAMES = [300, 900, 1800, 3600, 14400, 43200, 86400];

function PLSummaryCard({ pl, status, onReset }) {
  const [confirming, setConfirming] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [chart, setChart] = useState(() => localStorage.getItem("ui.pl.chart") || "line");
  const flipChart = () => setChart((c) => { const n = c === "line" ? "bars" : "line"; localStorage.setItem("ui.pl.chart", n); return n; });
  // Fixed-length time buckets, like a market chart: 5m … 1d, last 60 buckets, refetched every 30 s and on each new fill.
  const [tf, setTf] = useState(() => Number(localStorage.getItem("ui.pl.tf")) || 3600);
  const [buckets, setBuckets] = useState([]);
  const pickTf = (v) => { setTf(v); localStorage.setItem("ui.pl.tf", String(v)); };
  useEffect(() => {
    let alive = true;
    const load = () => api.plBuckets(tf, 60).then((d) => alive && setBuckets(d?.buckets || [])).catch(() => {});
    load();
    const id = setInterval(load, 30000);
    return () => { alive = false; clearInterval(id); };
  }, [tf, pl?.cumulative_usd, pl?.daily_pnl_usd]);
  const hasFills = buckets.some((b) => b.trades > 0);
  const tfLabel = (s) => (s >= 86400 ? `${s / 86400}d` : s >= 3600 ? `${s / 3600}h` : `${s / 60}m`);
  const fmtT = (t) => {
    const d = new Date(t * 1000);
    return tf >= 86400 ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
      : `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
  };
  const tip = { background: "#0a0a0a", border: "1px solid #262626", fontSize: 11, fontFamily: "IBM Plex Mono" };

  const daily = pl?.daily_pnl_usd ?? 0;
  const cum = pl?.cumulative_usd ?? 0;
  const positive = daily >= 0;

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
            onClick={flipChart}
            title={chart === "line" ? "Switch to daily red/green bars" : "Switch to cumulative line"}
            data-testid="pl-chart-toggle"
            className="text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-500 hover:text-blue-300 inline-flex items-center gap-1"
          >
            {chart === "line" ? <BarChart3 className="w-3 h-3" /> : <Activity className="w-3 h-3" />} {chart === "line" ? "bars" : "line"}
          </button>
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
      <div className="flex items-center gap-1 text-[9px] font-mono" data-testid="pl-timeframes">
        {TIMEFRAMES.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => pickTf(s)}
            data-testid={`pl-tf-${tfLabel(s)}`}
            className={`px-1.5 py-0.5 border uppercase transition-colors ${tf === s ? "border-blue-500 text-blue-300 bg-blue-950/40" : "border-neutral-800 text-neutral-500 hover:text-neutral-300"}`}
          >
            {tfLabel(s)}
          </button>
        ))}
        <span className="ml-auto text-neutral-600">{buckets.length} × {tfLabel(tf)}</span>
      </div>
      <div className="h-16 -mx-1" data-testid="pl-sparkline">
        {!hasFills ? (
          <div className="h-full flex items-center justify-center text-[10px] uppercase tracking-[0.2em] text-neutral-600">
            no closed trades in the last {buckets.length} × {tfLabel(tf)}
          </div>
        ) : chart === "bars" ? (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={buckets} barCategoryGap={1} data-testid="pl-daily-bars">
              <YAxis hide domain={[(min) => Math.min(0, min), (max) => Math.max(0, max)]} />
              <Bar dataKey="pnl_usd" isAnimationActive={false} radius={0}>
                {buckets.map((b) => <Cell key={b.t} fill={b.pnl_usd > 0 ? "#10b981" : b.pnl_usd < 0 ? "#ef4444" : "#262626"} />)}
              </Bar>
              <Tooltip
                cursor={{ fill: "#262626", opacity: 0.4 }}
                contentStyle={tip}
                labelFormatter={(_, p) => (p?.[0]?.payload ? fmtT(p[0].payload.t) : "")}
                formatter={(v, _n, p) => [`${v >= 0 ? "+" : ""}$${Number(v).toFixed(2)} · ${p?.payload?.trades ?? 0} fills (live ${Number(p?.payload?.live_usd ?? 0).toFixed(2)} / paper ${Number(p?.payload?.paper_usd ?? 0).toFixed(2)}) · cum ${Number(p?.payload?.cumulative_usd ?? 0).toFixed(2)}`, tfLabel(tf)]}
              />
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={buckets}>
              <Line type="stepAfter" dataKey="cumulative_usd" stroke={(buckets[buckets.length - 1]?.cumulative_usd ?? 0) >= 0 ? "#10b981" : "#ef4444"} strokeWidth={1.5} dot={false} isAnimationActive={false} />
              <Tooltip
                contentStyle={tip}
                labelFormatter={(_, p) => (p?.[0]?.payload ? fmtT(p[0].payload.t) : "")}
                formatter={(v, _n, p) => [`$${Number(v).toFixed(2)} (bucket ${Number(p?.payload?.pnl_usd ?? 0) >= 0 ? "+" : ""}${Number(p?.payload?.pnl_usd ?? 0).toFixed(2)}, ${p?.payload?.trades ?? 0} fills)`, "Cum"]}
              />
            </LineChart>
          </ResponsiveContainer>
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
