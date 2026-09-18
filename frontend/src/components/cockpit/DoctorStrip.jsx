import { memo, useState } from "react";
import { ArrowRight } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

const fmtLeft = (until) => { const s = Math.max(0, until - Date.now() / 1000); return s >= 3600 ? `${(s / 3600).toFixed(1)}h` : `${Math.ceil(s / 60)}m`; };

function ChainSeg({ chain, books, status, live, canary, onRevert, busy }) {
  const paused = status?.books_paused || {};
  const hit = books.filter((b) => paused[b] != null);
  const canaryHere = canary?.state === "running" && books.includes(canary?.proposal?.book);
  return (
    <span className="flex items-center gap-2 whitespace-nowrap" data-testid={`doctor-strip-${chain}`}>
      <span className={chain === "rh" ? "text-lime-300" : "text-sky-300"}>{chain.toUpperCase()}</span>
      {hit.length ? hit.map((b) => (
        <span key={b} className="flex items-center gap-1.5" title={`breaker armed on ${b} until ${new Date(paused[b] * 1000).toLocaleTimeString()} — ${live ? "LIVE entries benched" : "paper keeps trading at ×0.5 size, gates ×1.25"}`}>
          <span className="text-neutral-200">{b.replace("_", " ")}</span><span className="text-neutral-600">·</span>
          <span className={live ? "text-red-300" : "text-amber-300"}>{live ? "BENCHED" : "ADJUSTING ×0.5"}</span>
          <span className="text-neutral-600">{fmtLeft(paused[b])}</span>
        </span>
      )) : <><span className="text-neutral-200">{books[0].replace("_", " ")}</span><span className="text-neutral-600">·</span><span className="text-emerald-300">clear</span></>}
      {canaryHere && (
        <span className="flex items-center gap-1.5" title={`${canary.proposal.reason} — runs 20 fills, promoted only if R expectancy improves`} data-testid={`doctor-canary-${chain}`}>
          <span className="text-neutral-600">·</span><span className="text-neutral-400">canary</span>
          <span className="text-emerald-200">{canary.proposal.key}={canary.proposal.value}</span>
          <button type="button" onClick={onRevert} disabled={busy} data-testid="doctor-canary-revert" className="text-red-300 hover:text-red-200 disabled:opacity-50">[revert]</button>
        </span>
      )}
    </span>
  );
}

function DoctorStrip({ status, auto, pending = 0, onOpenDoctor }) {
  const [busy, setBusy] = useState(false);
  const canary = auto?.canary;
  const revert = async () => {
    setBusy(true);
    try { await api.doctorLearningRevert(); toast.success("Canary reverted — baseline restored"); } catch (e) { toast.error(e?.response?.data?.detail || "revert failed"); } finally { setBusy(false); }
  };
  return (
    <div className="control-card !p-0" data-testid="doctor-strip">
      <div className="px-3 py-2 border-b border-neutral-800 flex items-center justify-between text-[11px] font-mono tracking-[0.2em] text-neutral-200">
        <span>DOCTOR STRIP</span>
        <button type="button" onClick={onOpenDoctor} data-testid="doctor-strip-open" className="flex items-center gap-1 text-neutral-400 hover:text-emerald-300 tracking-normal text-[11px] underline underline-offset-2">open doctor <ArrowRight className="w-3 h-3" /></button>
      </div>
      <div className="px-3 py-2 flex items-center gap-4 font-mono text-[11px] overflow-x-auto">
        <ChainSeg chain="sol" books={["hunt", "scalp", "runner"]} status={status} live={!!status?.live_trading} canary={canary} onRevert={revert} busy={busy} />
        <span className="w-px h-4 bg-neutral-800 flex-shrink-0" />
        <ChainSeg chain="rh" books={["rh_pons"]} status={status} live={!!status?.rh_live_trading} canary={canary} onRevert={revert} busy={busy} />
        <span className="ml-auto flex items-center gap-3 whitespace-nowrap">
          <span className={pending ? "text-amber-300" : "text-neutral-500"} data-testid="doctor-strip-pending" title="Doctor suggestions waiting for your Apply / Dismiss (Doctor view)">{pending} pending</span>
          {auto?.autopilot_enabled && <span className="px-1.5 py-0.5 border border-lime-800 text-lime-300" data-testid="doctor-strip-driving">DOC driving</span>}
        </span>
      </div>
    </div>
  );
}

export default memo(DoctorStrip);
