import { useEffect, useState } from "react";
import { Radio, ExternalLink } from "lucide-react";
import { api } from "@/lib/api";
import CopyAddr from "./CopyAddr";
import HelpHint from "./HelpHint";

const ago = (ts) => { const s = Math.max(0, Date.now() / 1000 - ts); return s < 60 ? `${s.toFixed(0)}s` : s < 3600 ? `${(s / 60).toFixed(0)}m` : `${(s / 3600).toFixed(1)}h`; };

export default function SignalsStrip({ onOpen }) {
  const [data, setData] = useState(null);
  useEffect(() => {
    let alive = true;
    const load = () => api.signals(30).then((d) => alive && setData(d)).catch(() => {});
    load();
    const id = setInterval(load, 6000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  if (!data || (!data.signal_only_mode && (data.signals || []).length === 0)) return null;
  const rows = data.signals || [];
  return (
    <div className="border border-sky-900/60 bg-sky-950/10 p-2" data-testid="signals-strip">
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-sky-300 mb-1">
        <Radio className="w-3 h-3" /> Signals · would enter
        <span className="text-neutral-500 normal-case tracking-normal">{data.signal_only_mode ? "signal mode ON — no fills, no positions" : "signal mode off (history)"} · {data.total_session} this session</span>
        <HelpHint label="signals">Tokens that cleared EVERY entry gate while Signal mode is on. Instead of buying, the bot posts them here and to Telegram so you can eyeball the launch. One signal per mint per cooldown.</HelpHint>
      </div>
      {rows.length === 0 && <div className="text-[11px] text-neutral-600 font-mono">nothing has cleared the gates yet</div>}
      <div className="max-h-56 overflow-auto space-y-0.5">
        {rows.map((s) => (
          <div key={s.id} className="grid grid-cols-[52px_1fr_auto] gap-2 items-center text-[11px] font-mono border-t border-neutral-900 py-1" data-testid={`signal-${s.mint}`}>
            <span className="text-neutral-600">{ago(s.ts)}</span>
            <span className="truncate">
              <button type="button" className="text-neutral-100 hover:text-sky-300" onClick={() => onOpen?.({ mint: s.mint, symbol: s.symbol, chain: "sol" })} data-testid={`signal-open-${s.mint}`}>{s.symbol || "?"}</button>
              <span className="text-neutral-500"> · {s.band} · {s.action} · MC ${Number(s.mc_usd || 0).toLocaleString()} · curve {s.curve_fill_pct}% · {s.depth_sol} SOL · {s.age_min} min · {s.buyers} buyers
                {s.growth_pct != null ? ` · ${Number(s.growth_pct) >= 0 ? "+" : ""}${Number(s.growth_pct).toFixed(0)}%` : ""}{s.smart ? " · SMART" : ""}</span>
            </span>
            <span className="flex items-center gap-2 text-neutral-500">
              <CopyAddr value={s.mint} label="Mint" />
              <a href={`https://pump.fun/coin/${s.mint}`} target="_blank" rel="noreferrer" className="hover:text-sky-300" title="open on pump.fun" data-testid={`signal-link-${s.mint}`}><ExternalLink className="w-3 h-3" /></a>
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
