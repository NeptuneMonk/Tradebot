import { memo } from "react";
import { Wallet, TrendingUp, TrendingDown, FlaskConical } from "lucide-react";

const signed = (v) => `${v >= 0 ? "+" : "-"}$${Math.abs(Number(v) || 0).toFixed(2)}`;

function Kpi({ label, value, sub, cls = "text-neutral-100", Icon, testid }) {
  return (
    <div className="flex-1 min-w-0 border border-neutral-800 bg-neutral-950/60 px-3 py-2" data-testid={testid}>
      <div className="flex items-center gap-1.5 text-[9px] uppercase tracking-[0.2em] text-neutral-500">{Icon && <Icon className="w-3 h-3" />}{label}</div>
      <div className={`font-mono text-base md:text-lg font-semibold leading-tight truncate ${cls}`}>{value}</div>
      {sub && <div className="text-[10px] font-mono text-neutral-500 truncate">{sub}</div>}
    </div>
  );
}

function KpiRow({ wallet, status }) {
  const live = Number(status?.daily_pnl_live_usd ?? 0), paper = Number(status?.daily_pnl_paper_usd ?? 0);
  return (
    <div className="flex gap-2 mb-2" data-testid="cockpit-kpis">
      <Kpi label="wallet" Icon={Wallet} value={wallet ? `${wallet.sol_balance.toFixed(3)} SOL` : "—"} sub={wallet ? `$${wallet.usd_balance.toFixed(2)} · SOL $${wallet.sol_price_usd.toFixed(0)}` : null} testid="kpi-wallet" />
      <Kpi label="live today" Icon={live >= 0 ? TrendingUp : TrendingDown} value={signed(live)} cls={live >= 0 ? "text-emerald-400" : "text-red-400"} sub={status?.live_trading ? "sol LIVE armed" : "sol paper"} testid="kpi-live" />
      <Kpi label="paper today" Icon={FlaskConical} value={signed(paper)} cls={paper >= 0 ? "text-emerald-300/80" : "text-red-300/80"} sub={`${status?.total_trades_today ?? 0} trades today`} testid="kpi-paper" />
    </div>
  );
}

export default memo(KpiRow);
