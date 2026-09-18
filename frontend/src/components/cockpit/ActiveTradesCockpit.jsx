import { memo, useState } from "react";
import { X } from "lucide-react";
import { TokenDetailDialog } from "../TokenDetailDialog";

const BOOK_CLS = {
  scalp: "border-sky-700 text-sky-300", hunt: "border-emerald-600 text-emerald-300", runner: "border-cyan-600 text-cyan-300",
  rh_pons: "border-lime-600 text-lime-300", ladder: "border-fuchsia-600 text-fuchsia-300",
};
const clamp = (v) => Math.max(0, Math.min(100, v));
const Check = ({ ok }) => <span className={ok ? "text-emerald-300" : "text-neutral-600"}>{ok ? "✓" : "–"}</span>;

/** Book-aware stage: what the exit engine is waiting for on this position, from fields already on the trade doc. */
export function stageFor(t) {
  const pnl = Number(t.unrealized_pnl_pct ?? 0);
  const book = t.book || (t.chain === "rh" ? "rh_pons" : "scalp");
  if (book === "hunt") {
    const legs = Number(t.ladder_legs_done || 0);
    return { text: <>legs {legs}/2 · spike <Check ok={t.spike_banked} /></>, pct: clamp(legs * 40 + (t.spike_banked ? 20 : 0)), hint: "hunt: +1R / +2R ladder partials, spike bank sells half the remainder at +100%, ratchet trail on the rest" };
  }
  if (book === "runner") {
    const trail = t.runner_trail_pct != null ? `${Number(t.runner_trail_pct).toFixed(0)}%` : "—";
    return { text: <>{t.runner_stage || "launch"} · trail {trail} · +3R <Check ok={t.runner_3r_done} /></>, pct: clamp(Number(t.runner_peak_pct ?? pnl) / 3), hint: `runner: giveback trail from promotion (peak ${t.runner_peak_pct != null ? Number(t.runner_peak_pct).toFixed(0) + "%" : "—"}), +3R chip, rip-cords` };
  }
  if (book === "ladder") {
    return { text: <>leg {t.ladder_leg ?? "?"} · entry MC {t.entry_mc_usd ? `$${Math.round(t.entry_mc_usd / 1000)}k` : "—"}</>, pct: clamp(pnl), hint: "graduate ladder leg: ratchet trail from the leg peak, structure stop closes every leg" };
  }
  if (book === "rh_pons" || t.chain === "rh") {
    if (t.venue === "pool" && t.r_trail) {
      const stop = t.r_trail_stop_pct != null ? `${Number(t.r_trail_stop_pct) >= 0 ? "+" : ""}${Number(t.r_trail_stop_pct).toFixed(0)}%` : "—";
      const trail = t.r_trail_trail_pct != null ? `${Number(t.r_trail_trail_pct).toFixed(0)}%` : "—";
      return { text: <>R-trail · stop {stop} · give {trail}</>, pct: clamp(Number(t.r_trail_peak_pct ?? pnl) / 3),
               hint: `graduated while held → riding the v4 pool on an R trail: no fixed TP, no clock; stop ${stop} (breakeven+costs once +1R), exit on a ${trail} giveback from the peak (${t.r_trail_peak_pct != null ? "+" + Number(t.r_trail_peak_pct).toFixed(0) + "%" : "—"})` };
    }
    if (t.venue === "pool") return { text: <>curve→pool <Check ok /></>, pct: 100, hint: "graduated while held — priced and exited via the Uniswap v4 pool" };
    const fill = Number(t.live_curve_fill_pct ?? 0);
    return { text: <>curve {fill.toFixed(0)}% · pool <Check ok={false} /></>, pct: clamp(fill), hint: "PONS bonding curve — graduation sweeps the position into the v4 pool" };
  }
  if (t.venue_stage && t.venue_stage !== "pumpswap") return { text: <>curve→pool · {t.venue_stage}</>, pct: 50, hint: "Pump.fun curve complete — waiting for the PumpSwap pool before any PnL is booked" };
  const target = Number(t.target_r || 0) * Number(t.sl_pct || 0);
  return { text: <>target {t.target_r ? `${t.target_r}R` : "—"} · SL {t.sl_pct ? `${t.sl_pct}%` : "—"}</>, pct: target > 0 ? clamp((pnl / target) * 100) : 0, hint: "scalp: single exit at +target·R or −1R, clock allowed" };
}

const isManualHold = (t) => t.manual === true || t.classifier_action === "manual" || t.classifier_action === "rh_pons_manual";

function Row({ t, onExit, onOpen }) {
  const pnl = t.unrealized_pnl_pct;
  const book = t.book || (t.chain === "rh" ? "rh_pons" : "scalp");
  const st = stageFor(t);
  const manual = isManualHold(t);
  return (
    <tr data-testid={`active-trade-row-${t.mint}`} onClick={(e) => { if (!e.target.closest("button")) onOpen({ chain: t.chain || "sol", mint: t.mint, symbol: t.symbol }); }}
      className={`border-b border-neutral-900 hover:bg-neutral-900/50 cursor-pointer transition-colors duration-100 ${manual ? "bg-fuchsia-950/10" : ""}`}
      title={`${t.mode} · size $${Number(t.size_usd ?? t.entry_usd ?? 0).toFixed(2)} · R $${Number(t.r_usd ?? 0).toFixed(3)} · risk ${t.risk_score ?? "—"} · ${t.scorecard_cell || ""}${manual ? " · MANUAL HOLD: SL/TP/trail only — no clock, no momentum kill, not counted toward max positions" : ""}`}>
      <td className="py-2 pl-3"><span className={`px-1.5 py-0.5 border text-[9px] font-mono uppercase tracking-[0.15em] ${BOOK_CLS[book] || BOOK_CLS.scalp}`}>{book.replace("_", " ")}</span>
        {manual && <span className="ml-1 px-1 py-0.5 border border-fuchsia-700 text-fuchsia-300 text-[9px] font-mono" data-testid={`active-manual-badge-${t.mint}`}>HOLD</span>}
        {t.mode === "live" && <span className="ml-1 px-1 py-0.5 border border-red-800 text-red-300 text-[9px] font-mono">LIVE</span>}</td>
      <td className="font-mono text-xs"><span className="text-neutral-100">{t.symbol || "—"}</span> <span className="text-neutral-600 text-[10px]">{t.chain === "rh" ? "rh" : "sol"}</span></td>
      <td className="font-mono text-xs text-right" data-testid={`active-pnl-${t.mint}`}>
        {pnl == null ? <span className="text-neutral-600">—</span> : <span className={pnl >= 0 ? "text-emerald-300" : "text-red-300"}>{pnl >= 0 ? "+" : ""}{pnl.toFixed(1)}%</span>}
        {t.drawdown_from_peak_pct > 5 && <span className="ml-1 text-[9px] text-neutral-500">↓{t.drawdown_from_peak_pct.toFixed(0)}</span>}
      </td>
      <td className="font-mono text-[11px] text-neutral-300 pl-4" title={st.hint} data-testid={`active-stage-${t.mint}`}>{st.text}</td>
      <td className="w-20"><span className="block h-1.5 w-16 bg-neutral-900 border border-neutral-800 overflow-hidden"><span className="block h-full bg-emerald-500/80" style={{ width: `${st.pct}%`, transition: "width 400ms ease" }} /></span></td>
      <td className="text-right pr-3">
        <button onClick={() => onExit(t.id)} data-testid={`exit-trade-btn-${t.mint}`} className="p-1 border border-red-900 text-red-300 hover:bg-red-950 transition-colors duration-100" title="manual exit">
          <X className="w-3 h-3" />
        </button>
      </td>
    </tr>
  );
}

function ActiveTradesCockpit({ trades, onExit }) {
  const [detail, setDetail] = useState(null);
  const auto = trades.filter((t) => !isManualHold(t));
  const manual = trades.filter(isManualHold);
  return (
    <div className="control-card h-full flex flex-col !p-0" data-testid="active-trades-card">
      <div className="px-3 py-2 border-b border-neutral-800 text-[11px] font-mono tracking-[0.2em] text-neutral-200 flex items-center justify-between">
        <span>ACTIVE TRADES</span>
        <span className="text-neutral-500" data-testid="active-trades-count">{auto.length}{manual.length ? <span className="text-fuchsia-400"> +{manual.length} hold</span> : null}</span>
      </div>
      <div className="overflow-auto max-h-[380px]">
        <table className="w-full text-xs" data-testid="active-trades-table">
          <thead>
            <tr className="text-[9px] uppercase tracking-[0.2em] text-neutral-500 border-b border-neutral-800">
              <th className="text-left py-2 pl-3 font-normal">book</th><th className="text-left font-normal">symbol</th><th className="text-right font-normal">unrealized p/l</th>
              <th className="text-left pl-4 font-normal">stage</th><th className="font-normal" /><th className="font-normal" />
            </tr>
          </thead>
          <tbody>
            {trades.length === 0 && <tr><td colSpan="6" className="text-center py-8 text-[10px] uppercase tracking-[0.2em] text-neutral-600">no active positions</td></tr>}
            {auto.map((t) => <Row key={t.id} t={t} onExit={onExit} onOpen={setDetail} />)}
            {manual.length > 0 && (
              <tr data-testid="manual-holds-divider"><td colSpan="6" className="py-1 pl-3 text-[9px] uppercase tracking-[0.2em] text-fuchsia-400/80 bg-fuchsia-950/20 border-y border-fuchsia-900/40"
                title="Operator buys (Buy Now / Graduate Ladder). Long holds: SL / TP / trail only — no clock, no momentum kill, not counted toward max positions.">
                manual holds · {manual.length} · outside max positions</td></tr>
            )}
            {manual.map((t) => <Row key={t.id} t={t} onExit={onExit} onOpen={setDetail} />)}
          </tbody>
        </table>
      </div>
      <TokenDetailDialog token={detail} onClose={() => setDetail(null)} />
    </div>
  );
}

export default memo(ActiveTradesCockpit);
