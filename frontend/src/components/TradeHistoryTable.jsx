import { memo } from "react";
import { History, CircleDot, Search } from "lucide-react";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";

const short = (s) => (s ? `${s.slice(0, 4)}…${s.slice(-4)}` : "—");
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—");

// Map raw exit_reason strings → short label + colour + extra detail.
// Keeps the magnifier hover tight while still surfacing the trigger that fired.
function summarizeExit(t) {
  const r = (t.exit_reason || "").trim();
  if (!r) return { label: t.status || "—", tint: "text-neutral-500" };

  // Snipe gates
  if (/profit-ripcord/i.test(r)) return { label: "Profit Ripcord", tint: "text-emerald-300" };
  if (/stale-exit/i.test(r)) return { label: "Stale Exit", tint: "text-amber-300" };
  if (/SOL-velocity decay/i.test(r)) return { label: "SOL Velocity ↓", tint: "text-orange-300" };
  if (/new-holder velocity decay/i.test(r)) return { label: "Holders Velocity ↓", tint: "text-orange-300" };
  if (/curve-fill exit/i.test(r)) return { label: "Curve-fill Target", tint: "text-cyan-300" };
  if (/peak-mc/i.test(r)) return { label: "Peak-MC Target", tint: "text-cyan-300" };
  if (/snipe pattern-TP/i.test(r)) return { label: "Pattern TP", tint: "text-emerald-300" };
  if (/snipe rip-cord/i.test(r) || /rip-cord/i.test(r)) return { label: "Drawdown Ripcord", tint: "text-rose-300" };

  // Standard momentum exits
  if (/take-profit/i.test(r)) return { label: "Take Profit", tint: "text-emerald-300" };
  if (/stop-loss/i.test(r) || /\bSL hit/i.test(r)) return { label: "Stop Loss", tint: "text-rose-300" };
  if (/trail/i.test(r)) return { label: "Trail Stop", tint: "text-emerald-400" };
  if (/max[- ]hold/i.test(r)) return { label: "Max Hold", tint: "text-cyan-300" };
  if (/abort_trade|abort\b/i.test(r)) return { label: "Classifier Abort", tint: "text-purple-300" };
  if (/manual/i.test(r)) return { label: "Manual Exit", tint: "text-neutral-300" };
  if (/kill[- ]switch/i.test(r)) return { label: "Kill Switch", tint: "text-rose-400" };
  if (/graceful/i.test(r)) return { label: "Graceful Stop", tint: "text-neutral-300" };
  if (/balance was 0/i.test(r)) return { label: "Wallet Empty", tint: "text-rose-400" };
  if (/GAVE UP/i.test(r)) return { label: "Sell Failed", tint: "text-rose-500" };
  if (/RESCUED/i.test(r)) return { label: "Emergency Sell", tint: "text-amber-400" };

  // Fallback — first 3 words
  return { label: r.split(/[\s(]/).slice(0, 3).join(" "), tint: "text-neutral-400" };
}

function TradeHistoryTable({ history }) {
  const partialCount = history.filter((t) => t.partial_done).length;
  const partialBanked = history.reduce((sum, t) => sum + (t.partial_realized_usd || 0), 0);

  return (
    <div className="control-card" data-testid="trade-history-card">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
          <History className="w-3 h-3" /> Trade History ({history.length})
        </div>
        {partialCount > 0 && (
          <div
            data-testid="partial-tp-summary"
            className="flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-[0.15em] text-cyan-300 border border-cyan-900/60 bg-cyan-950/30 px-2 py-0.5"
            title={`Partial TP fired on ${partialCount} trades — \$${partialBanked.toFixed(2)} banked early`}
          >
            <CircleDot className="w-3 h-3" /> ½TP × {partialCount} · ${partialBanked.toFixed(2)}
          </div>
        )}
      </div>
      <div className="overflow-x-auto max-h-[300px] md:max-h-[420px] overflow-y-auto [contain:layout] [overscroll-behavior:contain]">
        <table className="w-full text-xs" data-testid="trade-history-table">
          <thead className="sticky top-0 bg-neutral-900">
            <tr className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 border-b border-neutral-800">
              <th className="w-6"></th>
              <th className="text-left py-2">When</th>
              <th className="text-left">Mint</th>
              <th className="text-right">Mode</th>
              <th className="text-right">Entry $</th>
              <th className="text-right">Exit $</th>
              <th className="text-right">½TP $</th>
              <th className="text-right">P/L $</th>
              <th className="text-right">P/L %</th>
            </tr>
          </thead>
          <tbody>
            {history.length === 0 && (
              <tr><td colSpan="9" className="text-center py-6 text-[10px] uppercase tracking-[0.2em] text-neutral-600">no trades yet</td></tr>
            )}
            {history.map((t) => {
              const win = t.pnl_usd > 0;
              const banked = Number(t.partial_realized_usd || 0);
              const exitSummary = summarizeExit(t);
              return (
                <tr key={t.id} className="border-b border-neutral-900" data-testid={`history-row-${t.id}`}>
                  <td className="py-1.5 pl-0.5">
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button
                          type="button"
                          data-testid={`history-exit-reason-${t.id}`}
                          className="text-neutral-600 hover:text-neutral-200 transition-colors duration-100"
                          aria-label={`Why this trade exited: ${exitSummary.label}`}
                        >
                          <Search className="w-3 h-3" />
                        </button>
                      </TooltipTrigger>
                      <TooltipContent
                        side="right"
                        className="max-w-[280px] bg-neutral-900 border border-neutral-700 text-neutral-200 px-3 py-2 text-[11px] font-mono leading-relaxed"
                      >
                        <div className="text-[9px] uppercase tracking-[0.15em] text-neutral-500 mb-1">Exit Trigger</div>
                        <div className={`text-[12px] mb-1 ${exitSummary.tint}`}>{exitSummary.label}</div>
                        {t.exit_reason && (
                          <div className="text-neutral-400 text-[10px] break-words">{t.exit_reason}</div>
                        )}
                        {t.classifier_action && (
                          <div className="mt-1.5 pt-1.5 border-t border-neutral-800 text-[10px] text-neutral-500">
                            entered via <span className="text-neutral-300">{t.classifier_action}</span>
                          </div>
                        )}
                      </TooltipContent>
                    </Tooltip>
                  </td>
                  <td className="py-1.5 font-mono text-neutral-500 text-[10px]">{fmtTime(t.exit_time || t.entry_time)}</td>
                  <td className="font-mono text-neutral-300">
                    {t.partial_done && (
                      <span
                        data-testid={`partial-badge-${t.id}`}
                        className="mr-1 inline-block px-1 py-0 border border-cyan-800 text-cyan-300 text-[9px] font-mono align-middle"
                        title={t.partial_reason || "partial TP fired"}
                      >½TP</span>
                    )}
                    {t.symbol || "?"} <span className="text-neutral-600 text-[10px]">{short(t.mint)}</span>
                  </td>
                  <td className="text-right font-mono text-[10px] uppercase text-neutral-500">{t.mode}</td>
                  <td className="text-right font-mono">${t.entry_usd?.toFixed(2)}</td>
                  <td className="text-right font-mono">${t.exit_usd?.toFixed(2)}</td>
                  <td className={`text-right font-mono ${banked > 0 ? "text-cyan-300" : "text-neutral-700"}`}>
                    {banked > 0 ? `+$${banked.toFixed(2)}` : "—"}
                  </td>
                  <td className={`text-right font-mono ${win ? "text-emerald-400" : "text-red-400"}`}>
                    {win ? "+" : ""}${t.pnl_usd?.toFixed(2)}
                  </td>
                  <td className={`text-right font-mono ${win ? "text-emerald-400" : "text-red-400"}`}>
                    {win ? "+" : ""}{t.pnl_pct?.toFixed(1)}%
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default memo(TradeHistoryTable);
