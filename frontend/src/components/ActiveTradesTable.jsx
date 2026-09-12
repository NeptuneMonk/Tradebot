import { memo } from "react";
import { Activity, X } from "lucide-react";
import { ChainBadge } from "./ChainBadge";
import { TradeTicket } from "./TradeTicket";

const short = (s) => (s ? `${s.slice(0, 4)}…${s.slice(-4)}` : "—");

function ActiveTradesTable({ trades, onExit }) {
  return (
    <div className="control-card" data-testid="active-trades-card">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
          <Activity className="w-3 h-3" /> Active Trades ({trades.length})
        </div>
      </div>
      <div className="overflow-x-auto overflow-y-auto max-h-[280px] md:max-h-[400px] [contain:layout]" data-testid="active-trades-scroll">
        <table className="w-full text-xs" data-testid="active-trades-table">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 border-b border-neutral-800">
              <th className="text-left py-2">Mint</th>
              <th className="text-right">Mode</th>
              <th className="text-right">Entry SOL</th>
              <th className="text-right">PnL %</th>
              <th className="text-right">Curve / Peak</th>
              <th className="text-right">Risk</th>
              <th className="text-right">Action</th>
            </tr>
          </thead>
          <tbody>
            {trades.length === 0 && (
              <tr><td colSpan="7" className="text-center py-6 text-[10px] uppercase tracking-[0.2em] text-neutral-600">
                no active positions
              </td></tr>
            )}
            {trades.map((t) => (
              <tr key={t.id} className="border-b border-neutral-900 hover:bg-neutral-900/40 transition-colors duration-100" data-testid={`active-trade-row-${t.mint}`}>
                <td className="py-2 font-mono">
                  <span className="mr-1.5 inline-flex align-middle"><TradeTicket t={t} compact /></span>
                  {t.venue === "pool" && (
                    <span
                      data-testid={`active-pool-badge-${t.id}`}
                      className="mr-1 inline-block px-1 py-0 border border-fuchsia-700 text-fuchsia-300 bg-fuchsia-950/40 text-[9px] font-mono align-middle"
                      title={`Graduated while held (${t.graduated_at_pnl_pct != null ? (t.graduated_at_pnl_pct >= 0 ? "+" : "") + t.graduated_at_pnl_pct.toFixed(1) + "%" : "?"}) — PONS curve swept into the Uniswap v4 pool; priced and exited via the pool, never a zero curve quote.`}
                    >pons → v4</span>
                  )}
                  {t.book === "runner" && <RunnerBadge t={t} />}
                  {t.venue_stage && t.venue_stage !== "pumpswap" && (
                    <span data-testid={`venue-stage-${t.mint}`} title="Curve finished — tokens held, waiting for the PumpSwap pool. No PnL is booked until a real AMM fill."
                      className="mr-1.5 px-1 py-0 border border-amber-700 text-amber-300 bg-amber-950/40 text-[9px] font-mono align-middle">
                      pumpfun → pumpswap · {t.venue_stage}
                    </span>
                  )}
                  {t.venue_stage === "pumpswap" && (
                    <span data-testid={`venue-stage-${t.mint}`} className="mr-1.5 px-1 py-0 border border-fuchsia-700 text-fuchsia-300 bg-fuchsia-950/40 text-[9px] font-mono align-middle">
                      pumpfun → pumpswap
                    </span>
                  )}
                  <span className="mr-1.5 align-middle inline-flex"><ChainBadge chain={t.chain} mint={t.mint} /></span>
                  {t.symbol ? <span className="text-neutral-200">{t.symbol}</span> : <span className="text-neutral-500">—</span>}
                  <span className="text-neutral-600 ml-2 text-[10px]">{short(t.mint)}</span>
                </td>
                <td className="text-right font-mono">
                  <span className={`px-1.5 py-0.5 border text-[10px] uppercase ${t.mode === "live" ? "border-red-700 text-red-300" : "border-blue-700 text-blue-300"}`}>
                    {t.mode}
                  </span>
                </td>
                <td className="text-right font-mono" data-testid={`active-entry-${t.mint}`}>
                  {t.chain === "rh"
                    ? <>{(t.entry_quote ?? 0).toFixed(5)} <span className="text-neutral-600 text-[10px]">{t.quote_symbol || "ETH"}</span></>
                    : t.entry_sol.toFixed(5)}
                </td>
                <PnlCell pnl={t.unrealized_pnl_pct} drawdown={t.drawdown_from_peak_pct} mint={t.mint} />
                <CurveCell live={t.live_curve_fill_pct} target={t.snipe_pattern_ctx?.expected_rug_curve_pct} liveMc={t.live_usd_market_cap} peakMc={t.snipe_pattern_ctx?.expected_peak_mc_usd} mint={t.mint} />
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

const STAGE_CLS = {
  launch: "border-sky-700 text-sky-300 bg-sky-950/40",
  graduating: "border-amber-700 text-amber-300 bg-amber-950/40",
  graduated: "border-fuchsia-700 text-fuchsia-300 bg-fuchsia-950/40",
  retail: "border-emerald-700 text-emerald-300 bg-emerald-950/40",
  exhausted: "border-rose-700 text-rose-300 bg-rose-950/40",
};

// RUNNER · stage · peak % since promotion · giveback % · pool yes/no
function RunnerBadge({ t }) {
  const stage = t.runner_stage || "launch";
  const fmt = (v, sign) => (v == null ? "—" : `${sign && v >= 0 ? "+" : ""}${Number(v).toFixed(0)}%`);
  return (
    <span
      data-testid={`runner-badge-${t.mint}`}
      title={`Promoted from ${t.promoted_from || "?"} · stage ${stage}${t.runner_retail_reason ? ` · ${t.runner_retail_reason}` : ""} · peak ${fmt(t.runner_peak_pct, true)} since promotion · giveback ${fmt(t.runner_giveback_pct)} · pool ${t.runner_pool ? "yes" : "no"}`}
      className={`mr-1.5 inline-flex items-center gap-1 px-1 py-0 border text-[9px] font-mono align-middle ${STAGE_CLS[stage] || STAGE_CLS.launch}`}
    >
      <span className="font-semibold tracking-wider">RUNNER</span>
      <span data-testid={`runner-stage-${t.mint}`}>{stage}</span>
      <span className="text-neutral-400">pk {fmt(t.runner_peak_pct, true)}</span>
      <span className="text-neutral-400">gb {fmt(t.runner_giveback_pct)}</span>
      <span className={t.runner_pool ? "text-emerald-300" : "text-neutral-500"}>pool {t.runner_pool ? "yes" : "no"}</span>
    </span>
  );
}

function PnlCell({ pnl, drawdown, mint }) {
  if (pnl == null) {
    return <td className="text-right font-mono text-neutral-600" data-testid={`active-pnl-${mint}`}>—</td>;
  }
  const sign = pnl >= 0 ? "+" : "";
  const cls = pnl >= 0 ? "text-emerald-300" : "text-rose-300";
  return (
    <td className="text-right font-mono" data-testid={`active-pnl-${mint}`}>
      <span className={cls}>{sign}{pnl.toFixed(1)}%</span>
      {drawdown != null && drawdown > 5 && (
        <span className="ml-1 text-[9px] text-neutral-500" title={`Down ${drawdown.toFixed(1)}% from peak`}>
          ↓{drawdown.toFixed(0)}
        </span>
      )}
    </td>
  );
}

function CurveCell({ live, target, liveMc, peakMc, mint }) {
  if (live == null && liveMc == null) {
    return <td className="text-right font-mono text-neutral-600" data-testid={`active-curve-${mint}`}>—</td>;
  }
  return (
    <td className="text-right font-mono text-[10px]" data-testid={`active-curve-${mint}`}>
      {live != null && (
        <div>
          <span className="text-neutral-400">{Number(live).toFixed(0)}%</span>
          {target != null && (
            <span className="text-neutral-600"> / {Number(target).toFixed(0)}%</span>
          )}
        </div>
      )}
      {liveMc != null && liveMc > 0 && (
        <div className="text-neutral-500">
          ${Math.round(liveMc / 1000)}k
          {peakMc != null && peakMc > 0 && (
            <span className="text-neutral-700"> / ${Math.round(peakMc / 1000)}k</span>
          )}
        </div>
      )}
    </td>
  );
}

function RiskBadge({ risk }) {
  let cls = "text-emerald-400 border-emerald-800 bg-emerald-950/40";
  if (risk >= 70) cls = "text-red-400 border-red-800 bg-red-950/40";
  else if (risk >= 50) cls = "text-amber-400 border-amber-800 bg-amber-950/40";
  return <span className={`px-1.5 py-0.5 border text-[10px] ${cls}`}>{risk}</span>;
}


export default memo(ActiveTradesTable);
