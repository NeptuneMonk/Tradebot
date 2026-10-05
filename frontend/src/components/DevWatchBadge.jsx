import { Eye, EyeOff } from "lucide-react";

const LABEL = { watching: "crazy watch", away: "watch · away", autopilot: "watch · autopilot", off: "watch · off", dark: "watch · dark" };
const CLS = {
  watching: "border-cyan-700 bg-cyan-950/40 text-cyan-200",
  away: "border-neutral-800 text-neutral-500",
  autopilot: "border-neutral-800 text-neutral-500",
  off: "border-neutral-800 text-neutral-600",
  dark: "border-transparent text-neutral-700",
};

// Header badge for the CRAZY-dev watch: a new launch whose dev ranks CRAZY on reputation.family is bought at min stake
// with no gates and parked as LTH — only while the dashboard is open and Autopilot is not driving.
export default function DevWatchBadge({ watch }) {
  if (!watch) return null;
  const s = watch.state || "dark";
  const last = watch.last_fire ? new Date(watch.last_fire * 1000).toLocaleTimeString() : null;
  const title = {
    watching: `CRAZY-dev watch ARMED — every new Pump.fun launch whose dev ranks CRAZY is bought for $${Number(watch.stake_usd || 0).toFixed(2)} with no gates and parked as a long-term hold (you exit with ✕).`,
    away: "CRAZY-dev watch dormant — no dashboard session seen on the trading pod. Keep this tab open to arm it.",
    autopilot: "CRAZY-dev watch paused — Autopilot is driving; CRAZY launches go through the normal entry path instead.",
    off: "CRAZY-dev watch switched OFF in Controls — CRAZY launches go through the normal entry path.",
    dark: "CRAZY-dev watch dark — REPUTATION_BASE_URL is not set on the backend.",
  }[s] + `\nCRAZY devs seen ${watch.crazy_seen || 0} · auto-bought ${watch.fired || 0} · open positions flipped to LTH ${watch.tagged_open || 0}` +
    `\nskipped: away ${watch.skipped_away || 0} · autopilot ${watch.skipped_autopilot || 0} · kill-switch ${watch.skipped_kill || 0}` +
    (last ? `\nlast buy ${watch.last_symbol || ""} at ${last}` : "");
  const Icon = s === "watching" ? Eye : EyeOff;
  return (
    <span data-testid="dev-watch-badge" data-state={s} title={title}
      className={`flex items-center gap-1.5 px-2 py-0.5 border text-[10px] uppercase tracking-[0.2em] transition-colors ${CLS[s]}`}>
      <Icon className="w-3 h-3" /> {LABEL[s]}{watch.fired ? <span className="text-[9px] normal-case tracking-normal">·{watch.fired}</span> : null}
    </span>
  );
}
