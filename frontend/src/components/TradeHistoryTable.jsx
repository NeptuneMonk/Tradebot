import { memo } from "react";
import { History, CircleDot, Search } from "lucide-react";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { ChainBadge } from "./ChainBadge";
import { TradeTicket } from "./TradeTicket";

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
  if (/max[- ]hold|scalp clock/i.test(r)) return { label: "Clock", tint: "text-cyan-300" };
  if (/ladder \+1R|ladder \+2R/i.test(r)) return { label: "Ladder Leg", tint: "text-cyan-300" };
  if (/ladder stop/i.test(r)) return { label: "Ladder Stop", tint: "text-amber-300" };
  if (/target \+/i.test(r)) return { label: "Target R", tint: "text-emerald-300" };
  if (/manual/i.test(r)) return { label: "Manual Exit", tint: "text-neutral-300" };
  if (/kill[- ]switch/i.test(r)) return { label: "Kill Switch", tint: "text-rose-400" };
  if (/graceful/i.test(r)) return { label: "Graceful Stop", tint: "text-neutral-300" };
  if (/balance was 0/i.test(r)) return { label: "Wallet Empty", tint: "text-rose-400" };
  if (/GAVE UP/i.test(r)) return { label: "Sell Failed", tint: "text-rose-500" };
  if (/RESCUED/i.test(r)) return { label: "Emergency Sell", tint: "text-amber-400" };

  // Fallback — first 3 words
  return { label: r.split(/[\s(]/).slice(0, 3).join(" "), tint: "text-neutral-400" };
}

const reentryTitle = (t) => {
  const c = t.reentry_ctx;
  if (!c) return `re-entry · ${t.reentry_trigger || t.reentry}`;
  const f = (v, d = 1) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(d)}%`);
  return [
    `Re-entry fired: ${c.trigger} (attempt ${c.attempt ?? "?"})`,
    `ran on after exit: ${f(c.run_on_pct, 0)} · pulled back from peak: ${c.pullback_pct == null ? "—" : `-${Number(c.pullback_pct).toFixed(0)}%`}`,
    `bounce off trough: ${f(c.bounce_pct)} · buyers in window: ${c.buyers ?? "—"}`,
    `entry vs prior exit: ${f(c.vs_exit_pct)}`,
  ].join("\n");
};

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
                            <div className="mt-1"><TradeTicket t={t} /></div>
                          </div>
                        )}
                        {(t.exit_trigger_pnl_pct != null || t.exit_deferred_s != null || t.rug_alert) && (
                          <div className="mt-1 text-[10px] text-neutral-500" data-testid={`exit-audit-${t.id}`}>
                            {t.exit_trigger_pnl_pct != null && (
                              <>trigger at <span className="text-neutral-300">{t.exit_trigger_pnl_pct >= 0 ? "+" : ""}{t.exit_trigger_pnl_pct.toFixed(1)}%</span> → fill {t.pnl_pct >= 0 ? "+" : ""}{Number(t.pnl_pct).toFixed(1)}% after {t.exit_latency_blocks ?? "?"} blocks of latency{t.exit_trigger_source === "feed" ? <span className="text-lime-300"> · triggered from the sequencer feed (est. price)</span> : ""}</>
                            )}
                            {t.rug_alert && (
                              <div className="text-rose-300">rug detected on the sequencer feed: sell ≈ ${t.rug_alert.est_usd} ({t.rug_alert.curve_pct}% of curve) — exited ahead of the poll</div>
                            )}
                            {t.exit_deferred_s != null && (
                              <div>SL/TP deferred <span className="text-amber-300">{t.exit_deferred_s}s</span> on buy momentum{t.exit_defer_bounded ? " · fired at the SL+extra bound" : ""}</div>
                            )}
                          </div>
                        )}
                        {t.reentry_ctx && (
                          <div className="mt-1 text-[10px] text-neutral-500 whitespace-pre-line" data-testid={`reentry-ctx-${t.id}`}>
                            {reentryTitle(t)}
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
                    {t.exit_venue === "pool" && (
                      <span
                        data-testid={`pool-badge-${t.id}`}
                        className="mr-1 inline-block px-1 py-0 border border-fuchsia-800 text-fuchsia-300 text-[9px] font-mono align-middle"
                        title={`Graduated while held${t.graduated_at_pnl_pct != null ? ` at ${t.graduated_at_pnl_pct >= 0 ? "+" : ""}${t.graduated_at_pnl_pct.toFixed(1)}%` : ""}; sold on the Uniswap v4 pool${t.pool_hold_s != null ? ` after ${Math.round(t.pool_hold_s)}s on the pool` : ""}.`}
                      >POOL</span>
                    )}
                    {t.chain !== "rh" && t.protocol === "pumpswap" && (
                      <span data-testid={`grad-badge-${t.id}`} title="Graduated token — entered and exited on its PumpSwap pool (seasoned band)"
                        className="mr-1 inline-block px-1 py-0 border border-fuchsia-800 text-fuchsia-300 text-[9px] font-mono align-middle">GRAD</span>
                    )}
                    <span className="mr-1.5 align-middle inline-flex"><ChainBadge chain={t.chain} mint={t.mint} /></span>
                    {t.symbol || "?"} <span className="text-neutral-600 text-[10px]">{short(t.mint)}</span>
                    {(t.reentry_trigger || t.reentry) && (
                      <span
                        data-testid={`reentry-badge-${t.id}`}
                        className={`ml-1.5 inline-block px-1 py-0 border text-[9px] font-mono uppercase align-middle ${
                          (t.reentry_trigger || t.reentry) === "pullback"
                            ? "border-amber-800 text-amber-300"
                            : "border-fuchsia-800 text-fuchsia-300"
                        }`}
                        title={reentryTitle(t)}
                      >
                        re-entry · {t.reentry_trigger || t.reentry}
                        {t.reentry_ctx && (
                          <span className="ml-1 normal-case text-neutral-400">
                            {t.reentry_ctx.run_on_pct != null && <>↑{t.reentry_ctx.run_on_pct.toFixed(0)}% </>}
                            {t.reentry_ctx.pullback_pct != null && <>↓{t.reentry_ctx.pullback_pct.toFixed(0)}% </>}
                            {t.reentry_ctx.bounce_pct != null && <>⤴{t.reentry_ctx.bounce_pct.toFixed(1)}% </>}
                            {t.reentry_ctx.buyers != null && <>· {t.reentry_ctx.buyers}b</>}
                          </span>
                        )}
                      </span>
                    )}
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
