import { ShieldAlert } from "lucide-react";

export default function DailyLossMeter({ status }) {
  const loss = status?.daily_loss_usd ?? 0;
  const limit = status?.daily_kill_switch_usd ?? 20;
  const pct = Math.min(100, (loss / Math.max(0.0001, limit)) * 100);

  let bar = "bg-emerald-600";
  if (pct >= 75) bar = "bg-red-600";
  else if (pct >= 40) bar = "bg-amber-600";

  return (
    <div className="control-card flex flex-col gap-3" data-testid="daily-loss-meter">
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-500 flex items-center gap-1.5">
          <ShieldAlert className="w-3 h-3" /> Daily Loss Meter
        </span>
        <span className="text-[10px] font-mono text-neutral-500">{pct.toFixed(0)}%</span>
      </div>
      <div>
        <div className="text-2xl font-mono font-semibold text-red-400" data-testid="daily-loss-value">
          -${loss.toFixed(2)}
        </div>
        <div className="text-xs font-mono text-neutral-500">
          of <span className="text-neutral-300">${limit.toFixed(2)}</span> kill switch
        </div>
      </div>
      <div className="h-2 bg-neutral-900 border border-neutral-800 overflow-hidden">
        <div className={`h-full ${bar} transition-all duration-200`} style={{ width: `${pct}%` }} data-testid="loss-bar" />
      </div>
      <div className="text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-600">
        Trades today: <span className="text-neutral-400" data-testid="trades-today">{status?.total_trades_today ?? 0}</span> · Active: <span className="text-neutral-400" data-testid="active-count">{status?.active_trade_count ?? 0}</span>
      </div>
    </div>
  );
}
