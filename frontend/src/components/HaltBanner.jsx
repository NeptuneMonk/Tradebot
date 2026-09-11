import { useEffect, useState, useRef } from "react";
import { OctagonAlert, PauseCircle, Rocket, Unlock } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

const fmtLeft = (untilTs) => {
  const s = Math.max(0, Math.round(untilTs - Date.now() / 1000));
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
};

// Header banner: why is the bot quiet? Inventory halt (last 5 Solana closes were stop-outs/rugs) and
// per-book live-doctor breaker pauses (payoff < 1 or MFE can't reach the first target), with countdowns.
export default function HaltBanner() {
  const [inv, setInv] = useState(null);
  const loadRef = useRef(() => {});
  const load = () => loadRef.current();
  const [, tick] = useState(0);
  useEffect(() => {
    let alive = true;
    const load = () => api.inventory().then((d) => alive && setInv(d)).catch(() => {});
    loadRef.current = load;
    load();
    const poll = setInterval(load, 30000);
    const clock = setInterval(() => tick((n) => n + 1), 1000);
    return () => { alive = false; clearInterval(poll); clearInterval(clock); };
  }, []);
  if (!inv) return null;
  const now = Date.now() / 1000;
  const paused = Object.entries(inv.book_paused_until || {}).filter(([, ts]) => ts > now);
  const runnerFull = (inv.runner_open || 0) >= (inv.runner_cap || 1);
  if (!inv.halted && paused.length === 0 && !runnerFull) return null;
  return (
    <div className="border-b border-amber-900/70 bg-amber-950/40 px-6 py-2 flex flex-wrap items-center gap-x-6 gap-y-1 text-[11px] font-mono" data-testid="halt-banner">
      {inv.halted && (
        <span className="inline-flex items-center gap-1.5 text-amber-200" data-testid="inventory-halt">
          <OctagonAlert className="w-3.5 h-3.5" />
          INVENTORY HALT — last {inv.trigger_n} Solana closes were stop-outs/rugs · no new Solana entries for {fmtLeft(inv.halted_until)}
        </span>
      )}
      {runnerFull && (
        <span className="inline-flex items-center gap-1.5 text-fuchsia-300" data-testid="runner-slot-full">
          <Rocket className="w-3.5 h-3.5" />
          RUNNER SLOT FULL — {(inv.runners || []).map((r) => `${r.symbol || r.mint.slice(0, 6)} · ${r.stage}`).join(", ")} · hunt cap {inv.hunt_cap_now}/{inv.hunt_slot_cap} · no second promotion
        </span>
      )}
      {paused.map(([book, ts]) => {
        const br = (inv.book_breakers || {})[book] || {};
        return (
          <span key={book} className="inline-flex items-center gap-1.5 text-amber-300" data-testid={`book-paused-${book}`}>
            <PauseCircle className="w-3.5 h-3.5" />
            {book.toUpperCase()} paused by live-doctor breaker{br.reason ? ` — ${br.reason}` : ""} · resumes in {fmtLeft(ts)}
            <button
              type="button"
              data-testid={`lift-breaker-${book}`}
              title={`Lift the ${book} breaker now — the Doctor may re-arm it on the next cycle if the payoff is still < 1.0`}
              onClick={() => api.doctorLiveLift(book).then((r) => { toast.success(`${book} breaker lifted`); load(); }).catch((e) => toast.error(e?.response?.data?.detail || e.message))}
              className="ml-1 px-1.5 border border-amber-800 hover:bg-amber-900/40 uppercase text-[9px] inline-flex items-center gap-1"
            >
              <Unlock className="w-3 h-3" /> lift
            </button>
          </span>
        );
      })}
    </div>
  );
}
