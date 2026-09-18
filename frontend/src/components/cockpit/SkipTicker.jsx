import { memo, useState } from "react";
import { Radar } from "lucide-react";
import { TokenDetailDialog } from "../TokenDetailDialog";
import { SOL_GATE_HINT, RH_GATE_HINT } from "../RecentLaunchesFeed";

const CHAIN_CLS = { rh: "text-lime-400", sol: "text-blue-300" };
const fmtAgo = (ts) => { const s = Math.max(0, (Date.now() - ts) / 1000); return s < 60 ? `${Math.floor(s)}s` : `${Math.floor(s / 60)}m`; };

function Chip({ it, onOpen }) {
  const base = it.reason.split(":")[0].split(" (")[0];
  const hint = SOL_GATE_HINT[it.reason] || RH_GATE_HINT[it.reason] || SOL_GATE_HINT[base] || RH_GATE_HINT[base] || "";
  return (
    <button type="button" data-testid={`ticker-chip-${it.mint}`}
      onClick={() => onOpen({ chain: it.chain, mint: it.mint, symbol: it.symbol })}
      title={`${it.symbol || it.mint} · ${it.reason}${it.detail ? `\n${it.detail}` : ""}${hint ? `\n${hint}` : ""}\n${it.seeded ? "last known verdict" : `${fmtAgo(it.ts)} ago`} — click for details`}
      className="inline-flex items-center gap-1.5 px-2.5 h-full font-mono text-[10px] whitespace-nowrap hover:bg-neutral-900 transition-colors duration-100">
      <span className={`uppercase ${CHAIN_CLS[it.chain] || CHAIN_CLS.sol}`}>{it.chain}</span>
      <span className="text-neutral-200 font-semibold">{it.symbol || `${it.mint.slice(0, 4)}…${it.mint.slice(-4)}`}</span>
      <span className="text-neutral-600">·</span>
      <span className="text-amber-300/90">{it.reason}</span>
      {!it.seeded && <span className="text-neutral-700 ml-1">{fmtAgo(it.ts)}</span>}
      <span className="text-neutral-800 ml-2">|</span>
    </button>
  );
}

function SkipTicker({ items, count10m, live }) {
  const [detail, setDetail] = useState(null);
  const list = items || [];
  const duration = Math.max(20, list.length * 2.5);
  return (
    <div data-testid="skip-ticker" className="ticker fixed bottom-0 inset-x-0 z-30 h-8 border-t border-neutral-800 bg-neutral-950/95 backdrop-blur flex items-stretch">
      <div data-testid="skip-ticker-counter" className="flex items-center gap-2 px-3 border-r border-neutral-800 text-[10px] font-mono flex-shrink-0 bg-neutral-950"
        title="Every token the gates refused in the last 10 minutes — proof the bot is looking even when nothing trades">
        <span className={`w-1.5 h-1.5 rounded-full ${live ? "bg-emerald-500 animate-pulse" : "bg-neutral-600"}`} />
        <Radar className="w-3 h-3 text-neutral-500" />
        <span className="text-neutral-300">{count10m}</span>
        <span className="text-neutral-500 uppercase tracking-[0.15em]">skips / 10m</span>
      </div>
      <div className="flex-1 overflow-hidden relative">
        {list.length === 0 ? (
          <div className="h-full flex items-center px-3 text-[10px] font-mono text-neutral-600 uppercase tracking-[0.2em]">waiting for the first gate verdict…</div>
        ) : (
          <div className="ticker-track inline-flex w-max h-full" style={{ animationDuration: `${duration}s` }}>
            {[0, 1].map((rep) => (
              <div key={rep} className="flex h-full flex-shrink-0 pr-8" aria-hidden={rep === 1}>
                {list.map((it) => <Chip key={`${rep}-${it.key}`} it={it} onOpen={setDetail} />)}
              </div>
            ))}
          </div>
        )}
      </div>
      <TokenDetailDialog token={detail} onClose={() => setDetail(null)} />
    </div>
  );
}

export default memo(SkipTicker);
