import { useEffect, useState, useCallback } from "react";
import { Lightbulb, Check, X, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import { toast } from "sonner";

const CONFIDENCE_STYLES = {
  high: "border-emerald-700 text-emerald-300 bg-emerald-950/30",
  medium: "border-blue-700 text-blue-300 bg-blue-950/30",
  low: "border-neutral-700 text-neutral-300 bg-neutral-900/50",
  info: "border-amber-700 text-amber-300 bg-amber-950/30",
};

const FIELD_LABELS = {
  take_profit_pct: "Take Profit (%)",
  stop_loss_pct: "Stop Loss (%)",
  trailing_stop_pct: "Trailing Stop (%)",
  exit_slippage_bps: "Exit Slip (bps)",
  hold_max_seconds: "Max Hold (s)",
  min_curve_liquidity_sol: "Min Liquidity (SOL)",
  min_buyers_for_entry: "Min Buyers",
  max_concurrent_positions: "Max Positions",
  scanner_min_growth_pct: "Scanner Min Growth (%)",
  scanner_min_recent_inflow_sol: "Scanner Min Inflow (SOL)",
  scanner_min_new_buyers: "Scanner Min Buyers",
  scanner_interval_s: "Scanner Interval (s)",
};

export default function SuggestionsCard({ onApplied }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [dismissed, setDismissed] = useState(new Set());
  const [applying, setApplying] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api.suggestions();
      setData(d);
    } catch (e) {
      toast.error("Failed to load suggestions");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const apply = async (s, idx) => {
    if (!s.field) return;
    setApplying(idx);
    try {
      await api.applySuggestion(s.field, s.suggested);
      toast.success(`${FIELD_LABELS[s.field] || s.field} → ${s.suggested}`);
      setDismissed((prev) => new Set(prev).add(idx));
      onApplied && onApplied();
      // Re-load to show recalculated suggestions
      setTimeout(load, 800);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Apply failed");
    } finally {
      setApplying(null);
    }
  };

  const stats = data?.stats;
  const items = (data?.suggestions || []).map((s, i) => ({ ...s, idx: i }))
    .filter((s) => !dismissed.has(s.idx));

  return (
    <div className="control-card" data-testid="suggestions-card">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
          <Lightbulb className="w-3 h-3" /> Suggestions ({items.length})
        </div>
        <button
          onClick={load}
          disabled={loading}
          data-testid="refresh-suggestions-btn"
          className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 hover:text-neutral-200 inline-flex items-center gap-1 disabled:opacity-40"
        >
          <RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} />
          Re-analyse
        </button>
      </div>

      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-3 text-[10px] font-mono">
          <Stat label="trades analysed" value={data.trades_analysed} />
          <Stat label="win rate" value={`${stats.win_rate_pct}%`} hl={stats.win_rate_pct >= 40 ? "good" : stats.win_rate_pct >= 25 ? "warn" : "bad"} />
          <Stat label="avg winner" value={`+${stats.avg_winner_pct}%`} hl="good" />
          <Stat label="avg loser" value={`${stats.avg_loser_pct}%`} hl="bad" />
        </div>
      )}

      {items.length === 0 ? (
        <div className="text-center py-6 text-[10px] uppercase tracking-[0.2em] text-neutral-600">
          {loading ? "analysing..." : "no active suggestions"}
        </div>
      ) : (
        <ul className="space-y-2">
          {items.map((s) => {
            const cls = CONFIDENCE_STYLES[s.confidence] || CONFIDENCE_STYLES.low;
            const fieldLabel = s.field ? (FIELD_LABELS[s.field] || s.field) : null;
            return (
              <li
                key={s.idx}
                data-testid={`suggestion-row-${s.idx}`}
                className={`border ${cls} px-3 py-2 flex items-start gap-3`}
              >
                <div className="min-w-0 flex-1">
                  {fieldLabel && (
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-xs font-mono font-semibold">{fieldLabel}</span>
                      <span className="text-[10px] font-mono text-neutral-500">
                        {s.current} <span className="text-neutral-600">→</span> <span className="text-neutral-100">{s.suggested}</span>
                      </span>
                      <span className="text-[9px] uppercase tracking-wider opacity-70 ml-auto">{s.confidence}</span>
                    </div>
                  )}
                  <p className="text-[11px] font-mono leading-relaxed text-neutral-300">{s.reason}</p>
                  {!fieldLabel && (
                    <div className="mt-1 text-[9px] uppercase tracking-wider opacity-70">{s.confidence}</div>
                  )}
                </div>
                <div className="flex flex-col gap-1 shrink-0">
                  {s.field && (
                    <button
                      onClick={() => apply(s, s.idx)}
                      disabled={applying === s.idx}
                      data-testid={`apply-suggestion-${s.idx}`}
                      title="Apply"
                      className="p-1.5 border border-emerald-700 text-emerald-300 hover:bg-emerald-950 disabled:opacity-40"
                    >
                      <Check className="w-3 h-3" />
                    </button>
                  )}
                  <button
                    onClick={() => setDismissed((prev) => new Set(prev).add(s.idx))}
                    data-testid={`dismiss-suggestion-${s.idx}`}
                    title="Dismiss"
                    className="p-1.5 border border-neutral-800 text-neutral-500 hover:bg-neutral-900"
                  >
                    <X className="w-3 h-3" />
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function Stat({ label, value, hl }) {
  const cls = hl === "good" ? "text-emerald-400"
    : hl === "bad" ? "text-red-400"
    : hl === "warn" ? "text-amber-400"
    : "text-neutral-200";
  return (
    <div className="border border-neutral-800 px-2 py-1">
      <div className="text-[9px] uppercase tracking-wider text-neutral-500">{label}</div>
      <div className={`text-sm font-mono font-semibold ${cls}`}>{value}</div>
    </div>
  );
}
