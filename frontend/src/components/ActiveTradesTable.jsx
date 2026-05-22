import { Activity, X } from "lucide-react";

const short = (s) => (s ? `${s.slice(0, 4)}…${s.slice(-4)}` : "—");

export default function ActiveTradesTable({ trades, onExit }) {
  return (
    <div className="control-card" data-testid="active-trades-card">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
          <Activity className="w-3 h-3" /> Active Trades ({trades.length})
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs" data-testid="active-trades-table">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 border-b border-neutral-800">
              <th className="text-left py-2">Mint</th>
              <th className="text-right">Mode</th>
              <th className="text-right">Entry SOL</th>
              <th className="text-right">Tokens</th>
              <th className="text-right">Risk</th>
              <th className="text-right">Action</th>
            </tr>
          </thead>
          <tbody>
            {trades.length === 0 && (
              <tr><td colSpan="6" className="text-center py-6 text-[10px] uppercase tracking-[0.2em] text-neutral-600">
                no active positions
              </td></tr>
            )}
            {trades.map((t) => (
              <tr key={t.id} className="border-b border-neutral-900 hover:bg-neutral-900/40 transition-colors duration-100" data-testid={`active-trade-row-${t.mint}`}>
                <td className="py-2 font-mono">
                  {t.symbol ? <span className="text-neutral-200">{t.symbol}</span> : <span className="text-neutral-500">—</span>}
                  <span className="text-neutral-600 ml-2 text-[10px]">{short(t.mint)}</span>
                </td>
                <td className="text-right font-mono">
                  <span className={`px-1.5 py-0.5 border text-[10px] uppercase ${t.mode === "live" ? "border-red-700 text-red-300" : "border-blue-700 text-blue-300"}`}>
                    {t.mode}
                  </span>
                </td>
                <td className="text-right font-mono">{t.entry_sol.toFixed(5)}</td>
                <td className="text-right font-mono text-neutral-400">{Number(t.entry_tokens).toExponential(2)}</td>
                <td className="text-right font-mono">
                  <RiskBadge risk={t.risk_score} />
                </td>
                <td className="text-right">
                  <button
                    onClick={() => onExit(t.id)}
                    data-testid={`exit-trade-btn-${t.mint}`}
                    className="px-2 py-0.5 border border-red-700 text-red-300 hover:bg-red-950 text-[10px] uppercase tracking-wider transition-colors duration-100 inline-flex items-center gap-1"
                  >
                    <X className="w-3 h-3" /> Exit
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RiskBadge({ risk }) {
  let cls = "text-emerald-400 border-emerald-800 bg-emerald-950/40";
  if (risk >= 70) cls = "text-red-400 border-red-800 bg-red-950/40";
  else if (risk >= 50) cls = "text-amber-400 border-amber-800 bg-amber-950/40";
  return <span className={`px-1.5 py-0.5 border text-[10px] ${cls}`}>{risk}</span>;
}
