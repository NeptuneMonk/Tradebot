import { Telescope, TrendingUp, Sparkles, Hourglass } from "lucide-react";
import HelpHint from "./HelpHint";

const short = (s) => (s ? `${s.slice(0, 4)}…${s.slice(-4)}` : "—");
const fmtAge = (s) => {
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
};
const fmtUsd = (n) => {
  const v = Number(n) || 0;
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toFixed(0)}`;
};

function Band({ title, Icon, accentClass, items, emptyText }) {
  const passing = items.filter((c) => c.passes);
  const watching = items.filter((c) => !c.passes).slice(0, 8);
  return (
    <div className="border border-neutral-800 p-3" data-testid={`scanner-band-${title.toLowerCase().split(" ")[0]}`}>
      <div className={`flex items-center justify-between mb-2 ${accentClass}`}>
        <span className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.2em]">
          <Icon className="w-3 h-3" /> {title}
        </span>
        <span className="text-[10px] font-mono text-neutral-500">
          {items.length} tracked · {passing.length} passing
        </span>
      </div>
      {items.length === 0 ? (
        <div className="text-center py-4 text-[10px] uppercase tracking-[0.2em] text-neutral-600">
          {emptyText}
        </div>
      ) : (
        <div className="space-y-2">
          {passing.length > 0 && (
            <div>
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

export default function ScannerCandidatesCard({ candidates, config }) {
  // Protocol-aware bands: NEW = pumpfun [band_new_min_age_min, band_new_max_age_min] min
  //                        SEASONED = pumpswap [band_seasoned_min_age_min, band_seasoned_max_age_min] min
  const newMin = config?.band_new_min_age_min ?? 0;
  const newMax = config?.band_new_max_age_min ?? 15;
  const seasonedMin = config?.band_seasoned_min_age_min ?? 0;
  const seasonedMax = config?.band_seasoned_max_age_min ?? 60;
  const fmt = (m) => (m >= 60 ? `${(m / 60).toFixed(m % 60 ? 1 : 0)}h` : `${m % 1 ? m.toFixed(2) : m}m`);
  const newRange = newMin > 0 ? `${fmt(newMin)}–${fmt(newMax)}` : `< ${fmt(newMax)}`;
  const seasonedRange = seasonedMin > 0 ? `${fmt(seasonedMin)}–${fmt(seasonedMax)}` : `< ${fmt(seasonedMax)}`;
  const newBand = candidates.filter((c) => c.band === "new");
  const seasonedBand = candidates.filter((c) => c.band === "seasoned");
  const totalPassing = candidates.filter((c) => c.passes).length;

  return (
    <div className="control-card" data-testid="scanner-card">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
          <Telescope className="w-3 h-3" /> Momentum Scanner ({candidates.length})
          <HelpHint label="Momentum Scanner">
            Live feed of tokens passing your per-band gates. Bands are <strong>protocol-segregated</strong>: <span className="text-amber-300">New</span> = Pump.fun bonding curve only, <span className="text-cyan-300">Seasoned</span> = PumpSwap AMM (graduated) only. Only "Passing" rows are eligible for entry.
          </HelpHint>
        </div>
        <span className="text-[10px] font-mono text-neutral-600">
          new {newRange} · seasoned {seasonedRange} · {totalPassing} passing
        </span>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <Band
          title={`New (Pump.fun · ${newRange})`}
          Icon={Sparkles}
          accentClass="text-amber-300"
          items={newBand}
          emptyText="no fresh tokens meeting momentum criteria"
        />
        <Band
          title={`Seasoned (PumpSwap · ${seasonedRange})`}
          Icon={Hourglass}
          accentClass="text-cyan-300"
          items={seasonedBand}
          emptyText="no seasoned tokens meeting momentum criteria"
        />
      </div>
    </div>
  );
}

function CandidateRow({ c, passing }) {
  const growth = c.growth_pct ?? 0;
  const growthCls = growth >= 0 ? "text-emerald-400" : "text-red-400";
  const discovered = c.discovered === true;
  const isPumpSwap = c.protocol === "pumpswap";
  return (
    <li
      data-testid={`scanner-row-${c.mint}`}
      className={`border px-3 py-2 ${passing ? "border-emerald-800 bg-emerald-950/20" : "border-neutral-800"}`}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-mono font-semibold text-sm truncate">{c.symbol || "?"}</span>
            <span className="text-[10px] font-mono text-neutral-500 truncate">{c.name || ""}</span>
            <span
              className="text-[10px] font-mono text-neutral-600"
              data-testid={`scanner-row-age-${c.mint}`}
            >
              {fmtAge(c.age_s)} old
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
            {isPumpSwap && (
              <span
                className="text-[9px] font-mono uppercase tracking-[0.15em] px-1.5 py-0.5 border border-emerald-700 text-emerald-300 bg-emerald-950/40"
                data-testid={`scanner-row-pumpswap-${c.mint}`}
                title="Graduated to PumpSwap AMM"
              >
                pumpswap
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
      <div className="flex items-center gap-x-3 gap-y-0.5 mt-1.5 text-[10px] font-mono text-neutral-400 flex-wrap">
        {c.band === "new" ? (
          <>
            <span className="inline-flex items-center gap-1">
              inflow(5m) <span className="text-neutral-200">{(c.recent_inflow_sol ?? 0).toFixed(2)}</span> SOL
              <HelpHint label="inflow(5m)">Net SOL flowing into the bonding curve over the configured inflow window. Strong actual demand — not just price wiggle.</HelpHint>
            </span>
            <span className="inline-flex items-center gap-1">
              new buyers(1m) <span className="text-neutral-200">{c.new_buyers_recent ?? 0}</span>
              <HelpHint label="new buyers(1m)">Distinct new wallets that bought in the last 60s. Filters fake pumps from a single wallet round-tripping.</HelpHint>
            </span>
            <span className="inline-flex items-center gap-1">
              holders <span className="text-neutral-200">{c.unique_buyers_total ?? 0}</span>
              <HelpHint label="holders">Total unique buyers seen on this token since the listener started tracking it.</HelpHint>
            </span>
            <span className="inline-flex items-center gap-1">
              buys <span className="text-neutral-200">{c.buy_count ?? 0}</span>
              <HelpHint label="buys">Cumulative buy count from the Pump.fun coin API.</HelpHint>
            </span>
          </>
        ) : (
          <>
            <span className="inline-flex items-center gap-1">
              MC vel(5m){" "}
              <span className={`${(c.mc_velocity_5m_pct ?? 0) >= 0 ? "text-emerald-300" : "text-red-300"}`}>
                {(c.mc_velocity_5m_pct ?? 0) >= 0 ? "+" : ""}{(c.mc_velocity_5m_pct ?? 0).toFixed(1)}%
              </span>
              <HelpHint label="MC vel(5m)">% change in market cap over the last 5 minutes (polled from Pump.fun API). Primary Seasoned-band momentum signal.</HelpHint>
            </span>
            <span className="inline-flex items-center gap-1">
              buys <span className="text-neutral-200">{c.buy_count ?? 0}</span>
              <HelpHint label="buys">Cumulative buy count from the Pump.fun coin API.</HelpHint>
            </span>
          </>
        )}
        {c.usd_market_cap > 0 && (
          <span className="inline-flex items-center gap-1">
            MC <span className="text-neutral-200">{fmtUsd(c.usd_market_cap)}</span>
            <HelpHint label="MC">Current USD market cap (Pump.fun API).</HelpHint>
          </span>
        )}
        {c.last_trade_age_s != null && (
          <span className="inline-flex items-center gap-1">
            last trade <span className="text-neutral-200">{fmtAge(c.last_trade_age_s)} ago</span>
            <HelpHint label="last trade">How long since the most recent trade landed on this token. Stale tokens get pruned from the scanner.</HelpHint>
          </span>
        )}
      </div>
    </li>
  );
}
