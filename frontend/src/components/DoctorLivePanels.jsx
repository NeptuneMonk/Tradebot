import React, { useEffect, useState, useCallback } from "react";
import { Activity, RefreshCw, Pause, Play, Server, AlertTriangle } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import HelpHint from "./HelpHint";

/**
 * Three compact panels stacked below the StrategyDoctorPanel:
 *  1. Trail-stop status     — regime score, peak, drawdown trail, paused?
 *  2. Helius credit budget  — burn rate, severity, reset button
 *  3. Applied history       — last 5 applied suggestions w/ revert button
 *
 * Designed to nest INSIDE the existing Doctor card so it's all one panel
 * the user can collapse. Avoids polluting Dashboard with new sections.
 */
export default function DoctorLivePanels() {
  const [live, setLive] = useState(null);
  const [budget, setBudget] = useState(null);
  const [history, setHistory] = useState([]);
  const [running, setRunning] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [l, b, h] = await Promise.all([
        api.doctorLive().catch(() => null),
        api.heliusBudget().catch(() => null),
        api.doctorAppliedHistory().catch(() => ({ items: [] })),
      ]);
      setLive(l);
      setBudget(b);
      setHistory(h?.items || []);
    } catch (e) { /* swallow */ }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 20000);  // 20s poll — cheap reads
    return () => clearInterval(id);
  }, [refresh]);

  const runLive = async () => {
    setRunning(true);
    try {
      await api.doctorLiveRunNow();
      toast.success("Doctor Live cycle complete");
      await refresh();
    } catch (e) {
      toast.error(`Run failed: ${e?.response?.data?.detail || e.message}`);
    } finally {
      setRunning(false);
    }
  };

  const resume = async () => {
    try {
      await api.doctorTrailResume();
      toast.success("Trail stop cleared — trading resumed");
      await refresh();
    } catch (e) {
      toast.error("Resume failed");
    }
  };

  const revert = async (id) => {
    if (!window.confirm("Revert this applied change?")) return;
    try {
      await api.doctorRevertApplied(id);
      toast.success("Reverted");
      await refresh();
    } catch (e) {
      toast.error("Revert failed");
    }
  };

  return (
    <div className="space-y-3 mt-4">
      <TrailStop live={live} onRun={runLive} running={running} onResume={resume} />
      <HeliusBudgetCard budget={budget} onRefresh={refresh} />
      <AppliedHistory history={history} onRevert={revert} />
    </div>
  );
}

function TrailStop({ live, onRun, running, onResume }) {
  const trail = live?.trail || {};
  const cfg = live?.trail_config || {};
  const paused = live?.pause_state?.paused;
  const score = trail.score;
  const peak = trail.peak;
  const trip = trail.trip_threshold;
  return (
    <div className="border border-neutral-800 p-3 bg-neutral-950/60" data-testid="trail-stop-card">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.15em]" >
          <Activity className={`w-3 h-3 ${paused ? "text-red-400" : "text-emerald-400"}`} />
          <span className={paused ? "text-red-400" : "text-emerald-300"}>
            Trail stop {paused ? "PAUSED" : "ACTIVE"}
          </span>
          <HelpHint label="Trail stop">
            Doctor tracks a regime score (60% rolling 4h win-rate + 40% avg winner-likeness of passing field) and maintains a rolling peak. Trips when score falls drawdown_pct from peak AND is below the min-score floor. Resumes automatically when score recovers to recovery_pct of the pre-pause peak.
          </HelpHint>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={onRun} disabled={running} data-testid="doctor-live-run"
                  className="text-[9px] font-mono uppercase tracking-wider px-2 py-0.5 border border-neutral-700 hover:bg-neutral-900 disabled:opacity-40">
            {running ? "running…" : "Run now"}
          </button>
          {paused && (
            <button onClick={onResume} data-testid="doctor-trail-resume"
                    className="text-[9px] font-mono uppercase tracking-wider px-2 py-0.5 border border-red-700 text-red-300 hover:bg-red-950/40">
              Force resume
            </button>
          )}
        </div>
      </div>
      <div className="grid grid-cols-3 gap-2 text-[11px] font-mono">
        <Stat label="Score" value={score != null ? score.toFixed(0) : "—"} />
        <Stat label="Peak" value={peak != null ? peak.toFixed(0) : "—"} />
        <Stat label="Trip at" value={trip != null ? trip.toFixed(0) : "—"} />
      </div>
      <div className="text-[9px] font-mono text-neutral-500 mt-2">
        drawdown {cfg.drawdown_pct}% · recovery {cfg.recovery_pct}% · floor {cfg.min_score_floor} · lookback {cfg.lookback_minutes}m
      </div>
      {paused && live?.pause_state?.reason && (
        <div className="mt-2 text-[10px] font-mono text-red-300 whitespace-pre-wrap border-t border-red-900/40 pt-2">
          {live.pause_state.reason}
        </div>
      )}
      {(live?.insights || []).map((ins, i) => (
        <div key={i} className="mt-2 text-[10px] font-mono text-neutral-400 whitespace-pre-wrap border-t border-neutral-800 pt-2">
          <div className="text-neutral-200">{ins.title}</div>
          {ins.body}
        </div>
      ))}
    </div>
  );
}

function HeliusBudgetCard({ budget, onRefresh }) {
  if (!budget) return null;
  const colorMap = { green: "text-emerald-400", yellow: "text-amber-400", red: "text-red-400" };
  const color = colorMap[budget.severity] || "text-neutral-400";
  const reset = async () => {
    if (!window.confirm("Reset Helius credit counter? Use when your billing cycle resets.")) return;
    await api.heliusBudgetReset();
    toast.success("Budget counter reset");
    onRefresh();
  };
  return (
    <div className="border border-neutral-800 p-3 bg-neutral-950/60" data-testid="helius-budget-card">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.15em] text-neutral-400">
          <Server className={`w-3 h-3 ${color}`} />
          Helius credits
          <HelpHint label="Helius budget">
            Counts RPC calls + WebSocket bytes against your monthly cap (10M credits on Developer plan). Severity: green &lt;60% projected, yellow &lt;100%, red ≥100%. Doctor will warn + can throttle scanner interval if you trend over budget.
          </HelpHint>
        </div>
        <div className="flex items-center gap-2">
          <span className={`text-[10px] font-mono ${color} uppercase`}>{budget.severity}</span>
          <button onClick={reset} className="text-[9px] font-mono uppercase tracking-wider px-2 py-0.5 border border-neutral-700 hover:bg-neutral-900"
                  data-testid="helius-budget-reset">Reset</button>
        </div>
      </div>
      <div className="grid grid-cols-3 gap-2 text-[11px] font-mono">
        <Stat label="Used" value={`${budget.estimated_credits_used?.toLocaleString?.()} cr`} />
        <Stat label="Daily" value={`${budget.estimated_daily_burn?.toLocaleString?.()} cr`} />
        <Stat
          label="30d proj"
          value={budget.warmup ? "warming up…" : `${budget.projected_30d_burn?.toLocaleString?.()} cr`}
          accent={budget.warmup ? "text-neutral-500" : color}
        />
      </div>
      <div className="text-[9px] font-mono text-neutral-500 mt-2">
        {budget.rpc_calls?.toLocaleString?.()} rpc · {budget.ws_messages?.toLocaleString?.()} ws msgs ·
        {" "}{(budget.ws_bytes / 1e6).toFixed(2)}MB streamed
        {budget.warmup ? (
          <> · need ≥30 min of data for projection (elapsed {budget.elapsed_hours}h)</>
        ) : (
          <> · {budget.pct_of_monthly_projected?.toFixed?.(1)}% of {budget.monthly_limit?.toLocaleString?.()} cap projected</>
        )}
      </div>
    </div>
  );
}

function AppliedHistory({ history, onRevert }) {
  if (!history?.length) return null;
  return (
    <div className="border border-neutral-800 p-3 bg-neutral-950/60" data-testid="applied-history-card">
      <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.15em] text-neutral-400 mb-2">
        <RefreshCw className="w-3 h-3 text-cyan-400" />
        Applied history
        <HelpHint label="Applied history">
          Last 5 Doctor suggestions you applied. Each shows the actual before→after values and whether the change is still in force in current config. Click Revert to restore the previous values.
        </HelpHint>
      </div>
      <div className="space-y-2 max-h-48 overflow-y-auto">
        {history.slice(0, 5).map((r) => (
          <div key={r.id} className="text-[10px] font-mono border-l-2 border-cyan-700 pl-2"
               data-testid={`applied-${r.id}`}>
            <div className="flex items-center justify-between">
              <span className="text-neutral-200 truncate">{r.title}</span>
              <button onClick={() => onRevert(r.id)} className="text-[9px] uppercase tracking-wider text-neutral-500 hover:text-red-400"
                      data-testid={`revert-${r.id}`}>Revert</button>
            </div>
            <div className="text-[9px] text-neutral-500 mt-0.5">
              {new Date(r.applied_at).toLocaleString()} ·
              {r.still_active_keys?.length ? (
                <span className="text-emerald-400"> {r.still_active_keys.length}/{Object.keys(r.actions || {}).length} active</span>
              ) : (
                <span className="text-amber-400"> overwritten</span>
              )}
            </div>
            {Object.entries(r.actions || {}).map(([k, v]) => (
              <div key={k} className="text-[9px] text-neutral-400">
                {k}: <span className="text-neutral-600">{JSON.stringify(r.before?.[k])}</span> → <span className="text-neutral-200">{JSON.stringify(v)}</span>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

function Stat({ label, value, accent = "text-neutral-200" }) {
  return (
    <div className="border border-neutral-900 px-2 py-1">
      <div className="text-[9px] uppercase tracking-[0.15em] text-neutral-500">{label}</div>
      <div className={accent}>{value}</div>
    </div>
  );
}
