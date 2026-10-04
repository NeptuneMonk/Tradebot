import { Feather } from "lucide-react";

// Header badge for the lite-mode watchdog: visible only while the bot is shedding non-essential work.
export default function LiteModeBadge({ lite }) {
  if (!lite) return null;
  const on = !!lite.active;
  const title = on
    ? `LITE MODE — ${lite.reason || "resources over the line"}\nPaused: token socials/images, Doctor cycle, seasoned refresh, launch persistence; tracker cut to 60 mints.\nTrading, exits and the live feed keep running. Clears by itself ~60 s after RAM/lag calm down.\nRAM ${lite.rss_mb} MB · loop lag ${lite.lag_ms} ms · trips ${lite.trips}${lite.forced != null ? " · operator override" : ""}`
    : `Watchdog armed · RAM ${lite.rss_mb} MB · loop lag ${lite.lag_ms} ms${lite.trips ? ` · tripped ${lite.trips}×` : ""}`;
  return (
    <span data-testid="lite-mode-badge" title={title}
      className={`flex items-center gap-1.5 px-2 py-0.5 border text-[10px] uppercase tracking-[0.2em] transition-colors ${on ? "border-amber-700 bg-amber-950/50 text-amber-200 animate-pulse" : "border-transparent text-neutral-600"}`}>
      <Feather className="w-3 h-3" /> {on ? "lite mode" : `${Math.round(lite.rss_mb)}mb`}
    </span>
  );
}
