import { memo } from "react";
import { Stethoscope, Gauge, ShieldAlert, Activity } from "lucide-react";

const fmt2 = (v) => Number(v ?? 0).toFixed(2);

function TempoPill({ chain, t }) {
  if (!t) return null;
  const hot = t.tempo >= 1.15, cold = t.tempo <= 0.85;
  const cls = hot ? "border-emerald-800 text-emerald-300" : cold ? "border-amber-800 text-amber-300" : "border-neutral-800 text-neutral-300";
  return (
    <span data-testid={`strip-tempo-${chain}`} className={`inline-flex items-center gap-1.5 px-2 py-0.5 border ${cls}`}
      title={`${chain.toUpperCase()} tempo ${t.tempo}× · gate multiplier ${t.gate_mult}× · buys/2m ${t.buys_2m ?? "—"} vs ${t.baseline_buys_2m} baseline · launches/h ${t.launch_rate_h} vs ${t.baseline_rate_h}`}>
      <span className="uppercase text-neutral-500">{chain}</span>
      <span className="font-semibold">{t.tempo}×</span>
      <span className="text-neutral-500">gates {t.gate_mult}×</span>
    </span>
  );
}

function DoctorStrip({ status, config }) {
  const tempo = status?.market_tempo || {};
  const benched = Object.keys(status?.books_paused || {});
  const loss = Number(status?.daily_loss_usd ?? 0), kill = Number(status?.daily_kill_switch_usd ?? 0);
  const lossPct = kill > 0 ? Math.min(100, (loss / kill) * 100) : 0;
  const live = !!status?.live_trading, rhLive = !!config?.rh_live_trading;
  const hour = new Date().getUTCHours();
  const peak = (tempo.peak_hours || []).includes(hour);
  return (
    <div data-testid="doctor-strip" className="border-b border-neutral-800 bg-neutral-950/80 backdrop-blur px-4 md:px-6 py-1.5 flex items-center gap-3 md:gap-4 text-[10px] font-mono overflow-x-auto whitespace-nowrap">
      <span className="flex items-center gap-1.5 uppercase tracking-[0.2em] text-neutral-500 flex-shrink-0"><Stethoscope className="w-3 h-3" /> doctor</span>
      <span className="flex items-center gap-1.5 flex-shrink-0">
        <Gauge className="w-3 h-3 text-neutral-600" />
        {["sol", "rh"].map((c) => <TempoPill key={c} chain={c} t={tempo[c]} />)}
        {peak && <span data-testid="strip-peak-hour" className="px-1.5 py-0.5 border border-emerald-900 text-emerald-400" title={`UTC ${hour}:00 is a peak hour — size ×1.25`}>peak hour</span>}
      </span>
      <span className="w-px h-3 bg-neutral-800 flex-shrink-0" />
      <span data-testid="strip-benched" className={`flex items-center gap-1.5 flex-shrink-0 ${benched.length ? "text-amber-300" : "text-neutral-500"}`}
        title={benched.length ? `Live Doctor breaker armed on: ${benched.join(", ")} (paper keeps trading at ×0.5 size)` : "no books benched"}>
        <ShieldAlert className="w-3 h-3" />
        {benched.length ? `benched: ${benched.map((b) => b.replace("_", " ")).join(" · ")}` : "no breakers armed"}
      </span>
      <span className="w-px h-3 bg-neutral-800 flex-shrink-0" />
      <span data-testid="strip-kill" className="flex items-center gap-2 flex-shrink-0" title="live loss today vs the daily kill switch">
        <span className="text-neutral-500">kill</span>
        <span className="relative w-20 h-1.5 bg-neutral-900 border border-neutral-800 overflow-hidden">
          <span className={`absolute inset-y-0 left-0 ${lossPct > 75 ? "bg-red-500" : lossPct > 40 ? "bg-amber-500" : "bg-emerald-600"}`} style={{ width: `${lossPct}%`, transition: "width 400ms ease" }} />
        </span>
        <span className={status?.kill_switch_tripped ? "text-red-400 font-semibold" : "text-neutral-300"}>
          {status?.kill_switch_tripped ? "TRIPPED" : `$${fmt2(loss)} / $${kill.toFixed(0)}`}
        </span>
      </span>
      <span className="w-px h-3 bg-neutral-800 flex-shrink-0" />
      <span data-testid="strip-active" className="flex items-center gap-1.5 flex-shrink-0 text-neutral-300">
        <Activity className="w-3 h-3 text-blue-500" /> {status?.active_trade_count ?? 0} open · {status?.total_trades_today ?? 0} today
      </span>
      <span className="ml-auto flex items-center gap-1.5 flex-shrink-0" data-testid="strip-modes">
        <span className={`px-1.5 py-0.5 border ${live ? "border-red-800 text-red-300" : "border-neutral-800 text-neutral-500"}`}>sol {live ? "LIVE" : "paper"}</span>
        <span className={`px-1.5 py-0.5 border ${rhLive ? "border-red-800 text-red-300" : "border-neutral-800 text-neutral-500"}`}>rh {rhLive ? "LIVE" : config?.rh_paper_enabled ? "paper" : "off"}</span>
      </span>
    </div>
  );
}

export default memo(DoctorStrip);
