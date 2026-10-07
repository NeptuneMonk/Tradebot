import { memo, useState } from "react";
import { Radio, Users, Droplets, Flame, DollarSign } from "lucide-react";
import { ChainBadge, ChainFilterChips } from "./ChainBadge";
import { TokenDetailDialog } from "./TokenDetailDialog";
import { VirtualUl } from "./VirtualRows";

const short = (s) => (s ? `${s.slice(0, 4)}…${s.slice(-4)}` : "—");
const fmtUsd = (n) => {
  const v = Number(n) || 0;
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toFixed(0)}`;
};
const timeAgo = (iso) => {
  if (!iso) return "—";
  const sec = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 60) return `${Math.floor(sec)}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  return `${Math.floor(sec / 3600)}h ago`;
};

const CHAIN_FILTER_KEY = "ui.launches.chain";

export const SOL_GATE_HINT = {
  pass: "cleared the momentum pre-rank — queued for the on-chain check and entry",
  age: "outside the New (Pump.fun) / Seasoned (PumpSwap) age window",
  "no-buy-events": "no buys seen on the tape yet — nothing to score",
  growth: "rolling growth below the band's Min growth %",
  liquidity: "curve / pool SOL below Min liquidity",
  mc: "market cap below the Seasoned Min MC",
  "mc-velocity": "5-minute MC velocity below the Seasoned minimum",
  inflow: "recent SOL inflow below Min inflow",
  "new-buyers": "fresh buyers in the velocity window below Min new buyers",
  "distribution-vacuum": "every holder appeared inside the velocity window — insider pre-distribution pattern",
  "no-pool": "graduated but no PumpSwap pool found yet",
  "pool-state": "PumpSwap pool state could not be read",
  "curve-complete": "bonding curve completed — token has left Pump.fun (waiting for the pool)",
  "curve-state": "bonding curve state could not be read",
  "runner-cap": "runner book is at its position cap",
  "hunt-cap": "hunt book is at its position cap",
  "seasoned-no-pool": "seasoned entry needs a live PumpSwap pool",
  "stale-tape": "seasoned tape is stale — no recent trades",
  "buyers-since-grad": "not enough distinct buyers since graduation",
  "pf-creator-sol": "deployer wallet holds less SOL than the creator-solvency floor",
  "creator-dumped": "deployer sold more than the allowed share of their stake in the dump window",
  "creator-balance-unknown": "deployer balance RPC failed (fail-closed)",
  "classifier skip": "risk classifier declined this launch",
  entry_velocity: "entry velocity cap — too many entries in the last minute",
};

export const RH_GATE_HINT = {
  pass: "cleared every PONS entry gate",
  "sl-cooldown": "stopped out on this token — SL Cooldown (momentum scanner setting) blocks every re-entry until it lapses",
  "reentry-wait": "closed this token moments ago — re-entry waits `Min wait (s)` from the Re-entry settings",
  "reentry-max": "re-entry attempts on this token are used up for the current re-entry window",
  "reentry-off": "re-entry is switched OFF in settings — this token traded inside the re-entry window",
  "already-entered": "position open or a paper fill is queued for this token",
  "cost-gate": "passed the momentum gates but the expected round-trip cost (fees + slippage) eats the first target — entry skipped",
  "r-size": "passed the momentum gates but R-sizing found no tradable size at the current bankroll / stop",
  "fill-rejected": "paper fill rejected — price ran past the live slippage tolerance before the fill block",
  "fill-expired": "queued paper fill never landed (chain head stalled for 2 min) — slot released",
  "head-stalled": "RH poll loop is not advancing — a paper fill lands on a later block, so no entry until the head moves",
  "not-leader": "this pod is not the trading leader — entries are fenced off here",
  "entry-lock": "another pod holds the entry lock for this token",
  "stake-zero": "RH stake is $0 — bankroll too small for the fee floor (see bankroll card)",
  "no-price": "no curve / pool price yet",
  "doctor-breaker": "rh_pons is benched by the Live Doctor breaker",
  "live-buy-failed": "live buy did not land — see RH wallet card / logs",
  "max-positions": "RH max positions reached",
};

function RecentLaunchesFeed({ launches: allLaunches, feedLive = { sol: false, rh: false } }) {
  const [detail, setDetail] = useState(null);
  const [chainFilter, setChainFilter] = useState(() => localStorage.getItem(CHAIN_FILTER_KEY) || "all");
  const setFilter = (k) => { localStorage.setItem(CHAIN_FILTER_KEY, k); setChainFilter(k); };
  const counts = { all: allLaunches.length, sol: 0, rh: 0 };
  for (const l of allLaunches) counts[l.chain === "rh" ? "rh" : "sol"]++;
  const launches = chainFilter === "all"
    ? allLaunches
    : allLaunches.filter((l) => (l.chain === "rh" ? "rh" : "sol") === chainFilter);
  return (
    <div className="control-card flex flex-col" data-testid="recent-launches-card">
      <div className="flex items-center justify-between mb-3 gap-2 flex-wrap">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
          <Radio className="w-3 h-3" /> Launches in window ({launches.length})
        </div>
        <div className="flex items-center gap-2">
          <ChainFilterChips value={chainFilter} onChange={setFilter} counts={counts} />
          {(() => {
            const live = chainFilter === "sol" ? feedLive.sol : chainFilter === "rh" ? feedLive.rh : (feedLive.sol || feedLive.rh);
            return (
              <div className={`flex items-center gap-1.5 text-[10px] uppercase tracking-[0.2em] ${live ? "text-emerald-500" : "text-neutral-500"}`} data-testid="launches-live-chip" title="follows the active tab: Sol → Pump.fun WS, RH → RH poll loop, All → either">
                {live ? <span className="pulse-dot"></span> : <span className="w-2 h-2 rounded-full bg-neutral-600 inline-block"></span>} {live ? "LIVE" : "OFFLINE"}
              </div>
            );
          })()}
        </div>
      </div>
      <VirtualUl items={launches} estimate={68} maxHeightClass="max-h-[280px] md:max-h-[480px]" testId="launches-list"
        empty={<div className="text-center py-6 text-[10px] uppercase tracking-[0.2em] text-neutral-600" data-testid="launches-empty">no tokens inside your age window yet — rows appear as launches age into it</div>}
        renderItem={(l) => {
            const isRh = l.chain === "rh";
            return (
            <div
              data-testid={`launch-row-${l.mint}`}
              className="border border-neutral-800 hover:bg-neutral-900/60 px-3 py-2 mb-1 transition-colors duration-100 relative cursor-pointer"
              onClick={(e) => { if (!e.target.closest("button, a")) setDetail({ chain: l.chain || "sol", mint: l.mint, symbol: l.symbol, name: l.name }); }}
              title="Click for live market data, our record and a manual re-entry"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <ChainBadge chain={l.chain} protocol={isRh ? l.protocol : null} mint={l.mint} />
                    {l.band && !l.entered && (
                      <span className={`text-[10px] font-mono px-1 py-0 border uppercase ${l.band === "seasoned" ? "border-amber-800 text-amber-300" : "border-sky-800 text-sky-300"}`}
                        title={l.band === "seasoned" ? "inside your Seasoned (post-graduation) age window" : l.band === "rh_new" ? "inside your RH age window" : "inside your New (Pump.fun curve) age window"}
                        data-testid={`launch-band-${l.mint}`}>{l.band === "rh_new" ? "window" : l.band}</span>
                    )}
                    <span className="font-mono font-semibold text-sm truncate">
                      {l.symbol || "?"}
                    </span>
                    <span className="text-[10px] font-mono text-neutral-500 truncate">{l.name || "Unknown"}</span>
                    {l.entered && (
                      <span className="text-[10px] font-mono px-1 py-0 border border-blue-700 text-blue-300 uppercase">ENT</span>
                    )}
                    {isRh && l.graduated && (
                      <span className="text-[10px] font-mono px-1 py-0 border border-lime-700 text-lime-300 uppercase" data-testid={`launch-graduated-${l.mint}`}>grad</span>
                    )}
                    {isRh && l.backfilled && (
                      <span className="text-[10px] font-mono px-1 py-0 border border-violet-800 text-violet-300 uppercase" data-testid={`launch-window-${l.mint}`}
                        title="Window discovery: launched before this bot process started, pulled in because it is actively trading inside your RH max-age window">window</span>
                    )}
                    {isRh && l.rh_gate && !l.entered && (
                      <span className={`text-[10px] font-mono px-1 py-0 border uppercase ${l.rh_gate === "pass" ? "border-emerald-700 text-emerald-300" : "border-neutral-700 text-neutral-500"}`}
                        title={(RH_GATE_HINT[l.rh_gate] || `backend gate verdict: ${l.rh_gate}`) + (l.rh_gate_detail ? `\n${l.rh_gate_detail}` : "")}
                        data-testid={`launch-rh-gate-${l.mint}`}>{l.rh_gate === "pass" ? "gate ✓" : `gate: ${l.rh_gate}`}</span>
                    )}
                    {!isRh && l.gate && !l.entered && (
                      <span className={`text-[10px] font-mono px-1 py-0 border uppercase ${l.gate === "pass" ? "border-emerald-700 text-emerald-300" : "border-neutral-700 text-neutral-500"}`}
                        title={(SOL_GATE_HINT[l.gate] || RH_GATE_HINT[l.gate] || `scanner verdict: ${l.gate}`) + (l.gate_detail ? `\n${l.gate_detail}` : "")}
                        data-testid={`launch-gate-${l.mint}`}>{l.gate === "pass" ? "gate ✓" : `gate: ${l.gate}`}</span>
                    )}
                    {(l.creator_eth != null || l.creator_sol != null) && (
                      <span className="text-[10px] font-mono px-1 py-0 border border-neutral-800 text-neutral-500 lowercase"
                        title="deployer native balance at first sight · share of their stake sold in the first minute"
                        data-testid={`launch-creator-solvency-${l.mint}`}>
                        creator {l.creator_eth != null ? `${Number(l.creator_eth).toFixed(3)} ETH` : `${Number(l.creator_sol).toFixed(2)} SOL`}
                        {l.creator_sold_pct != null ? ` · dumped ${Number(l.creator_sold_pct).toFixed(0)}%` : ""}
                      </span>
                    )}
                  </div>
                  <div className="text-[10px] font-mono text-neutral-500 mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5">
                    <span>{isRh ? "token" : "mint"} <span className="text-neutral-300">{short(l.mint)}</span></span>
                    <span>·</span>
                    <span>creator <span className="text-neutral-300">{short(l.creator)}</span></span>
                    <CreatorBadge l={l} />
                  </div>
                  <div className="mt-1.5 flex items-center gap-2 text-[10px] font-mono flex-wrap">
                    <Stat icon={<Users className="w-3 h-3" />} value={l.unique_buyers ?? 0} label="buyers" data-testid={`launch-buyers-${l.mint}`} />
                    {isRh ? (
                      <Stat icon={<Droplets className="w-3 h-3" />} value={Number(l.quote_inflow ?? 0).toFixed(l.quote_symbol === "USDG" ? 0 : 3)} suffix={l.quote_symbol || "ETH"} label="inflow" data-testid={`launch-inflow-${l.mint}`} />
                    ) : (
                      <Stat icon={<Droplets className="w-3 h-3" />} value={(l.sol_inflow ?? 0).toFixed(2)} suffix="SOL" label="inflow" data-testid={`launch-inflow-${l.mint}`} />
                    )}
                    <Stat icon={<Flame className="w-3 h-3" />} value={(l.curve_fill_pct ?? 0).toFixed(0)} suffix="%" label="curve" data-testid={`launch-curve-${l.mint}`} />
                    {isRh && (l.usd_market_cap ?? 0) > 0 && (
                      <Stat icon={<DollarSign className="w-3 h-3" />} value={fmtUsd(l.usd_market_cap)} label="MC" data-testid={`launch-mc-${l.mint}`} />
                    )}
                    {!isRh && <ProjectBadge score={l.project_score} flags={l.project_flags} mint={l.mint} />}
                  </div>
                </div>
                <div className="flex flex-col items-end gap-1 shrink-0">
                  <ActionBadge action={l.classifier_action} risk={l.classifier_risk} entered={l.entered} entryAction={l.entry_action} />
                  {l.entered && l.live_pnl_pct != null && (
                    <PnlBadge pnlPct={l.live_pnl_pct} drawdown={l.live_drawdown_from_peak_pct} mint={l.mint} live={true} />
                  )}
                  <span className="text-[10px] font-mono text-neutral-600">{timeAgo(l.detected_at)}</span>
                </div>
              </div>
            </div>
            );
          }} />
      <TokenDetailDialog token={detail} onClose={() => setDetail(null)} />
    </div>
  );
}

function PnlBadge({ pnlPct, reason, mint, live, drawdown }) {
  if (pnlPct == null) return null;
  const sign = pnlPct >= 0 ? "+" : "";
  const cls = pnlPct >= 0
    ? "border-emerald-700 text-emerald-300 bg-emerald-950/40"
    : "border-rose-800 text-rose-300 bg-rose-950/40";
  // Live PnL pulses subtly so the operator can spot it shifting at a glance.
  const liveCls = live ? " animate-pulse" : "";
  const title = live
    ? `LIVE unrealized PnL ${sign}${pnlPct.toFixed(1)}%${drawdown != null && drawdown > 5 ? ` (down ${drawdown.toFixed(0)}% from peak)` : ""}. Click X on the PIN to manually exit.`
    : (reason ? `exit: ${reason}` : `realized PnL ${sign}${pnlPct.toFixed(1)}%`);
  return (
    <span
      className={`px-1.5 py-0.5 border text-[10px] font-mono uppercase ${cls}${liveCls}`}
      title={title}
      data-testid={`launch-pnl-badge-${mint}`}
    >
      {live && <span className="opacity-70 mr-0.5">●</span>}
      {sign}{pnlPct.toFixed(1)}%
      {live && drawdown != null && drawdown > 5 && (
        <span className="ml-0.5 text-[9px] opacity-70">↓{drawdown.toFixed(0)}</span>
      )}
    </span>
  );
}


function Stat({ icon, value, suffix, label, "data-testid": testid }) {
  return (
    <span data-testid={testid} className="inline-flex items-center gap-1 text-neutral-400" title={label}>
      <span className="text-neutral-600">{icon}</span>
      <span className="text-neutral-200">{value}</span>
      {suffix && <span className="text-neutral-600">{suffix}</span>}
    </span>
  );
}

function CreatorBadge({ l }) {
  const created = l.creator_tokens_created ?? 1;
  const failed = l.creator_tokens_failed ?? 0;
  const graduated = l.creator_tokens_graduated ?? 0;
  if (created <= 1 && failed === 0 && graduated === 0) return null;
  let cls = "border-neutral-800 text-neutral-500";
  if (failed >= 1) cls = "border-red-800 text-red-400 bg-red-950/40";
  else if (graduated >= 1) cls = "border-emerald-800 text-emerald-400 bg-emerald-950/40";
  else if (created >= 3) cls = "border-amber-800 text-amber-400 bg-amber-950/40";
  const tooltip = `Creator: ${created} created · ${graduated} graduated · ${failed} failed`;
  return (
    <span
      title={tooltip}
      data-testid={`launch-creator-stats-${l.mint}`}
      className={`px-1.5 py-0.5 border ${cls} text-[10px] font-mono uppercase`}
    >
      {created}c·{graduated}g·{failed}f
    </span>
  );
}

function ProjectBadge({ score, flags, mint }) {
  const s = score ?? 0;
  const f = flags || {};
  let cls = "border-neutral-800 text-neutral-500";
  if (s >= 4) cls = "border-emerald-700 text-emerald-300 bg-emerald-950/40";
  else if (s >= 2) cls = "border-amber-700 text-amber-300 bg-amber-950/40";
  const yn = (k) => (f[k] ? "✓" : "✗");
  const tooltip = f.meta_seen
    ? `Project Score ${s}/5 · logo ${yn("logo")} · website ${yn("website")} · X ${yn("x")} · creator graduated before ${yn("creator_graduated")} · posts ${yn("posts")}${f.telegram ? " · telegram ✓" : ""}`
    : "Project Score 0–5 (logo · website · X · creator graduated before · posts) — waiting for Pump.fun metadata";
  return (
    <span
      data-testid={`launch-project-${mint}`}
      title={tooltip}
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 border text-[10px] uppercase ${cls}`}
    >
      <span className="font-bold">PRJ</span>
      <span>{f.meta_seen ? `${s}/5` : "…"}</span>
    </span>
  );
}

function ActionBadge({ action, risk, entered, entryAction }) {
  // Verdict is the book router: scalp / hunt / skip. A sniper entry shows SNIPED (hunt book).
  if (entered && entryAction === "greylist_snipe") {
    return (
      <div className="flex items-center gap-1" data-testid="launch-snipe-badge">
        <span className="px-1.5 py-0.5 border text-[10px] font-mono uppercase border-rose-700 text-rose-300 bg-rose-950/40">
          SNIPED
        </span>
        {risk != null && <span className="text-[10px] font-mono text-neutral-500">{risk}</span>}
      </div>
    );
  }
  if (entered && entryAction === "rh_pons_paper") {
    return (
      <div className="flex items-center gap-1" data-testid="launch-rh-paper-badge">
        <span className="px-1.5 py-0.5 border text-[10px] font-mono uppercase border-lime-700 text-lime-300 bg-lime-950/40">
          PAPER
        </span>
      </div>
    );
  }
  let cls = "border-neutral-700 text-neutral-400";
  if (action === "skip") cls = "border-red-800 text-red-400 bg-red-950/40";
  else if (action === "hunt") cls = "border-rose-800 text-rose-300 bg-rose-950/40";
  else if (action === "scalp") cls = "border-emerald-800 text-emerald-400 bg-emerald-950/40";
  else if (action === "tracking") cls = "border-lime-800 text-lime-400 bg-lime-950/30";
  else if (action === "pending") cls = "border-neutral-600 text-neutral-300 bg-neutral-900/60 border-dashed animate-pulse";
  return (
    <div className="flex items-center gap-1">
      <span className={`px-1.5 py-0.5 border text-[10px] font-mono uppercase ${cls}`} data-testid={`launch-verdict-${action || "none"}`}
        title={action === "pending" ? "Feed label only — waiting for tape / metadata / creator pattern; re-assessed at 3s, 8s, 15s. Entries always re-classify on fresh metrics." : undefined}>
        {action === "pending" ? "pending…" : (action || "—").replace("_", " ")}
      </span>
      {risk != null && <span className="text-[10px] font-mono text-neutral-500">{risk}</span>}
    </div>
  );
}


// React.memo wrapper — only re-render when the launches array reference
// changes. With the coalesced flush in Dashboard.jsx, a 20-event burst
// now produces 1 re-render every 400ms instead of 20 per tick.
export default memo(RecentLaunchesFeed);
