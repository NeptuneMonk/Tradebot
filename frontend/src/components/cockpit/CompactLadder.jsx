import { memo, useState } from "react";
import { ChainBadge } from "../ChainBadge";
import { TokenDetailDialog } from "../TokenDetailDialog";

const fmtUsd = (n) => {
  const v = Number(n) || 0;
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toFixed(0)}`;
};

function Steps({ n }) {
  return (
    <span className="inline-flex gap-0.5" title={`${n}/3 confirmed higher-highs`}>
      {[0, 1, 2].map((i) => <span key={i} className={`w-2 h-2 border ${i < n ? "bg-fuchsia-400 border-fuchsia-400" : "border-neutral-700"}`} />)}
    </span>
  );
}

function Row({ t, onOpen }) {
  const holding = t.state === "holding";
  const legPnl = t.legs?.length ? t.legs.reduce((a, l) => a + l.pnl_pct, 0) / t.legs.length : null;
  const state = holding ? `holding ${t.legs.length} leg${t.legs.length === 1 ? "" : "s"}` : t.qualified ? "qualified" : "watching";
  return (
    <tr data-testid={`cockpit-ladder-${t.mint}`} onClick={() => onOpen({ chain: t.chain, mint: t.mint, symbol: t.symbol, name: t.name })}
      className={`border-b border-neutral-900 cursor-pointer hover:bg-neutral-900/50 transition-colors duration-100 ${holding ? "" : "opacity-70"}`}
      title={`MC ${fmtUsd(t.mc)} · high ${fmtUsd(t.high)} · drawdown -${t.drawdown_pct}% · holders ${t.holders} · on ladder ${t.age_h}h`}>
      <td className="py-1.5 pl-3 font-mono text-xs"><ChainBadge chain={t.chain} mint={t.mint} /> <span className="text-neutral-100 ml-1">{t.symbol || "?"}</span> <span className="text-neutral-600 text-[10px]">{fmtUsd(t.mc)}</span></td>
      <td><Steps n={t.steps} /> <span className="font-mono text-[10px] text-neutral-400 ml-1">{t.steps}/3</span></td>
      <td className="font-mono text-[11px] text-neutral-300">{state}</td>
      <td className={`font-mono text-xs text-right pr-3 ${legPnl == null ? "text-neutral-600" : legPnl >= 0 ? "text-emerald-300" : "text-red-300"}`}>
        {legPnl == null ? "—" : `${legPnl >= 0 ? "+" : ""}${legPnl.toFixed(1)}%`}
      </td>
    </tr>
  );
}

function CompactLadder({ ladder, limit = 10 }) {
  const [detail, setDetail] = useState(null);
  const tokens = [...(ladder?.tokens || [])].sort((a, b) => (b.state === "holding") - (a.state === "holding") || (b.steps ?? 0) - (a.steps ?? 0));
  const holding = tokens.filter((t) => t.state === "holding").length;
  const live = ladder?.live?.sol || ladder?.live?.rh;
  return (
    <div className="control-card h-full flex flex-col !p-0" data-testid="cockpit-ladder">
      <div className="px-3 py-2 border-b border-neutral-800 text-[11px] font-mono tracking-[0.2em] text-neutral-200 flex items-center justify-between">
        <span>GRADUATE LADDER</span>
        <span className="text-neutral-500 tracking-normal">{tokens.length} watched · <span className={holding ? "text-emerald-300" : ""}>{holding} holding</span></span>
      </div>
      <div className="flex-1 overflow-auto max-h-[280px]">
        {tokens.length === 0 ? <div className="py-8 text-center text-[10px] uppercase tracking-[0.2em] text-neutral-600">no graduated token above $50K MC yet</div> : (
          <table className="w-full text-xs">
            <thead><tr className="text-[9px] uppercase tracking-[0.2em] text-neutral-500 border-b border-neutral-800">
              <th className="text-left py-1.5 pl-3 font-normal">current</th><th className="text-left font-normal">steps</th><th className="text-left font-normal">status</th><th className="text-right pr-3 font-normal">legs p/l</th></tr></thead>
            <tbody>{tokens.slice(0, limit).map((t) => <Row key={t.key} t={t} onOpen={setDetail} />)}</tbody>
          </table>
        )}
      </div>
      <div className="px-3 py-1.5 border-t border-neutral-800 font-mono text-[11px] flex items-center justify-between" data-testid="ladder-live-status"
        title="Ladder legs route through the live executor once 15 paper legs have closed AND that chain's LIVE switch is armed">
        <span className="text-neutral-500 tracking-[0.15em]">MODE</span>
        <span className={live ? "text-red-300" : "text-neutral-300"}>{live ? `LIVE ${[ladder.live.sol && "sol", ladder.live.rh && "rh"].filter(Boolean).join("+")}` : `paper ${ladder?.live?.paper_legs_closed ?? 0}/${ladder?.live?.needed ?? 15}`}</span>
      </div>
      <TokenDetailDialog token={detail} onClose={() => setDetail(null)} />
    </div>
  );
}

export default memo(CompactLadder);
