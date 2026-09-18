import { memo, useState } from "react";
import { Layers } from "lucide-react";
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
  const state = holding ? `holding ${t.legs.length}` : t.qualified ? "qualified" : "watching";
  return (
    <li data-testid={`cockpit-ladder-${t.mint}`}
      onClick={() => onOpen({ chain: t.chain, mint: t.mint, symbol: t.symbol, name: t.name })}
      className={`grid grid-cols-[auto_1fr_auto_auto_auto] items-center gap-2 px-2 py-1 border-b border-neutral-900 cursor-pointer hover:bg-neutral-900/50 transition-colors duration-100 ${holding ? "bg-emerald-950/20" : ""}`}
      title={`MC ${fmtUsd(t.mc)} · high ${fmtUsd(t.high)} · drawdown -${t.drawdown_pct}% · holders ${t.holders}`}>
      <ChainBadge chain={t.chain} mint={t.mint} />
      <span className="min-w-0 truncate font-mono text-xs font-semibold">{t.symbol || "?"} <span className="text-neutral-600 font-normal">{fmtUsd(t.mc)}</span></span>
      <Steps n={t.steps} />
      <span className={`text-[9px] font-mono uppercase tracking-[0.15em] px-1.5 py-0.5 border ${holding ? "border-emerald-700 text-emerald-300" : t.qualified ? "border-amber-700 text-amber-300" : "border-neutral-800 text-neutral-500"}`}>{state}</span>
      <span className={`font-mono text-xs text-right w-16 ${legPnl == null ? "text-neutral-600" : legPnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
        {legPnl == null ? "—" : `${legPnl >= 0 ? "+" : ""}${legPnl.toFixed(1)}%`}
      </span>
    </li>
  );
}

function CompactLadder({ ladder, limit = 14 }) {
  const [detail, setDetail] = useState(null);
  const tokens = [...(ladder?.tokens || [])].sort((a, b) => (b.state === "holding") - (a.state === "holding") || (b.steps ?? 0) - (a.steps ?? 0));
  const holding = tokens.filter((t) => t.state === "holding").length;
  const live = ladder?.live?.sol || ladder?.live?.rh;
  return (
    <div className="control-card h-full flex flex-col" data-testid="cockpit-ladder">
      <div className="flex items-center justify-between mb-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
        <span className="flex items-center gap-2 text-fuchsia-300"><Layers className="w-3 h-3" /> graduate ladder</span>
        <span className="font-mono">{tokens.length} watched · <span className={holding ? "text-emerald-400" : ""}>{holding} holding</span> · <span className={live ? "text-red-300" : ""}>{live ? "LIVE" : `paper ${ladder?.live?.paper_legs_closed ?? 0}/${ladder?.live?.needed ?? 15}`}</span></span>
      </div>
      {tokens.length === 0 ? (
        <div className="flex-1 flex items-center justify-center text-[10px] uppercase tracking-[0.2em] text-neutral-600 py-6">no graduated token above $50K MC yet</div>
      ) : (
        <ul className="overflow-y-auto max-h-[300px]">{tokens.slice(0, limit).map((t) => <Row key={t.key} t={t} onOpen={setDetail} />)}</ul>
      )}
      <TokenDetailDialog token={detail} onClose={() => setDetail(null)} />
    </div>
  );
}

export default memo(CompactLadder);
