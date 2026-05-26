import { Radio, Users, Droplets, Flame, Pin, PinOff, X } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

const short = (s) => (s ? `${s.slice(0, 4)}…${s.slice(-4)}` : "—");
const timeAgo = (iso) => {
  if (!iso) return "—";
  const sec = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 60) return `${Math.floor(sec)}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  return `${Math.floor(sec / 3600)}h ago`;
};

export default function RecentLaunchesFeed({ launches, onUnpin }) {
  const pinnedCount = launches.filter((l) => l.pinned).length;
  return (
    <div className="control-card flex flex-col" data-testid="recent-launches-card">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
          <Radio className="w-3 h-3" /> Recent Launches ({launches.length})
          {pinnedCount > 0 && (
            <span
              className="ml-1 px-1.5 py-0.5 border border-fuchsia-700 text-fuchsia-300 bg-fuchsia-950/40 text-[9px] font-mono uppercase tracking-wider inline-flex items-center gap-1"
              data-testid="launches-pinned-count"
              title="Mints from greylisted creators stay pinned at top until manually unpinned"
            >
              <Pin className="w-2.5 h-2.5" /> {pinnedCount} pinned
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.2em] text-emerald-500">
          <span className="pulse-dot"></span> LIVE
        </div>
      </div>
      <div className="overflow-y-auto max-h-[480px]" data-testid="launches-list">
        {launches.length === 0 && (
          <div className="text-center py-6 text-[10px] uppercase tracking-[0.2em] text-neutral-600">
            listening for launches…
          </div>
        )}
        <ul className="space-y-1">
          {launches.map((l) => {
            const isPinned = !!l.pinned;
            const isPinExited = !!l.pin_exited;
            return (
            <li
              key={l.id}
              data-testid={`launch-row-${l.mint}`}
              className={[
                "border px-3 py-2 transition-colors duration-100 relative",
                isPinned
                  ? (isPinExited
                      ? "border-neutral-700 bg-neutral-900/60 opacity-60 hover:opacity-90"
                      : "border-fuchsia-800/60 bg-fuchsia-950/20 hover:bg-fuchsia-950/30")
                  : "border-neutral-800 hover:bg-neutral-900/60",
              ].join(" ")}
            >
              {isPinned && (
                <div
                  className="absolute top-0 left-0 w-0.5 h-full"
                  style={{ background: isPinExited ? "#525252" : "#a21caf" }}
                />
              )}
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    {isPinned && (
                      <PinBadge l={l} exited={isPinExited} onUnpin={onUnpin} />
                    )}
                    <span className="font-mono font-semibold text-sm truncate">
                      {l.symbol || "?"}
                    </span>
                    <span className="text-[10px] font-mono text-neutral-500 truncate">{l.name || "Unknown"}</span>
                    {l.entered && (
                      <span className="text-[10px] font-mono px-1 py-0 border border-blue-700 text-blue-300 uppercase">ENT</span>
                    )}
                  </div>
                  <div className="text-[10px] font-mono text-neutral-500 mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5">
                    <span>mint <span className="text-neutral-300">{short(l.mint)}</span></span>
                    <span>·</span>
                    <span>creator <span className="text-neutral-300">{short(l.creator)}</span></span>
                    <CreatorBadge l={l} />
                  </div>
                  <div className="mt-1.5 flex items-center gap-2 text-[10px] font-mono">
                    <Stat icon={<Users className="w-3 h-3" />} value={l.unique_buyers ?? 0} label="buyers" data-testid={`launch-buyers-${l.mint}`} />
                    <Stat icon={<Droplets className="w-3 h-3" />} value={(l.sol_inflow ?? 0).toFixed(2)} suffix="SOL" label="inflow" data-testid={`launch-inflow-${l.mint}`} />
                    <Stat icon={<Flame className="w-3 h-3" />} value={(l.curve_fill_pct ?? 0).toFixed(0)} suffix="%" label="curve" data-testid={`launch-curve-${l.mint}`} />
                    <SocialBadge score={l.social_score} sources={l.social_sources} mint={l.mint} />
                  </div>
                </div>
                <div className="flex flex-col items-end gap-1 shrink-0">
                  <ActionBadge action={l.classifier_action} risk={l.classifier_risk} entered={l.entered} entryAction={l.entry_action} />
                  {l.entered && !l.pin_exited && l.live_pnl_pct != null && (
                    <PnlBadge pnlPct={l.live_pnl_pct} drawdown={l.live_drawdown_from_peak_pct} mint={l.mint} live={true} />
                  )}
                  {l.entered && l.pin_exited && l.exit_pnl_pct != null && (
                    <PnlBadge pnlPct={l.exit_pnl_pct} reason={l.exit_reason} mint={l.mint} live={false} />
                  )}
                  <span className="text-[10px] font-mono text-neutral-600">{timeAgo(l.detected_at)}</span>
                </div>
              </div>
            </li>
            );
          })}
        </ul>
      </div>
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

function PinBadge({ l, exited, onUnpin }) {
  const tier = l.pin_strategy || "tier";
  const pattern = l.pin_creator_pattern;
  const tip = exited
    ? "Trade exited — still pinned. Click the X to unpin."
    : `Greylist ${tier}${pattern ? " · " + pattern.replace(/_/g, " ") : ""}. Stays at top until you unpin.`;
  const click = async (e) => {
    e.stopPropagation();
    try {
      await api.unpinLaunch(l.id);
      toast.success(`Unpinned ${l.symbol || "mint"}`);
      onUnpin && onUnpin(l.id);
    } catch (err) {
      toast.error("Unpin failed: " + (err?.response?.data?.detail || err.message));
    }
  };
  return (
    <span
      data-testid={`launch-pin-badge-${l.mint}`}
      title={tip}
      className={[
        "inline-flex items-center gap-1 px-1.5 py-0.5 border text-[9px] font-mono uppercase tracking-wider",
        exited
          ? "border-neutral-700 text-neutral-400 bg-neutral-900"
          : "border-fuchsia-700 text-fuchsia-300 bg-fuchsia-950/40",
      ].join(" ")}
    >
      {exited ? <PinOff className="w-2.5 h-2.5" /> : <Pin className="w-2.5 h-2.5" />}
      {exited ? "EXITED" : "PINNED"}
      <button
        type="button"
        onClick={click}
        data-testid={`launch-unpin-btn-${l.mint}`}
        className="ml-0.5 hover:text-rose-300"
        title="Unpin"
      >
        <X className="w-2.5 h-2.5" />
      </button>
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

function SocialBadge({ score, sources, mint }) {
  const s = score ?? 0;
  let cls = "border-neutral-800 text-neutral-500";
  if (s >= 60) cls = "border-emerald-700 text-emerald-300 bg-emerald-950/40";
  else if (s >= 30) cls = "border-amber-700 text-amber-300 bg-amber-950/40";
  const tooltip = sources
    ? `Reddit hits (1h): ${sources.reddit_hour_hits ?? 0} · CoinGecko: ${sources.coingecko_match ? "yes" : "no"} · Wikipedia: ${sources.wikipedia_exists ? "yes" : "no"}`
    : "Social trending score (Reddit + CoinGecko + Wikipedia)";
  return (
    <span
      data-testid={`launch-social-${mint}`}
      title={tooltip}
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 border text-[10px] uppercase ${cls}`}
    >
      <span className="font-bold">SOC</span>
      <span>{s}</span>
    </span>
  );
}

function ActionBadge({ action, risk, entered, entryAction }) {
  // When the bot ACTUALLY entered the trade via the Greylist Sniper path,
  // the classifier verdict ("abort_trade" / "exit_early") is misleading —
  // the sniper bypasses it on purpose. Show a "SNIPED" badge instead so
  // the operator doesn't think the trade was aborted when it actually
  // entered and ran through the pattern-based exit ladder.
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
  let cls = "border-neutral-700 text-neutral-400";
  if (action === "abort_trade") cls = "border-red-800 text-red-400 bg-red-950/40";
  else if (action === "exit_early") cls = "border-amber-800 text-amber-400 bg-amber-950/40";
  else if (action === "hold_briefly") cls = "border-emerald-800 text-emerald-400 bg-emerald-950/40";
  return (
    <div className="flex items-center gap-1">
      <span className={`px-1.5 py-0.5 border text-[10px] font-mono uppercase ${cls}`}>
        {(action || "—").replace("_", " ")}
      </span>
      {risk != null && <span className="text-[10px] font-mono text-neutral-500">{risk}</span>}
    </div>
  );
}
