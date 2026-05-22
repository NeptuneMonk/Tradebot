import { Radio } from "lucide-react";

const short = (s) => (s ? `${s.slice(0, 4)}…${s.slice(-4)}` : "—");
const timeAgo = (iso) => {
  if (!iso) return "—";
  const sec = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 60) return `${Math.floor(sec)}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  return `${Math.floor(sec / 3600)}h ago`;
};

export default function RecentLaunchesFeed({ launches }) {
  return (
    <div className="control-card flex flex-col" data-testid="recent-launches-card">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
          <Radio className="w-3 h-3" /> Recent Launches ({launches.length})
        </div>
        <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.2em] text-emerald-500">
          <span className="pulse-dot"></span> LIVE
        </div>
      </div>
      <div className="overflow-y-auto max-h-[420px]" data-testid="launches-list">
        {launches.length === 0 && (
          <div className="text-center py-6 text-[10px] uppercase tracking-[0.2em] text-neutral-600">
            listening for launches…
          </div>
        )}
        <ul className="space-y-1">
          {launches.map((l) => (
            <li key={l.id} data-testid={`launch-row-${l.mint}`} className="border border-neutral-800 px-3 py-2 hover:bg-neutral-900/60 transition-colors duration-100">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-mono font-semibold text-sm truncate">
                      {l.symbol || "?"}
                    </span>
                    <span className="text-[10px] font-mono text-neutral-500 truncate">{l.name || "Unknown"}</span>
                  </div>
                  <div className="text-[10px] font-mono text-neutral-500 mt-0.5">
                    mint <span className="text-neutral-300">{short(l.mint)}</span>
                    <span className="mx-1.5">·</span>
                    creator <span className="text-neutral-300">{short(l.creator)}</span>
                  </div>
                </div>
                <div className="flex flex-col items-end gap-1 shrink-0">
                  <ActionBadge action={l.classifier_action} risk={l.classifier_risk} />
                  <span className="text-[10px] font-mono text-neutral-600">{timeAgo(l.detected_at)}</span>
                </div>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function ActionBadge({ action, risk }) {
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
