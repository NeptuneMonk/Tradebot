import { useEffect, useState, useCallback } from "react";
import { Crosshair, Radar, Repeat, TrendingUp, TrendingDown, Layers } from "lucide-react";
import { api } from "@/lib/api";

const ICONS = {
  sniper: Crosshair,
  scanner: Radar,
  reentry: Repeat,
};

const COLORS = {
  sniper: "text-blue-300 border-blue-900/60 bg-blue-950/30",
  scanner: "text-amber-300 border-amber-900/60 bg-amber-950/30",
  reentry: "text-fuchsia-300 border-fuchsia-900/60 bg-fuchsia-950/30",
};

function fmtUsd(n) {
  const v = Number(n) || 0;
  return `${v >= 0 ? "+" : ""}$${v.toFixed(2)}`;
}
function fmtPct(n) {
  const v = Number(n) || 0;
  return `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;
}

function SourceRow({ s }) {
  const Icon = ICONS[s.source] || Layers;
  const wins = s.wins, losses = s.losses;
  const pnlPositive = (s.pnl_usd ?? 0) >= 0;
  return (
    <div
      data-testid={`pl-source-${s.source}`}
      className={`border ${COLORS[s.source]} px-3 py-3 flex flex-col gap-2`}
    >
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em]">
          <Icon className="w-3 h-3" />
          {s.label}
        </span>
        <span className="text-[10px] font-mono text-neutral-400" data-testid={`pl-source-${s.source}-trades`}>
          {s.trades} trades
        </span>
      </div>
      <div className="flex items-baseline gap-3">
        <span
          className={`text-xl font-mono font-semibold ${pnlPositive ? "text-emerald-400" : "text-red-400"}`}
          data-testid={`pl-source-${s.source}-pnl`}
        >
          {fmtUsd(s.pnl_usd)}
        </span>
        <span className="text-[10px] font-mono text-neutral-500">
          {fmtPct(s.avg_pnl_pct)} avg
        </span>
      </div>
      <div className="grid grid-cols-3 gap-1 text-[10px] font-mono">
        <div className="flex items-center gap-1 text-emerald-400">
          <TrendingUp className="w-3 h-3" /> {wins}W
        </div>
        <div className="flex items-center gap-1 text-red-400">
          <TrendingDown className="w-3 h-3" /> {losses}L
        </div>
        <div className="text-neutral-400 text-right" data-testid={`pl-source-${s.source}-winrate`}>
          {s.trades ? `${(s.win_rate_pct ?? 0).toFixed(0)}% win` : "—"}
        </div>
      </div>
      {s.trades > 0 && (
        <div className="text-[10px] font-mono text-neutral-500 flex justify-between">
          <span>best {fmtPct(s.best_pct)}</span>
          <span>worst {fmtPct(s.worst_pct)}</span>
        </div>
      )}
    </div>
  );
}

export default function PLBySourceCard({ refreshSignal }) {
  const [data, setData] = useState(null);
  const [days, setDays] = useState(7);

  const load = useCallback(async () => {
    try {
      setData(await api.plBySource(days));
    } catch (e) { /* swallow */ }
  }, [days]);

  useEffect(() => { load(); }, [load, refreshSignal]);

  const total = data?.total;
  const sources = data?.sources || [];

  return (
    <div className="control-card" data-testid="pl-by-source-card">
      <div className="flex items-center justify-between mb-3">
        <span className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-400">
          <Layers className="w-3 h-3" /> P/L by Source
        </span>
        <div className="flex items-center gap-1 text-[10px] font-mono">
          {[1, 7, 30].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              data-testid={`pl-source-days-${d}`}
              className={`px-2 py-1 border ${
                days === d
                  ? "border-blue-700 text-blue-300 bg-blue-950/40"
                  : "border-neutral-800 text-neutral-500 hover:text-neutral-300"
              }`}
            >
              {d}d
            </button>
          ))}
        </div>
      </div>

      {total && (
        <div className="flex items-baseline justify-between mb-3 px-1">
          <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-500">Total ({data?.days}d)</span>
          <span
            className={`text-base font-mono font-semibold ${
              (total.pnl_usd ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"
            }`}
            data-testid="pl-source-total-pnl"
          >
            {fmtUsd(total.pnl_usd)}
            <span className="text-[10px] text-neutral-500 ml-2">
              {total.trades} trades · {total.trades ? `${(total.win_rate_pct ?? 0).toFixed(0)}% win` : "—"}
            </span>
          </span>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {sources.length > 0
          ? sources.map((s) => <SourceRow key={s.source} s={s} />)
          : ["sniper", "scanner", "reentry"].map((src) => (
              <SourceRow
                key={src}
                s={{
                  source: src,
                  label: {
                    sniper: "Launch Sniper",
                    scanner: "Momentum Scanner",
                    reentry: "Winner Re-entry",
                  }[src],
                  trades: 0,
                  wins: 0,
                  losses: 0,
                  pnl_usd: 0,
                  avg_pnl_pct: 0,
                  win_rate_pct: 0,
                  best_pct: 0,
                  worst_pct: 0,
                }}
              />
            ))}
      </div>
    </div>
  );
}
