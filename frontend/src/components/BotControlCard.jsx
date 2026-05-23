import { useState, useEffect } from "react";
import { Power, Zap, Settings2, ChevronDown, ChevronRight } from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { toast } from "sonner";
import SpeedModeSlider from "./SpeedModeSlider";

export default function BotControlCard({ status, config, onUpdate, onStart, onStop }) {
  const [local, setLocal] = useState(null);
  const [showAdvancedFees, setShowAdvancedFees] = useState(false);

  // Seed `local` from `config` only when:
  //   (a) we haven't initialized yet (first load), OR
  //   (b) the form is clean (no unsaved edits) — so background config refreshes
  //       (Dashboard's 20s poll, WS-driven refreshAll on every trade event)
  //       can't wipe in-progress edits. Once the user starts typing, this
  //       effect becomes a no-op until they Save (which round-trips and re-cleans
  //       the form) or hit Reset.
  // Without this dirty-guard, every trade triggered a refetch which called
  // setLocal(config) and erased anything the user had typed but not saved —
  // making it look like "saved values revert after trades" when in fact the
  // backend was fine and the user's in-flight edits were being clobbered.
  useEffect(() => {
    if (!config) return;
    setLocal((prev) => {
      if (prev === null) return config;
      const isDirty = JSON.stringify(prev) !== JSON.stringify(config);
      return isDirty ? prev : config;
    });
  }, [config]);

  if (!local) return <div className="control-card text-neutral-500 text-sm">Loading...</div>;

  const dirty = JSON.stringify(local) !== JSON.stringify(config);
  const running = status?.enabled;

  const save = async () => {
    try {
      await onUpdate(local);
      toast.success("Config saved");
    } catch (e) {
      toast.error("Save failed");
    }
  };

  return (
    <div className="control-card flex flex-col gap-3" data-testid="bot-control-card">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
          <Settings2 className="w-3 h-3" />
          Bot Control
        </div>
        <div className="flex items-center gap-2 text-[10px] font-mono uppercase">
          <span className={local.live_trading ? "text-red-400" : "text-neutral-500"}>LIVE</span>
          <Switch
            data-testid="live-trading-switch"
            checked={local.live_trading}
            onCheckedChange={(v) => setLocal({ ...local, live_trading: v })}
          />
        </div>
      </div>

      {(() => {
        const stopping = status?.stopping_gracefully;
        const activeN = status?.active_trade_count || 0;
        if (stopping) {
          return (
            <div className="space-y-1">
              <button
                disabled
                data-testid="stopping-bot-indicator"
                className="w-full flex items-center justify-center gap-2 px-3 py-2.5 border border-amber-700 text-amber-300 bg-amber-950/50 font-mono text-xs uppercase tracking-[0.2em] cursor-not-allowed"
              >
                <Power className="w-3 h-3 animate-pulse" />
                Stopping · waiting on {activeN} position{activeN === 1 ? "" : "s"}
              </button>
              <div className="flex items-center justify-between text-[10px] font-mono uppercase tracking-[0.15em] px-1">
                <button
                  onClick={onStart}
                  data-testid="resume-bot-btn"
                  className="text-emerald-400 hover:text-emerald-300 transition-colors"
                >
                  ▸ resume trading
                </button>
                <button
                  onClick={async () => {
                    if (!window.confirm(`Force-close ${activeN} active position${activeN === 1 ? "" : "s"} right now? This skips TP/SL triggers.`)) return;
                    try {
                      const m = await import("@/lib/api");
                      await m.api.abortBot();
                      toast.success("Hard stop — all positions force-closed");
                    } catch {
                      toast.error("Abort failed");
                    }
                  }}
                  data-testid="abort-bot-btn"
                  className="text-red-400 hover:text-red-300 transition-colors"
                >
                  ✕ abort all
                </button>
              </div>
            </div>
          );
        }
        return (
          <button
            onClick={running ? onStop : onStart}
            disabled={status?.kill_switch_tripped}
            data-testid={running ? "stop-bot-btn" : "start-bot-btn"}
            className={`w-full flex items-center justify-center gap-2 px-3 py-2.5 border font-mono text-xs uppercase tracking-[0.2em] transition-colors duration-100 ${
              running
                ? "border-red-700 text-red-300 bg-red-950 hover:bg-red-900"
                : "border-emerald-700 text-emerald-300 bg-emerald-950 hover:bg-emerald-900"
            } disabled:opacity-40 disabled:cursor-not-allowed`}
          >
            <Power className="w-3 h-3" />
            {running ? "Stop Bot" : "Start Bot"}
          </button>
        );
      })()}

      {/* Speed Mode slider — controls priority fee + slippage as a bundle */}
      <SpeedModeSlider
        value={local.speed_mode || "manual"}
        onChange={(mode) => setLocal({ ...local, speed_mode: mode })}
      />
      <button
        onClick={() => {
          setShowAdvancedFees((v) => !v);
          // If they want manual control, opening Advanced switches mode to manual
          if (!showAdvancedFees && local.speed_mode !== "manual") {
            setLocal({ ...local, speed_mode: "manual" });
          }
        }}
        data-testid="toggle-advanced-fees-btn"
        className="flex items-center gap-1 text-[10px] uppercase tracking-[0.15em] text-neutral-500 hover:text-neutral-300 transition-colors -mt-1"
      >
        {showAdvancedFees ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        Manual fee override
      </button>

      <div className="grid grid-cols-2 gap-2 text-xs">
        <Field label="Min Trade ($)" testid="min-trade-input"
               value={local.min_trade_usd}
               onChange={(v) => setLocal({ ...local, min_trade_usd: parseFloat(v) || 0 })} step="0.1" />
        <Field label="Max Trade ($)" testid="max-trade-input"
               value={local.max_trade_usd}
               onChange={(v) => setLocal({ ...local, max_trade_usd: parseFloat(v) || 0 })} step="0.1" />
        {showAdvancedFees && (
          <Field label="Slippage (bps)" testid="slippage-input"
                 value={local.slippage_bps}
                 onChange={(v) => setLocal({ ...local, slippage_bps: parseInt(v, 10) || 0 })} step="50" />
        )}
        <Field label="Kill Switch ($)" testid="killswitch-input"
               value={local.daily_kill_switch_usd}
               onChange={(v) => setLocal({ ...local, daily_kill_switch_usd: parseFloat(v) || 0 })} step="1" />
        <Field label="TP (%)" testid="tp-input"
               value={local.take_profit_pct}
               onChange={(v) => setLocal({ ...local, take_profit_pct: parseFloat(v) || 0 })} step="1" />
        <Field label="SL (%)" testid="sl-input"
               value={local.stop_loss_pct}
               onChange={(v) => setLocal({ ...local, stop_loss_pct: parseFloat(v) || 0 })} step="1" />
        <Field label="Max Hold (s)" testid="hold-input"
               value={local.hold_max_seconds}
               onChange={(v) => setLocal({ ...local, hold_max_seconds: parseInt(v, 10) || 0 })} step="1" />
        {showAdvancedFees && (
          <Field label="Priority µLamp" testid="prio-input"
                 value={local.priority_fee_microlamports}
                 onChange={(v) => setLocal({ ...local, priority_fee_microlamports: parseInt(v, 10) || 0 })} step="100000" />
        )}
        <Field label="Trailing Stop (%)" testid="trailing-input"
               value={local.trailing_stop_pct}
               onChange={(v) => setLocal({ ...local, trailing_stop_pct: parseFloat(v) || 0 })} step="1" />
        {showAdvancedFees && (
          <Field label="Exit Slip (bps)" testid="exit-slip-input"
                 value={local.exit_slippage_bps}
                 onChange={(v) => setLocal({ ...local, exit_slippage_bps: parseInt(v, 10) || 0 })} step="50" />
        )}
        <Field label="Partial TP (%)" testid="partial-tp-input"
               value={local.partial_tp_pct}
               onChange={(v) => setLocal({ ...local, partial_tp_pct: parseFloat(v) || 0 })} step="5" />
        <Field label="Runner Trail (%)" testid="partial-trail-input"
               value={local.partial_tp_trail_tighten_pct}
               onChange={(v) => setLocal({ ...local, partial_tp_trail_tighten_pct: parseFloat(v) || 0 })} step="1" />
      </div>

      {/* Portfolio / global entry settings (per-band liquidity & buyer thresholds live in the gates table below) */}
      <div className="border-t border-neutral-800 pt-3 mt-1">
        <div className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 mb-2">Portfolio</div>
        <div className="grid grid-cols-2 gap-2 text-xs">
          <Field label="Max Positions" testid="max-positions-input"
                 value={local.max_concurrent_positions}
                 onChange={(v) => setLocal({ ...local, max_concurrent_positions: parseInt(v, 10) || 0 })} step="1" />
        </div>
      </div>

      {/* Momentum scanner config */}
      <div className="border-t border-neutral-800 pt-3 mt-1">
        <div className="flex items-center justify-between mb-2">
          <span className="text-[10px] uppercase tracking-[0.15em] text-neutral-500">Momentum Scanner</span>
          <label className="flex items-center gap-1.5 text-[10px] font-mono uppercase text-neutral-400">
            <input
              type="checkbox"
              data-testid="scanner-enabled-checkbox"
              checked={local.scanner_enabled}
              onChange={(e) => setLocal({ ...local, scanner_enabled: e.target.checked })}
            />
            enabled
          </label>
        </div>
        <div className="grid grid-cols-2 gap-2 text-xs">
          <Field label="Window (h)" testid="scanner-window-input"
                 value={local.scanner_window_hours}
                 onChange={(v) => setLocal({ ...local, scanner_window_hours: parseInt(v, 10) || 0 })} step="1" />
          <Field label="Min Age (min)" testid="scanner-min-age-input"
                 value={local.scanner_min_age_minutes}
                 onChange={(v) => setLocal({ ...local, scanner_min_age_minutes: parseInt(v, 10) || 0 })} step="15" />
          <Field label="Scan every (s)" testid="scanner-interval-input"
                 value={local.scanner_interval_s}
                 onChange={(v) => setLocal({ ...local, scanner_interval_s: parseInt(v, 10) || 0 })} step="5" />
          <Field label="Inflow Win (s)" testid="scanner-inflow-window-input"
                 value={local.scanner_recent_inflow_window_s}
                 onChange={(v) => setLocal({ ...local, scanner_recent_inflow_window_s: parseInt(v, 10) || 0 })} step="30" />
          <Field label="Max Idle (min)" testid="scanner-max-idle-input"
                 value={local.scanner_discovery_max_idle_minutes}
                 onChange={(v) => setLocal({ ...local, scanner_discovery_max_idle_minutes: parseInt(v, 10) || 0 })} step="1" />
          <Field label="Entry Vel Win (s)" testid="scanner-entry-vel-window-input"
                 value={local.scanner_entry_velocity_window_s}
                 onChange={(v) => setLocal({ ...local, scanner_entry_velocity_window_s: parseInt(v, 10) || 0 })} step="5" />
          <Field label="Min Entry Vel (%)" testid="scanner-entry-vel-min-input"
                 value={local.scanner_entry_velocity_min_pct}
                 onChange={(v) => setLocal({ ...local, scanner_entry_velocity_min_pct: parseFloat(v) || 0 })} step="0.5" />
          <Field label="SL Cooldown (min)" testid="sl-cooldown-input"
                 value={local.sl_cooldown_minutes}
                 onChange={(v) => setLocal({ ...local, sl_cooldown_minutes: parseFloat(v) || 0 })} step="0.5" />
        </div>

        {/* Distribution-vacuum gate (insider pre-distribution filter) */}
        <div className="mt-3 border-t border-neutral-800 pt-2.5">
          <label className="flex items-center justify-between text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-400 cursor-pointer">
            <span className="flex items-center gap-1.5">
              <input
                type="checkbox"
                data-testid="gate-distribution-vacuum-checkbox"
                checked={local.gate_distribution_vacuum}
                onChange={(e) => setLocal({ ...local, gate_distribution_vacuum: e.target.checked })}
              />
              Distribution vacuum filter
            </span>
            <span className="text-neutral-600 normal-case tracking-normal">
              reject if all holders appeared in last window
            </span>
          </label>
          <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
            <Field label="Min Holders" testid="gate-distribution-min-input"
                   value={local.gate_distribution_min_holders}
                   onChange={(v) => setLocal({ ...local, gate_distribution_min_holders: parseInt(v, 10) || 0 })} step="1" />
          </div>
        </div>

        {/* Socials gate */}
        <div className="mt-3 border-t border-neutral-800 pt-2.5">
          <label className="flex items-center justify-between text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-400 cursor-pointer">
            <span className="flex items-center gap-1.5">
              <input
                type="checkbox"
                data-testid="gate-socials-required-checkbox"
                checked={local.gate_socials_required}
                onChange={(e) => setLocal({ ...local, gate_socials_required: e.target.checked })}
              />
              Socials required for entry
            </span>
            <span className="text-neutral-600 normal-case tracking-normal">
              twitter / telegram / website + reply_count
            </span>
          </label>
          <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
            <Field label="Min Reply Count" testid="gate-min-replies-input"
                   value={local.gate_min_reply_count}
                   onChange={(v) => setLocal({ ...local, gate_min_reply_count: parseInt(v, 10) || 0 })} step="5" />
          </div>
        </div>

        {/* Per-band gates table */}
        <div className="mt-3">
          <div className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 mb-1">Per-band gates</div>
          <div className="border border-neutral-800 text-xs">
            <div className="grid grid-cols-[1.4fr_1fr_1fr] bg-neutral-950 text-[10px] uppercase tracking-[0.15em] text-neutral-500 border-b border-neutral-800">
              <div className="px-2 py-1.5">Gate</div>
              <div className="px-2 py-1.5 text-amber-400">New (&lt; seasoning)</div>
              <div className="px-2 py-1.5 text-cyan-300">Seasoned (≥ seasoning)</div>
            </div>
            <GateRow label="Min Growth (%)"
                     newTestid="scanner-growth-new-input"
                     newValue={local.scanner_min_growth_pct_new}
                     onNewChange={(v) => setLocal({ ...local, scanner_min_growth_pct_new: parseFloat(v) || 0 })}
                     seasonedTestid="scanner-growth-input"
                     seasonedValue={local.scanner_min_growth_pct}
                     onSeasonedChange={(v) => setLocal({ ...local, scanner_min_growth_pct: parseFloat(v) || 0 })}
                     step="5" />
            <GateRow label="Min Liquidity (SOL)"
                     newTestid="min-liq-new-input"
                     newValue={local.min_curve_liquidity_sol_new}
                     onNewChange={(v) => setLocal({ ...local, min_curve_liquidity_sol_new: parseFloat(v) || 0 })}
                     seasonedTestid="min-liq-seasoned-input"
                     seasonedValue={local.min_curve_liquidity_sol}
                     onSeasonedChange={(v) => setLocal({ ...local, min_curve_liquidity_sol: parseFloat(v) || 0 })}
                     step="0.5" />
            {/* New-only: live mempool signals from Helius */}
            <GateRow label="Min Inflow (SOL/win)" newOnly
                     newTestid="scanner-inflow-new-input"
                     newValue={local.scanner_min_recent_inflow_sol_new}
                     onNewChange={(v) => setLocal({ ...local, scanner_min_recent_inflow_sol_new: parseFloat(v) || 0 })}
                     step="0.5" />
            <GateRow label="Min new buyers (1m)" newOnly
                     newTestid="scanner-newbuyers-new-input"
                     newValue={local.scanner_min_new_buyers_new}
                     onNewChange={(v) => setLocal({ ...local, scanner_min_new_buyers_new: parseInt(v, 10) || 0 })}
                     step="1" />
            <GateRow label="Min Total Holders" newOnly
                     newTestid="min-buyers-new-input"
                     newValue={local.min_buyers_for_entry_new}
                     onNewChange={(v) => setLocal({ ...local, min_buyers_for_entry_new: parseInt(v, 10) || 0 })}
                     step="1" />
            {/* Seasoned-only: Pump.fun API polled signals */}
            <GateRow label="Min MC ($)" seasonedOnly
                     seasonedTestid="scanner-mc-seasoned-input"
                     seasonedValue={local.scanner_min_mc_usd_seasoned}
                     onSeasonedChange={(v) => setLocal({ ...local, scanner_min_mc_usd_seasoned: parseFloat(v) || 0 })}
                     step="1000" />
            <GateRow label="Min MC vel (5m %)" seasonedOnly last
                     seasonedTestid="scanner-mcvel-seasoned-input"
                     seasonedValue={local.scanner_min_mc_velocity_5m_pct_seasoned}
                     onSeasonedChange={(v) => setLocal({ ...local, scanner_min_mc_velocity_5m_pct_seasoned: parseFloat(v) || 0 })}
                     step="1" />
          </div>
        </div>
      </div>

      {/* Re-entry config */}
      <div className="border-t border-neutral-800 pt-3 mt-1">
        <div className="flex items-center justify-between mb-2">
          <span className="text-[10px] uppercase tracking-[0.15em] text-neutral-500">Re-entry on winners</span>
          <label className="flex items-center gap-1.5 text-[10px] font-mono uppercase text-neutral-400">
            <input
              type="checkbox"
              data-testid="reentry-enabled-checkbox"
              checked={local.reentry_enabled}
              onChange={(e) => setLocal({ ...local, reentry_enabled: e.target.checked })}
            />
            enabled
          </label>
        </div>
        <div className="grid grid-cols-2 gap-2 text-xs">
          <Field label="Max attempts" testid="reentry-max-input"
                 value={local.reentry_max_attempts}
                 onChange={(v) => setLocal({ ...local, reentry_max_attempts: parseInt(v, 10) || 0 })} step="1" />
          <Field label="Pullback (%)" testid="reentry-pullback-input"
                 value={local.reentry_pullback_pct}
                 onChange={(v) => setLocal({ ...local, reentry_pullback_pct: parseFloat(v) || 0 })} step="1" />
          <Field label="Window (s)" testid="reentry-window-input"
                 value={local.reentry_window_seconds}
                 onChange={(v) => setLocal({ ...local, reentry_window_seconds: parseInt(v, 10) || 0 })} step="30" />
          <Field label="Size ×" testid="reentry-size-input"
                 value={local.reentry_size_multiplier}
                 onChange={(v) => setLocal({ ...local, reentry_size_multiplier: parseFloat(v) || 0 })} step="0.1" />
        </div>
      </div>

      <button
        onClick={save}
        disabled={!dirty}
        data-testid="save-config-btn"
        className="w-full px-3 py-2 border border-blue-700 text-blue-300 bg-blue-950 hover:bg-blue-900 font-mono text-xs uppercase tracking-[0.2em] transition-colors duration-100 disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
      >
        <Zap className="w-3 h-3" />
        {dirty ? "Save Config" : "Saved"}
      </button>
      <button
        onClick={async () => {
          if (!window.confirm("Reset ALL settings to suggested defaults? (kill switch + live trading flag preserved)")) return;
          try {
            const fresh = await import("@/lib/api").then(m => m.api.resetConfig());
            setLocal(fresh);
            toast.success("Settings restored to defaults");
          } catch (e) {
            toast.error("Reset failed");
          }
        }}
        data-testid="reset-defaults-btn"
        className="w-full px-3 py-1.5 border border-neutral-700 text-neutral-400 hover:bg-neutral-900 hover:text-neutral-200 font-mono text-[10px] uppercase tracking-[0.2em] transition-colors duration-100"
      >
        Reset to Defaults
      </button>
    </div>
  );
}

function Field({ label, value, onChange, step, testid }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] uppercase tracking-[0.15em] text-neutral-500">{label}</span>
      <input
        data-testid={testid}
        type="number"
        step={step}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-neutral-950 border border-neutral-800 px-2 py-1 font-mono text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
      />
    </label>
  );
}

function GateRow({ label, newTestid, newValue, onNewChange, seasonedTestid, seasonedValue, onSeasonedChange, step, last, newOnly, seasonedOnly }) {
  const cell = "px-2 py-1 border-l border-neutral-800";
  const dim = "px-2 py-1 border-l border-neutral-800 text-[10px] font-mono text-neutral-700 italic text-center self-center";
  return (
    <div className={`grid grid-cols-[1.4fr_1fr_1fr] ${last ? "" : "border-b border-neutral-800"}`}>
      <div className="px-2 py-1 text-[10px] uppercase tracking-[0.1em] text-neutral-400 font-mono self-center">{label}</div>
      {seasonedOnly ? (
        <div className={dim}>n/a</div>
      ) : (
        <div className={cell}>
          <input
            data-testid={newTestid}
            type="number"
            step={step}
            value={newValue}
            onChange={(e) => onNewChange(e.target.value)}
            className="w-full bg-neutral-950 border border-amber-900/50 px-2 py-0.5 font-mono text-xs text-amber-200 focus:border-amber-500 focus:outline-none"
          />
        </div>
      )}
      {newOnly ? (
        <div className={dim}>n/a</div>
      ) : (
        <div className={cell}>
          <input
            data-testid={seasonedTestid}
            type="number"
            step={step}
            value={seasonedValue}
            onChange={(e) => onSeasonedChange(e.target.value)}
            className="w-full bg-neutral-950 border border-cyan-900/50 px-2 py-0.5 font-mono text-xs text-cyan-200 focus:border-cyan-500 focus:outline-none"
          />
        </div>
      )}
    </div>
  );
}
