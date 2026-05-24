import { useState } from "react";
import { LineChart, Line, ResponsiveContainer, Tooltip } from "recharts";
import { TrendingUp, TrendingDown, RotateCcw } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

export default function PLSummaryCard({ pl, status, onReset }) {
  const [confirming, setConfirming] = useState(false);
  const [resetting, setResetting] = useState(false);

  const daily = pl?.daily_pnl_usd ?? 0;
  const cum = pl?.cumulative_usd ?? 0;
  const series = (pl?.series || []).map((d, i) => ({
    x: i,
    v: d.cumulative_usd,
    pnl: d.pnl_usd,
  }));
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
      <div className="h-16 -mx-1" data-testid="pl-sparkline">
        {series.length > 1 ? (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={series}>
              <Line
                type="monotone"
                dataKey="v"
                stroke={cum >= 0 ? "#10b981" : "#ef4444"}
                strokeWidth={1.5}
                dot={false}
                isAnimationActive={false}
              />
              <Tooltip
                contentStyle={{ background: "#0a0a0a", border: "1px solid #262626", fontSize: 11, fontFamily: "IBM Plex Mono" }}
                labelStyle={{ display: "none" }}
                formatter={(v) => [`$${Number(v).toFixed(2)}`, "Cum"]}
              />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="h-full flex items-center justify-center text-[10px] uppercase tracking-[0.2em] text-neutral-600">
            no closed trades yet
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
