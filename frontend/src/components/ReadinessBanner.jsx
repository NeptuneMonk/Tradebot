import { useEffect, useState } from "react";
import { Siren, Play } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

// Red strip: RH is not trading and here is exactly why (stopped-after-restart, feed off, not armed,
// env/wallet missing in this container, breaker, dead poller, kill switch). One-click Start when that is the fix.
export default function ReadinessBanner({ onChanged }) {
  const [r, setR] = useState(null);
  const [starting, setStarting] = useState(false);
  useEffect(() => {
    let alive = true;
    const load = () => api.readiness().then((d) => alive && setR(d)).catch(() => {});
    load();
    const poll = setInterval(load, 20000);
    return () => { alive = false; clearInterval(poll); };
  }, []);
  if (!r || r.trading) return null;
  const stopped = r.checks && r.checks.bot_enabled === false;
  const start = async () => {
    setStarting(true);
    try {
      await api.start();
      toast.success("Bot started");
      setR((prev) => prev && { ...prev, trading: prev.reasons.length <= 1, reasons: prev.reasons.filter((x) => !x.startsWith("bot STOPPED")), checks: { ...prev.checks, bot_enabled: true } });
      onChanged && onChanged();
    } catch (e) {
      toast.error(`Start failed: ${e?.response?.data?.detail || e.message}`);
    } finally {
      setStarting(false);
    }
  };
  return (
    <div className="border-b border-red-900/70 bg-red-950/40 px-6 py-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] font-mono" data-testid="readiness-banner">
      <span className="inline-flex items-center gap-1.5 text-red-200 uppercase tracking-[0.15em]" data-testid="readiness-title">
        <Siren className="w-3.5 h-3.5" /> RH not trading · {r.mode}
      </span>
      {r.reasons.map((x, i) => (
        <span key={i} className="text-red-100/90" data-testid={`readiness-reason-${i}`}>· {x}</span>
      ))}
      {r.auto_disabled_on_restart_at && stopped && (
        <span className="text-red-300/70" data-testid="readiness-restart-ts">(restart at {new Date(r.auto_disabled_on_restart_at).toLocaleTimeString()})</span>
      )}
      {stopped && (
        <button type="button" onClick={start} disabled={starting} data-testid="readiness-start-btn"
          className="ml-auto inline-flex items-center gap-1 px-2 py-0.5 border border-emerald-700 text-emerald-300 hover:bg-emerald-950 uppercase tracking-wider text-[10px] transition-colors duration-100">
          <Play className="w-3 h-3" /> {starting ? "starting…" : "start bot"}
        </button>
      )}
    </div>
  );
}
