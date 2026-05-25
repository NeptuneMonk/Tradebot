import { useEffect, useState } from "react";
import { Receipt, Activity } from "lucide-react";
import HelpHint from "./HelpHint";

const fmtUsd = (v, d = 4) => `$${Number(v || 0).toFixed(d)}`;
const fmtSol = (v, d = 6) => `${Number(v || 0).toFixed(d)} SOL`;

/**
 * Cost Tracker — shows accumulated trading fees and current network
 * conditions. Auto-refreshes every 8s.
 */
export default function CostTrackerCard({ apiBase }) {
  const [data, setData] = useState(null);
  const [net, setNet] = useState(null);
  const [days, setDays] = useState(7);

  useEffect(() => {
    let timer;
    const load = async () => {
      try {
        const [c, n] = await Promise.all([
          fetch(`${apiBase}/api/costs/summary?days=${days}`).then((r) => r.json()),
          fetch(`${apiBase}/api/costs/network`).then((r) => r.json()),
        ]);
        setData(c);
        setNet(n);
      } catch {
        /* swallow — show last-known data */
      }
    };
    load();
    timer = setInterval(load, 8000);
    return () => clearInterval(timer);
  }, [apiBase, days]);

  if (!data) {
    return (
      <div className="control-card text-neutral-500 text-sm" data-testid="cost-tracker-card">
        Loading cost tracker…
      </div>
    );
  }

  const speedMode = net?.speed_mode || "manual";
  const effPriority = net?.effective_priority_fee_microlamports || 0;
  const effSlipBps = net?.effective_slippage_bps || 0;
  const autoCur = net?.auto_tuner_current;

  return (
    <div className="control-card" data-testid="cost-tracker-card">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
          <Receipt className="w-3 h-3" /> Trading Costs
        </div>
        <div className="flex gap-1">
          {[1, 7, 30].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              data-testid={`cost-window-${d}d`}
              className={`px-2 py-0.5 border text-[10px] font-mono uppercase tracking-[0.15em] transition-colors ${
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

      {/* Top stats */}
      <div className="grid grid-cols-2 gap-2 mb-3">
        <Stat label="Trades" value={data.trades}
              hint="Number of completed trades inside the selected window." />
        <Stat label="Fees Total" value={fmtUsd(data.fee_usd_total, 4)} accent="text-amber-300" testid="stat-fee-usd"
              hint="Sum of base network fee + priority fee paid across all trades in the window." />
        <Stat label="Avg / Trade" value={fmtUsd(data.avg_fee_usd_per_trade, 4)}
              hint="Average fee per trade. Should stay well below your average winner to keep EV positive." />
        <Stat
          label="Fee / Notional"
          value={`${(data.fee_as_pct_of_notional || 0).toFixed(2)}%`}
          accent="text-neutral-300"
          hint="Fees as a % of total trade notional. A key health metric — if this approaches your avg winner %, fees are eating the strategy."
        />
      </div>

      {/* Per speed-mode breakdown */}
      {Object.keys(data.by_speed || {}).length > 0 && (
        <div className="border border-neutral-800 mb-3">
          <div className="grid grid-cols-[1.4fr_0.6fr_1fr] bg-neutral-950 text-[10px] uppercase tracking-[0.15em] text-neutral-500 border-b border-neutral-800">
            <div className="px-2 py-1">Mode</div>
            <div className="px-2 py-1 text-right">N</div>
            <div className="px-2 py-1 text-right">Fee SOL</div>
          </div>
          {Object.entries(data.by_speed)
            .sort((a, b) => b[1].fee_sol - a[1].fee_sol)
            .map(([m, v]) => (
              <div key={m} className="grid grid-cols-[1.4fr_0.6fr_1fr] text-xs border-b border-neutral-900 last:border-0">
                <div className="px-2 py-1 font-mono text-neutral-300 uppercase">{m}</div>
                <div className="px-2 py-1 font-mono text-right text-neutral-500">{v.n}</div>
                <div className="px-2 py-1 font-mono text-right text-amber-200">{fmtSol(v.fee_sol)}</div>
              </div>
            ))}
        </div>
      )}

      {/* Live network state */}
      <div className="border-t border-neutral-800 pt-2">
        <div className="flex items-center justify-between mb-1.5">
          <span className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 inline-flex items-center gap-1">
            Live network
            <HelpHint label="Live network">
              The actual priority fee + slippage the bot will use right now, computed from your current Speed Mode (or AUTO live tuner).
            </HelpHint>
          </span>
          <span className="flex items-center gap-1 text-[10px] font-mono uppercase text-emerald-400">
            <Activity className="w-3 h-3" /> {speedMode}
          </span>
        </div>
        <div className="grid grid-cols-2 gap-2 text-[11px] font-mono">
          <Pair label="prio µLamp" value={effPriority.toLocaleString()}
                hint="Effective compute-unit price (micro-lamports) used on the next transaction." />
          <Pair label="slip bps" value={effSlipBps}
                hint="Effective slippage tolerance for entries (basis points; 100 = 1%)." />
          {speedMode === "auto" && (
            <Pair
              label="auto p75"
              value={autoCur != null ? autoCur.toLocaleString() : "polling…"}
              accent="text-fuchsia-300"
              hint="75th-percentile priority fee from a recent block sample — the live target for AUTO mode."
            />
          )}
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value, accent = "text-neutral-200", testid, hint }) {
  return (
    <div className="border border-neutral-800 px-2 py-1.5" data-testid={testid}>
      <div className="text-[9px] uppercase tracking-[0.15em] text-neutral-500 inline-flex items-center gap-1">
        {label}
        {hint && <HelpHint label={`help: ${label}`}>{hint}</HelpHint>}
      </div>
      <div className={`font-mono text-sm ${accent}`}>{value}</div>
    </div>
  );
}

function Pair({ label, value, accent = "text-neutral-200", hint }) {
  return (
    <div className="flex justify-between border border-neutral-900 px-2 py-1">
      <span className="text-neutral-500 text-[10px] uppercase tracking-[0.1em] inline-flex items-center gap-1">
        {label}
        {hint && <HelpHint label={`help: ${label}`}>{hint}</HelpHint>}
      </span>
      <span className={accent}>{value}</span>
    </div>
  );
}
