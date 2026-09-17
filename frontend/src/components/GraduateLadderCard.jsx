import { memo } from "react";
import { Layers, X } from "lucide-react";
import { toast } from "sonner";
import { ChainBadge } from "./ChainBadge";
import HelpHint from "./HelpHint";
import { api } from "@/lib/api";

const fmtUsd = (n) => {
  const v = Number(n) || 0;
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toFixed(0)}`;
};
const short = (s) => (s ? `${s.slice(0, 4)}…${s.slice(-4)}` : "—");

function LadderRow({ t, onRemove }) {
  const holding = t.state === "holding";
  const legPnl = t.legs?.length ? t.legs.reduce((a, l) => a + l.pnl_pct, 0) / t.legs.length : null;
  return (
    <li data-testid={`ladder-row-${t.mint}`} className={`border px-3 py-2 ${holding ? "border-emerald-800 bg-emerald-950/20" : "border-neutral-800"}`}>
      <div className="flex items-center gap-2">
        <ChainBadge chain={t.chain} protocol={t.chain === "rh" ? t.protocol : null} mint={t.mint} />
        <span className="font-mono font-semibold text-sm truncate">{t.symbol || "?"}</span>
        <span className="text-[10px] font-mono text-neutral-500 truncate">{t.name || short(t.mint)}</span>
        <span className={`text-[9px] font-mono uppercase tracking-[0.15em] px-1.5 py-0.5 border ${holding ? "border-emerald-700 text-emerald-300" : t.qualified ? "border-amber-700 text-amber-300" : "border-neutral-700 text-neutral-500"}`}
          data-testid={`ladder-state-${t.mint}`}>{holding ? `holding ${t.legs.length} leg${t.legs.length === 1 ? "" : "s"}` : t.qualified ? "qualified" : "watching"}</span>
        {t.gate && <span className="text-[9px] font-mono text-neutral-500" title="re-entry control holding the starter">gate: {t.gate}</span>}
        <button onClick={() => onRemove(t.key)} data-testid={`ladder-remove-${t.mint}`} className="ml-auto p-1 border border-neutral-800 hover:bg-neutral-800 text-neutral-500" title="Drop from the ladder">
          <X className="w-3 h-3" />
        </button>
      </div>
      <div className="text-[10px] font-mono text-neutral-500 mt-1 flex flex-wrap gap-x-3">
        <span>MC <span className="text-neutral-200">{fmtUsd(t.mc)}</span></span>
        <span>steps <span className={t.steps >= 3 ? "text-emerald-400" : "text-neutral-200"} data-testid={`ladder-steps-${t.mint}`}>{t.steps}/3</span></span>
        <span>high <span className="text-neutral-200">{fmtUsd(t.high)}</span></span>
        <span>drawdown <span className={t.drawdown_pct > 25 ? "text-amber-400" : "text-neutral-200"}>-{t.drawdown_pct}%</span></span>
        <span>holders <span className="text-neutral-200">{t.holders}</span></span>
        <span>on ladder <span className="text-neutral-200">{t.age_h}h</span></span>
        {legPnl != null && <span>legs <span className={legPnl >= 0 ? "text-emerald-400" : "text-red-400"}>{legPnl >= 0 ? "+" : ""}{legPnl.toFixed(1)}%</span></span>}
        {t.closed_legs > 0 && <span>banked <span className={t.realized_usd >= 0 ? "text-emerald-400" : "text-red-400"}>${(t.realized_usd || 0).toFixed(2)}</span> / {t.closed_legs} legs</span>}
        {t.reentry?.last_exit_ts && (
          <span title={t.reentry.last_exit_reason || ""}>re-entry <span className="text-neutral-200">{t.reentry.attempts} att</span> · since exit peak {fmtUsd(t.reentry.peak_after_exit)} / trough {fmtUsd(t.reentry.trough_after_exit)}</span>
        )}
      </div>
    </li>
  );
}

function GraduateLadderCard({ ladder, config, onConfigPatch, onRefresh }) {
  const tokens = ladder?.tokens || [];
  const holding = tokens.filter((t) => t.state === "holding");
  const remove = async (key) => {
    try { await api.removeLadder(key); toast.success("Dropped from the ladder"); onRefresh?.(); } catch { toast.error("Could not remove"); }
  };
  const enabled = config?.ladder_enabled ?? true;
  return (
    <div data-testid="ladder-card">
      <div className="flex items-center justify-between mb-2 text-[10px] font-mono text-neutral-500">
        <span className="flex items-center gap-1.5 uppercase tracking-[0.2em] text-fuchsia-300"><Layers className="w-3 h-3" /> graduate ladder
          <HelpHint label="?">Age-less watch of graduated tokens (PumpSwap + RH v4 pools) that keep making higher market-cap highs: 3 confirmed steps (+15% each, separate 4h windows, pullbacks ≤ 40%, holders growing) qualify. Paper legs: a starter at qualification, one add per confirmed step after a ≥10% dip, the starter is banked at the first add, each leg trails with the ratchet, and a structure stop (MC −25% vs the last confirmed high or holders shrinking) closes everything. Re-entry follows the universal re-entry controls (SL cooldown first).</HelpHint>
        </span>
        <span className="flex items-center gap-3">
          <span>{tokens.length} tracked · {holding.length} holding · {ladder?.stats?.legs_opened ?? 0} legs</span>
          <span data-testid="ladder-live-status" title="Ladder legs route through the live executor (Sol → runner book, RH → rh_pons live) once 15 paper legs have closed AND that chain's LIVE switch is armed"
            className={`px-1.5 py-0.5 border ${(ladder?.live?.sol || ladder?.live?.rh) ? "border-red-700 text-red-300" : "border-neutral-800 text-neutral-500"}`}>
            {(ladder?.live?.sol || ladder?.live?.rh) ? `LIVE ${[ladder.live.sol && "sol", ladder.live.rh && "rh"].filter(Boolean).join("+")}` : `paper ${ladder?.live?.paper_legs_closed ?? 0}/${ladder?.live?.needed ?? 15}`}
          </span>
          <button data-testid="ladder-toggle" onClick={() => onConfigPatch?.({ ladder_enabled: !enabled })}
            className={`px-2 py-0.5 border uppercase tracking-[0.15em] ${enabled ? "border-emerald-700 text-emerald-300" : "border-neutral-700 text-neutral-500"}`}>{enabled ? "on" : "off"}</button>
        </span>
      </div>
      {tokens.length === 0 ? (
        <div className="text-center py-4 text-[10px] uppercase tracking-[0.2em] text-neutral-600">no graduated token above $50K MC seen yet</div>
      ) : (
        <ul className="space-y-1">{tokens.slice(0, 40).map((t) => <LadderRow key={t.key} t={t} onRemove={remove} />)}</ul>
      )}
    </div>
  );
}

export default memo(GraduateLadderCard);
