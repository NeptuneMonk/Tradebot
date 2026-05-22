import { Telescope, TrendingUp } from "lucide-react";

const short = (s) => (s ? `${s.slice(0, 4)}…${s.slice(-4)}` : "—");
const fmtAge = (s) => {
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
};

export default function ScannerCandidatesCard({ candidates, config }) {
  const passing = candidates.filter((c) => c.passes);
  const watching = candidates.filter((c) => !c.passes).slice(0, 8);
  const winH = config?.scanner_window_hours ?? 4;
  const minAge = config?.scanner_min_age_minutes ?? 180;
  const minAgeLabel = minAge >= 60 ? `${(minAge / 60).toFixed(minAge % 60 ? 1 : 0)}h` : `${minAge}m`;

  return (
    <div className="control-card" data-testid="scanner-card">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
          <Telescope className="w-3 h-3" /> Momentum Scanner ({candidates.length})
        </div>
        <span className="text-[10px] font-mono text-neutral-600">
          {minAgeLabel}–{winH}h window · {passing.length} passing
        </span>
      </div>

      {candidates.length === 0 ? (
        <div className="text-center py-6 text-[10px] uppercase tracking-[0.2em] text-neutral-600">
          building {minAgeLabel}–{winH}h watch window… (scanner kicks in once tokens season)
        </div>
      ) : (
        <div className="overflow-y-auto max-h-[360px]" data-testid="scanner-list">
          {passing.length > 0 && (
            <div className="mb-3">
              <div className="text-[10px] uppercase tracking-[0.15em] text-emerald-500 mb-1 flex items-center gap-1">
                <TrendingUp className="w-3 h-3" /> Passing
              </div>
              <ul className="space-y-1">
                {passing.map((c) => <CandidateRow key={c.mint} c={c} passing />)}
              </ul>
            </div>
          )}
          {watching.length > 0 && (
            <div>
              <div className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 mb-1">Watching</div>
              <ul className="space-y-1">
                {watching.map((c) => <CandidateRow key={c.mint} c={c} />)}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function CandidateRow({ c, passing }) {
  const growth = c.growth_pct ?? 0;
  const growthCls = growth >= 0 ? "text-emerald-400" : "text-red-400";
  const seasoned = c.seasoned !== false; // default true for legacy responses
  const discovered = c.discovered === true;
  return (
    <li
      data-testid={`scanner-row-${c.mint}`}
      className={`border px-3 py-2 ${passing ? "border-emerald-800 bg-emerald-950/20" : "border-neutral-800"}`}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-mono font-semibold text-sm truncate">{c.symbol || "?"}</span>
            <span className="text-[10px] font-mono text-neutral-500 truncate">{c.name || ""}</span>
            <span
              className={`text-[10px] font-mono ${seasoned ? "text-neutral-600" : "text-amber-500"}`}
              data-testid={`scanner-row-age-${c.mint}`}
              title={seasoned ? "Past min age" : "Below min age — scanner won't enter yet"}
            >
              {fmtAge(c.age_s)} old{seasoned ? "" : " · raw"}
            </span>
            {discovered && (
              <span
                className="text-[9px] font-mono uppercase tracking-[0.15em] px-1.5 py-0.5 border border-cyan-800 text-cyan-300 bg-cyan-950/40"
                data-testid={`scanner-row-discovered-${c.mint}`}
                title="Pulled from Pump.fun API (existed before bot started)"
              >
                discovered
              </span>
            )}
          </div>
          <div className="text-[10px] font-mono text-neutral-500 mt-0.5">
            mint <span className="text-neutral-300">{short(c.mint)}</span>
          </div>
        </div>
        <div className={`text-right font-mono text-sm ${growthCls}`} title="Growth from first-seen price">
          {growth >= 0 ? "+" : ""}{growth.toFixed(1)}%
        </div>
      </div>
      <div className="flex items-center gap-3 mt-1.5 text-[10px] font-mono text-neutral-400">
        <span>inflow(5m) <span className="text-neutral-200">{(c.recent_inflow_sol ?? 0).toFixed(2)}</span> SOL</span>
        <span>new buyers(1m) <span className="text-neutral-200">{c.new_buyers_recent ?? 0}</span></span>
        <span>total holders <span className="text-neutral-200">{c.unique_buyers_total ?? 0}</span></span>
      </div>
    </li>
  );
}
