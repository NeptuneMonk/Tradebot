import { memo, useState } from "react";
import { Telescope } from "lucide-react";
import { ChainBadge } from "../ChainBadge";
import { TokenDetailDialog } from "../TokenDetailDialog";

const BAND_CLS = { new: "text-amber-300", seasoned: "text-cyan-300", rh_new: "text-lime-300" };
const BAND_LABEL = { new: "new", seasoned: "seasoned", rh_new: "rh" };

function Row({ c, onOpen }) {
  const growth = c.growth_pct ?? 0;
  const gate = c.passes ? "pass" : (c.gate_reason || "watching");
  return (
    <li data-testid={`cockpit-cand-${c.mint}`}
      onClick={() => onOpen({ chain: c.chain || "sol", mint: c.mint, symbol: c.symbol, name: c.name })}
      className={`grid grid-cols-[auto_1fr_auto_auto_auto] items-center gap-2 px-2 py-1 border-b border-neutral-900 cursor-pointer hover:bg-neutral-900/50 transition-colors duration-100 ${c.passes ? "bg-emerald-950/20" : ""}`}
      title="Click for live market data, our record and a manual buy">
      <ChainBadge chain={c.chain} mint={c.mint} />
      <span className="min-w-0 truncate font-mono text-xs font-semibold">{c.symbol || "?"} <span className="text-neutral-600 font-normal">{c.name || ""}</span></span>
      <span className={`text-[9px] font-mono uppercase tracking-[0.15em] ${BAND_CLS[c.band] || "text-neutral-500"}`}>{BAND_LABEL[c.band] || c.band}</span>
      <span data-testid={`cockpit-cand-gate-${c.mint}`}
        className={`text-[9px] font-mono px-1.5 py-0.5 border ${c.passes ? "border-emerald-700 text-emerald-300" : "border-neutral-800 text-neutral-500"}`}
        title={c.gate_detail || ""}>{gate}</span>
      <span className={`font-mono text-xs text-right w-16 ${growth >= 0 ? "text-emerald-400" : "text-red-400"}`}>{growth >= 0 ? "+" : ""}{growth.toFixed(0)}%</span>
    </li>
  );
}

function CompactCandidates({ candidates, limit = 14 }) {
  const [detail, setDetail] = useState(null);
  const sorted = [...(candidates || [])].sort((a, b) => (b.passes === true) - (a.passes === true) || (b.growth_pct ?? 0) - (a.growth_pct ?? 0));
  const passing = sorted.filter((c) => c.passes).length;
  return (
    <div className="control-card h-full flex flex-col" data-testid="cockpit-candidates">
      <div className="flex items-center justify-between mb-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
        <span className="flex items-center gap-2"><Telescope className="w-3 h-3" /> candidates</span>
        <span className="font-mono">{sorted.length} tracked · <span className={passing ? "text-emerald-400" : ""}>{passing} passing</span></span>
      </div>
      {sorted.length === 0 ? (
        <div className="flex-1 flex items-center justify-center text-[10px] uppercase tracking-[0.2em] text-neutral-600 py-6">scanner is warming up</div>
      ) : (
        <ul className="overflow-y-auto max-h-[300px]">{sorted.slice(0, limit).map((c) => <Row key={`${c.chain}-${c.mint}`} c={c} onOpen={setDetail} />)}</ul>
      )}
      <TokenDetailDialog token={detail} onClose={() => setDetail(null)} />
    </div>
  );
}

export default memo(CompactCandidates);
