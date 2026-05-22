import { Repeat, X } from "lucide-react";
import { api } from "@/lib/api";
import { toast } from "sonner";

const short = (s) => (s ? `${s.slice(0, 4)}…${s.slice(-4)}` : "—");
const fmtCountdown = (s) => {
  if (s <= 0) return "expired";
  if (s < 60) return `${Math.floor(s)}s`;
  return `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
};

export default function ReentryWatchCard({ watchlist, onRefresh }) {
  const remove = async (mint) => {
    try {
      await api.removeReentry(mint);
      toast.success("Removed from watchlist");
      onRefresh && onRefresh();
    } catch (e) {
      toast.error("Remove failed");
    }
  };

  return (
    <div className="control-card" data-testid="reentry-watch-card">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
          <Repeat className="w-3 h-3" /> Re-entry Watch ({watchlist.length})
        </div>
        <span className="text-[10px] font-mono text-neutral-600">Pullback re-entry on winners</span>
      </div>

      {watchlist.length === 0 ? (
        <div className="text-center py-6 text-[10px] uppercase tracking-[0.2em] text-neutral-600">
          no profitable exits awaiting re-entry
        </div>
      ) : (
        <ul className="space-y-1">
          {watchlist.map((w) => (
            <li
              key={w.mint}
              data-testid={`reentry-row-${w.mint}`}
              className="border border-neutral-800 px-3 py-2 flex items-center justify-between gap-3"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="font-mono font-semibold text-sm truncate">{w.symbol || "?"}</span>
                  <span className="text-[10px] font-mono text-neutral-500 truncate">{w.name || ""}</span>
                </div>
                <div className="text-[10px] font-mono text-neutral-500 mt-0.5">
                  mint <span className="text-neutral-300">{short(w.mint)}</span>
                  <span className="mx-1.5">·</span>
                  exit <span className="text-emerald-400">+${(w.original_pnl_usd ?? 0).toFixed(2)}</span>
                </div>
                <div className="text-[10px] font-mono text-neutral-500 mt-0.5">
                  awaiting <span className="text-amber-400">-{w.pullback_pct?.toFixed(0)}%</span> pullback
                  <span className="mx-1.5">·</span>
                  attempts <span className="text-neutral-300">{w.attempts}/{w.max_attempts}</span>
                  <span className="mx-1.5">·</span>
                  expires <span className="text-neutral-300" data-testid={`reentry-countdown-${w.mint}`}>{fmtCountdown(w.remaining_window_s ?? 0)}</span>
                </div>
              </div>
              <button
                onClick={() => remove(w.mint)}
                data-testid={`reentry-remove-${w.mint}`}
                className="shrink-0 p-1 border border-neutral-800 hover:bg-neutral-800 text-neutral-500"
                title="Remove from watchlist"
              >
                <X className="w-3 h-3" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
