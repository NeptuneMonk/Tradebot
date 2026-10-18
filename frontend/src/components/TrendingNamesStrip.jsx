import { useEffect, useState } from "react";
import { Flame, Crown } from "lucide-react";
import { api } from "@/lib/api";

const short = (s) => (s ? `${s.slice(0, 4)}…${s.slice(-4)}` : "—");
const mc = (v) => (v >= 1000 ? `$${(v / 1000).toFixed(0)}k` : `$${Number(v || 0).toFixed(0)}`);

// Names being cloned right now: copies, the leader smart wallets are piling into, and what we already hold.
export default function TrendingNamesStrip({ onOpen }) {
  const [data, setData] = useState(null);
  useEffect(() => {
    let alive = true;
    const load = () => api.trendingNames().then((d) => alive && setData(d)).catch(() => {});
    load();
    const id = setInterval(load, 8000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  const list = data?.clusters || [];
  if (!list.length) return null;
  const cfg = data?.config || {};
  return (
    <div className="mb-3 border border-orange-700/70 bg-orange-950/20 p-2" data-testid="trending-names-strip">
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-orange-300 mb-1.5">
        <Flame className="w-3 h-3" /> Trending names · {list.length}
        <span className="normal-case tracking-normal text-neutral-400">
          ≥ {cfg.trend_name_min_copies ?? 3} copies of one ticker inside the band window → the name is trending{cfg.trend_name_hunt_enabled !== false ? `: every copy from the last ${cfg.trend_name_search_window_h ?? 4}h is ranked (outside the time gates) and ONLY the top dog is bought` : " — the bot buys ONLY the leader"} (most smart wallets → MC → buys)
          {cfg.trend_name_leader_min_smart > 0 ? `, and only once ≥ ${cfg.trend_name_leader_min_smart} smart wallet bought it` : ""}; max {cfg.trend_name_max_positions ?? 2} positions per name
        </span>
      </div>
      <div className="space-y-1">
        {list.map((c) => {
          const L = c.leader;
          const buyable = L.smart_total >= (cfg.trend_name_leader_min_smart ?? 1);
          return (
            <div key={c.key} data-testid={`trending-name-${c.key}`}
              className="flex items-center justify-between gap-2 px-2 py-1.5 border border-orange-800/60 bg-neutral-950/60 cursor-pointer"
              onClick={(e) => { if (!e.target.closest("button, a")) onOpen?.({ chain: "sol", mint: L.mint, symbol: L.symbol, name: L.name }); }}>
              <div className="min-w-0 flex items-center gap-2 flex-wrap font-mono">
                <span className="text-sm font-semibold text-orange-200 uppercase">{c.key}</span>
                <span className="text-[10px] text-neutral-400">×{c.copies} copies{c.wide ? ` in ${c.window_h}h` : ""}</span>
                {c.wide && <span className="text-[9px] uppercase tracking-[0.15em] px-1 border border-sky-800 text-sky-300" title="Wide search: every copy of this ticker in the last N hours was ranked, outside the band time gates" data-testid={`trending-wide-${c.key}`}>top dog · {c.window_h}h</span>}
                <span className={`inline-flex items-center gap-1 text-[9px] uppercase tracking-[0.15em] px-1.5 py-0.5 border ${buyable ? "border-emerald-700 text-emerald-300 bg-emerald-950/40" : "border-neutral-700 text-neutral-400"}`}
                  data-testid={`trending-leader-${c.key}`} title={buyable ? "Leader — the only copy the bot may buy" : "Leader by MC/buyers, but no smart wallet has bought any copy yet — nothing is bought"}>
                  <Crown className="w-3 h-3" /> leader {L.symbol || "?"} {short(L.mint)}
                </span>
                <span className="text-[10px] text-amber-300">{L.smart_total} smart{L.smart_manual ? ` (${L.smart_manual} manual)` : ""}</span>
                <span className="text-[10px] text-neutral-300">{mc(L.mc_usd)} MC</span>
                <span className="text-[10px] text-neutral-400">{L.buyers} buyers</span>
                {c.held?.length > 0 && <span className="text-[10px] text-sky-300" data-testid={`trending-held-${c.key}`}>holding {c.held.length}{c.held.includes(L.mint) ? " (leader)" : " (not the leader)"}</span>}
              </div>
              <span className="text-[9px] font-mono text-neutral-500 whitespace-nowrap text-right">
                {c.smart_total} smart across copies
                {c.verdict && <span className={`block ${c.verdict.startsWith("ENTERED") ? "text-emerald-300" : "text-neutral-400"}`} data-testid={`trending-verdict-${c.key}`}>{c.verdict}</span>}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
