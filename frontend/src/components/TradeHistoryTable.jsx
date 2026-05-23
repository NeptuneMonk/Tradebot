import { History, CircleDot } from "lucide-react";

const short = (s) => (s ? `${s.slice(0, 4)}…${s.slice(-4)}` : "—");
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—");

export default function TradeHistoryTable({ history }) {
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
      <div className="overflow-x-auto max-h-[420px] overflow-y-auto">
        <table className="w-full text-xs" data-testid="trade-history-table">
          <thead className="sticky top-0 bg-neutral-900">
            <tr className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 border-b border-neutral-800">
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
              <tr><td colSpan="8" className="text-center py-6 text-[10px] uppercase tracking-[0.2em] text-neutral-600">no trades yet</td></tr>
            )}
            {history.map((t) => {
              const win = t.pnl_usd > 0;
              const banked = Number(t.partial_realized_usd || 0);
              return (
                <tr key={t.id} className="border-b border-neutral-900" data-testid={`history-row-${t.id}`}>
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
