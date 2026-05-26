import React, { useEffect, useState, useCallback } from "react";
import { Loader2, Sparkles, X, CheckCircle2, RefreshCw, Stethoscope } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import HelpHint from "./HelpHint";
import DoctorLivePanels from "./DoctorLivePanels";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";

const CATEGORY_LABEL = {
  sizing: "Position size — how much capital is committed per buy",
  sl: "Stop-loss tuning — exits when PnL goes against you",
  tp: "Take-profit tuning — exits when PnL hits the target",
  partial: "Partial-TP tuning — selling a fraction at TP, riding the rest",
  hold: "Max-hold tuning — time cap on a position",
  gate: "Entry gates — what tokens are allowed through to a buy",
  scanner: "Scanner timing / bands — what tokens get tracked",
  classifier: "Classifier rules — abort/exit-early on real-time signals",
  timing: "Re-entry & cooldown — handling exits and re-buys",
  greylist_sniper: "Greylist Sniper tuning — auto-adjusts min_score based on win-rate feedback",
  needs_more_data: "Doctor needs more trades before it can suggest changes",
};

const CATEGORY_TINT = {
  sizing: "border-amber-700/60 text-amber-300",
  sl: "border-rose-800/60 text-rose-300",
  tp: "border-emerald-800/60 text-emerald-300",
  partial: "border-emerald-800/60 text-emerald-300",
  hold: "border-cyan-800/60 text-cyan-300",
  gate: "border-purple-800/60 text-purple-300",
  scanner: "border-blue-800/60 text-blue-300",
  classifier: "border-fuchsia-800/60 text-fuchsia-300",
  timing: "border-violet-800/60 text-violet-300",
  greylist_sniper: "border-rose-700/60 text-rose-300",
  needs_more_data: "border-neutral-700 text-neutral-400",
};

const CONFIDENCE_DOT = { high: "bg-emerald-400", med: "bg-amber-400", low: "bg-neutral-500" };

export default function StrategyDoctorPanel({ onApplied }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [busyId, setBusyId] = useState(null);
  const [lastRun, setLastRun] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const d = await api.doctorList();
      setItems(d.items || []);
      // Track when we last saw any pending data
      setLastRun(new Date());
    } catch (e) {
      toast.error(`Doctor list failed: ${e?.response?.data?.detail || e.message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 60_000);
    return () => clearInterval(t);
  }, [refresh]);

  const runNow = async () => {
    setRunning(true);
    try {
      const d = await api.doctorRunNow();
      toast.success(`Doctor cycle complete — ${d.new_suggestions} new`);
      await refresh();
    } catch (e) {
      toast.error(`Run failed: ${e?.response?.data?.detail || e.message}`);
    } finally {
      setRunning(false);
    }
  };

  const apply = async (s) => {
    setBusyId(s.id);
    try {
      const d = await api.doctorApply(s.id);
      toast.success(`Applied: ${Object.keys(d.applied || {}).join(", ") || "no changes"}`);
      onApplied?.();
      await refresh();
    } catch (e) {
      toast.error(`Apply failed: ${e?.response?.data?.detail || e.message}`);
    } finally {
      setBusyId(null);
    }
  };

  const dismiss = async (s) => {
    setBusyId(s.id);
    try {
      await api.doctorDismiss(s.id);
      toast.info(`Dismissed (won't reappear for 24h)`);
      await refresh();
    } catch (e) {
      toast.error(`Dismiss failed: ${e?.response?.data?.detail || e.message}`);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="border border-neutral-800 bg-neutral-950 rounded-sm p-3 md:p-4" data-testid="strategy-doctor">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Stethoscope className="w-4 h-4 text-emerald-300" />
          <h2 className="text-sm uppercase tracking-widest font-mono text-emerald-300">Strategy Doctor</h2>
          <HelpHint label="Strategy Doctor">
            A background analyzer that reviews your last 24–72h of trades every 30 min and proposes tuning changes (TP, SL, slippage, gates, max hold, etc.). Each suggestion is one click to apply. Runs even when you're logged out.
          </HelpHint>
          {items.length > 0 && (
            <span className="text-[10px] font-mono px-1.5 py-0.5 border border-neutral-800 bg-neutral-900 text-neutral-400">
              {items.length} pending
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={runNow}
          disabled={running}
          data-testid="doctor-run-now"
          className="px-2 py-1 text-[10px] uppercase tracking-wider font-mono border border-neutral-700 text-neutral-300 hover:bg-neutral-900 disabled:opacity-40 inline-flex items-center gap-1"
        >
          {running ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
          {running ? "Analyzing…" : "Re-analyze"}
        </button>
      </div>

      {loading ? (
        <div className="text-[11px] font-mono text-neutral-600 py-6 text-center">
          <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
          loading…
        </div>
      ) : items.length === 0 ? (
        <div className="text-[11px] font-mono text-neutral-500 py-6 text-center border border-dashed border-neutral-800 rounded-sm">
          <Sparkles className="w-4 h-4 inline mr-1 text-emerald-700" />
          No suggestions. Bot's running healthy by my measure.
          <div className="text-[10px] text-neutral-700 mt-1">
            Doctor re-checks every 30 min. Tap Re-analyze to force a cycle.
          </div>
        </div>
      ) : (
        <div className="space-y-2">
          {items.map((s) => (
            <SuggestionCard
              key={s.id}
              s={s}
              busy={busyId === s.id}
              onApply={() => apply(s)}
              onDismiss={() => dismiss(s)}
            />
          ))}
        </div>
      )}

      <div className="mt-3 text-[9px] font-mono text-neutral-700 flex justify-between">
        <span>Doctor runs autonomously — keeps working when you're logged out.</span>
        {lastRun && <span>last poll: {lastRun.toLocaleTimeString()}</span>}
      </div>

      {/* Doctor Live: trailing-stop circuit breaker, helius budget, applied history */}
      <DoctorLivePanels />
    </div>
  );
}

function SuggestionCard({ s, busy, onApply, onDismiss }) {
  const tint = CATEGORY_TINT[s.category] || "border-neutral-700 text-neutral-300";
  const cdot = CONFIDENCE_DOT[s.confidence] || "bg-neutral-500";
  const hasActions = s.actions && Object.keys(s.actions).length > 0;
  const isInfo = !hasActions;

  return (
    <div
      data-testid={`doctor-suggestion-${s.id}`}
      className={`border ${tint} bg-neutral-900/40 rounded-sm p-2.5`}
    >
      <div className="flex items-start gap-2">
        <Tooltip delayDuration={120}>
          <TooltipTrigger asChild>
            <button
              type="button"
              aria-label={`${s.confidence} confidence`}
              className={`w-1.5 h-1.5 rounded-full mt-1.5 ${cdot} cursor-help`}
            />
          </TooltipTrigger>
          <TooltipContent
            side="right"
            className="max-w-[260px] bg-neutral-900 border border-neutral-700 text-neutral-200 font-mono text-[11px] leading-relaxed px-2.5 py-1.5"
          >
            <span className="font-semibold uppercase">{s.confidence}</span> confidence — derived from sample size, effect strength, and recency of the underlying trades.
          </TooltipContent>
        </Tooltip>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-[9px] uppercase tracking-wider font-mono opacity-80 inline-flex items-center gap-1">
              {s.category}
              <HelpHint label={`category: ${s.category}`}>
                {CATEGORY_LABEL[s.category] || "Doctor suggestion category."}
              </HelpHint>
            </span>
            {isInfo && (
              <span className="text-[9px] uppercase tracking-wider font-mono text-neutral-600 border border-neutral-800 px-1">
                info
              </span>
            )}
          </div>
          <div className="text-[12px] font-medium leading-snug mb-1">{s.title}</div>
          <div className="text-[10px] font-mono text-neutral-400 whitespace-pre-wrap leading-relaxed">
            {s.rationale}
          </div>
          {hasActions && (
            <div className="mt-2 text-[10px] font-mono text-neutral-500 inline-flex items-center gap-1 flex-wrap">
              <span className="text-neutral-400 inline-flex items-center gap-1">
                applies:
                <HelpHint label="applies">
                  Exact config fields that "Apply" will overwrite. You can always revert by editing the value in Bot Control and saving.
                </HelpHint>
              </span>{" "}
              {Object.entries(s.actions).map(([k, v]) => (
                <span key={k} className="inline-block mr-2 text-neutral-300">
                  {k}={JSON.stringify(v)}
                </span>
              ))}
            </div>
          )}
        </div>
        <div className="flex flex-col gap-1 items-end shrink-0">
          {hasActions && (
            <button
              type="button"
              onClick={onApply}
              disabled={busy}
              data-testid={`doctor-apply-${s.id}`}
              className="px-2 py-0.5 text-[9px] uppercase tracking-wider font-mono border border-emerald-700 text-emerald-300 hover:bg-emerald-950/40 disabled:opacity-40 inline-flex items-center gap-1"
            >
              {busy ? <Loader2 className="w-2.5 h-2.5 animate-spin" /> : <CheckCircle2 className="w-2.5 h-2.5" />}
              Apply
            </button>
          )}
          <button
            type="button"
            onClick={onDismiss}
            disabled={busy}
            data-testid={`doctor-dismiss-${s.id}`}
            className="px-2 py-0.5 text-[9px] uppercase tracking-wider font-mono border border-neutral-700 text-neutral-400 hover:bg-neutral-900 disabled:opacity-40 inline-flex items-center gap-1"
            title="Hide for 24 hours"
          >
            <X className="w-2.5 h-2.5" />
            Dismiss
          </button>
        </div>
      </div>
    </div>
  );
}
