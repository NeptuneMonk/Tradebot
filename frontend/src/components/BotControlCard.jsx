import { useState, useEffect } from "react";
import { Power, Zap, Settings2, ChevronDown, ChevronRight } from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { toast } from "sonner";
import SpeedModeSlider from "./SpeedModeSlider";
import ConfigSyncPanel from "./ConfigSyncPanel";
import HelpHint from "./HelpHint";

export default function BotControlCard({ status, config, onUpdate, onStart, onStop }) {
  const [local, setLocal] = useState(null);
  // Baseline = last clean snapshot of config we've seen. The form is "dirty"
  // ONLY when `local !== baseline`. This lets backend-side changes (Doctor
  // Apply, ConfigSync, /api/config/apply-recommended) overwrite the form
  // when the user has no pending edits, instead of being silently rejected
  // because `config !== local`.
  const [baseline, setBaseline] = useState(null);
  const [showAdvancedFees, setShowAdvancedFees] = useState(false);

  useEffect(() => {
    if (!config) return;
    setLocal((prev) => {
      // First load
      if (prev === null) {
        setBaseline(config);
        return config;
      }
      // User has unsaved edits — KEEP them (don't clobber via background poll)
      const userIsDirty = JSON.stringify(prev) !== JSON.stringify(baseline);
      if (userIsDirty) return prev;
      // Form is clean → accept the incoming config (Doctor Apply etc.)
      setBaseline(config);
      return config;
    });
  }, [config, baseline]);

  if (!local) return <div className="control-card text-neutral-500 text-sm">Loading...</div>;

  const dirty = JSON.stringify(local) !== JSON.stringify(baseline);
  const running = status?.enabled;

  const save = async () => {
    try {
      await onUpdate(local);
      setBaseline(local);  // promote current edit to baseline
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
          <HelpHint label="LIVE toggle" side="left">
            <span className="block"><span className="text-red-300 font-semibold">LIVE</span> = sends real on-chain transactions with the local wallet's SOL. <span className="text-emerald-300 font-semibold">OFF</span> = paper-trade mode; signals fire and PnL is tracked but no funds are touched.</span>
          </HelpHint>
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
               hint="Floor on USD size per buy. Trades smaller than this are skipped to avoid fee‑drag eating the entire EV on micro positions."
               value={local.min_trade_usd}
               onChange={(v) => setLocal({ ...local, min_trade_usd: parseFloat(v) || 0 })} step="0.1" />
        <Field label="Max Trade ($)" testid="max-trade-input"
               hint="Hard cap on USD size per buy. The bot scales position by liquidity/score but never exceeds this."
               value={local.max_trade_usd}
               onChange={(v) => setLocal({ ...local, max_trade_usd: parseFloat(v) || 0 })} step="0.1" />
        {showAdvancedFees && (
          <Field label="Slippage (bps)" testid="slippage-input"
                 hint="Entry slippage tolerance in basis points (100 = 1%). Speed Mode normally sets this; only override if you know why."
                 value={local.slippage_bps}
                 onChange={(v) => setLocal({ ...local, slippage_bps: parseInt(v, 10) || 0 })} step="50" />
        )}
        <Field label="Kill Switch ($)" testid="killswitch-input"
               hint="If LIVE PnL drops by this much in a single day, the bot auto-disables. Manual reset required."
               value={local.daily_kill_switch_usd}
               onChange={(v) => setLocal({ ...local, daily_kill_switch_usd: parseFloat(v) || 0 })} step="1" />
        <Field label="TP (%)" testid="tp-input"
               hint="Take-Profit target. When unrealized PnL hits +TP%, the bot exits (or sells the TP fraction below if partials are on)."
               value={local.take_profit_pct}
               onChange={(v) => setLocal({ ...local, take_profit_pct: parseFloat(v) || 0 })} step="1" />
        <Field label="SL (%)" testid="sl-input"
               hint="Stop-Loss. When unrealized PnL drops to -SL%, the bot exits the full position with the configured exit slippage."
               value={local.stop_loss_pct}
               onChange={(v) => setLocal({ ...local, stop_loss_pct: parseFloat(v) || 0 })} step="1" />
        <Field label="Max Hold (s)" testid="hold-input"
               hint="Hard time cap on a position. If neither TP nor SL fires within this window, the bot exits as 'timeout'."
               value={local.hold_max_seconds}
               onChange={(v) => setLocal({ ...local, hold_max_seconds: parseInt(v, 10) || 0 })} step="1" />
        {showAdvancedFees && (
          <Field label="Priority µLamp" testid="prio-input"
                 hint="Compute-unit price in micro-lamports. Higher = better landing odds, higher fee. Speed Mode handles this; manual override only."
                 value={local.priority_fee_microlamports}
                 onChange={(v) => setLocal({ ...local, priority_fee_microlamports: parseInt(v, 10) || 0 })} step="100000" />
        )}
        <Field label="Trailing Stop (%)" testid="trailing-input"
               hint="Once price moves favorably, trail by this %. Locks in gains if the move reverses before TP. Set 0 to disable."
               value={local.trailing_stop_pct}
               onChange={(v) => setLocal({ ...local, trailing_stop_pct: parseFloat(v) || 0 })} step="1" />
        {showAdvancedFees && (
          <Field label="Exit Slip (bps)" testid="exit-slip-input"
                 hint="Slippage tolerance for sells. Higher = better landing in fast dumps; lower = preserves more value on the way out."
                 value={local.exit_slippage_bps}
                 onChange={(v) => setLocal({ ...local, exit_slippage_bps: parseInt(v, 10) || 0 })} step="50" />
        )}
        <Field label="TP Sell Frac (%)" testid="partial-tp-input"
               hint="When TP hits, sell only this % of the position. Set 100 to disable partial-TP. The remainder rides with a tightened trailing stop ('runner')."
               value={local.partial_tp_pct}
               onChange={(v) => setLocal({ ...local, partial_tp_pct: parseFloat(v) || 0 })} step="5" />
        <Field label="Runner Trail (%)" testid="partial-trail-input"
               hint="Tighter trailing stop applied to the leftover 'runner' position after a partial TP fires. Locks in the rest of the move."
               value={local.partial_tp_trail_tighten_pct}
               onChange={(v) => setLocal({ ...local, partial_tp_trail_tighten_pct: parseFloat(v) || 0 })} step="1" />
      </div>

      {/* Portfolio / global entry settings (per-band liquidity & buyer thresholds live in the gates table below) */}
      <div className="border-t border-neutral-800 pt-3 mt-1">
        <div className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 mb-2">Portfolio</div>
        <div className="grid grid-cols-2 gap-2 text-xs">
          <Field label="Max Positions" testid="max-positions-input"
                 hint="Maximum concurrent open positions. The bot won't enter a new buy while at this limit."
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
                 hint="Upper bound on token age for the Seasoned band. Tokens older than this fall off the scanner entirely."
                 value={local.scanner_window_hours}
                 onChange={(v) => setLocal({ ...local, scanner_window_hours: parseInt(v, 10) || 0 })} step="1" />
          <Field label="Min Age (min)" testid="scanner-min-age-input"
                 hint="Boundary between New and Seasoned bands. Below this = New (live mempool signals). Above = Seasoned (Pump.fun API signals)."
                 value={local.scanner_min_age_minutes}
                 onChange={(v) => setLocal({ ...local, scanner_min_age_minutes: parseInt(v, 10) || 0 })} step="15" />
          <Field label="Scan every (s)" testid="scanner-interval-input"
                 hint="How often the scanner loop re-evaluates all tracked tokens. Backend enforces a 5s minimum — values below 5 are clamped to 5 to avoid frontend OOM from rapid metric broadcasts. SL/TP reactions on open positions are NOT controlled by this — they use LaserStream WSS push events."
                 value={local.scanner_interval_s}
                 onChange={(v) => setLocal({ ...local, scanner_interval_s: parseInt(v, 10) || 0 })} step="5" />
          <Field label="Inflow Win (s)" testid="scanner-inflow-window-input"
                 hint="Rolling window used to sum SOL inflow into the bonding curve. Compared against 'Min Inflow' gate."
                 value={local.scanner_recent_inflow_window_s}
                 onChange={(v) => setLocal({ ...local, scanner_recent_inflow_window_s: parseInt(v, 10) || 0 })} step="30" />
          <Field label="Max Idle (min)" testid="scanner-max-idle-input"
                 hint="Drop a discovered token from the scanner if no trades land within this idle window. Saves cycles on dead tokens."
                 value={local.scanner_discovery_max_idle_minutes}
                 onChange={(v) => setLocal({ ...local, scanner_discovery_max_idle_minutes: parseInt(v, 10) || 0 })} step="1" />
          <Field label="Entry Vel Win (s)" testid="scanner-entry-vel-window-input"
                 hint="Window used to compute the just-before-entry price velocity. Filters out stale momentum that's already faded."
                 value={local.scanner_entry_velocity_window_s}
                 onChange={(v) => setLocal({ ...local, scanner_entry_velocity_window_s: parseInt(v, 10) || 0 })} step="5" />
          <Field label="Min Entry Vel (%)" testid="scanner-entry-vel-min-input"
                 hint="Minimum % price move within the entry-velocity window for a buy to fire. Keeps the bot on the rising edge."
                 value={local.scanner_entry_velocity_min_pct}
                 onChange={(v) => setLocal({ ...local, scanner_entry_velocity_min_pct: parseFloat(v) || 0 })} step="0.5" />
          <Field label="SL Cooldown (min)" testid="sl-cooldown-input"
                 hint="After a stop-loss exit on a token, ignore that mint for this many minutes. Prevents re-buying into a continuing dump."
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
              <HelpHint label="Distribution vacuum filter">
                Blocks entries where every holder appeared inside the same recent window — a classic pattern for a single insider seeding wallets before the dump.
              </HelpHint>
            </span>
            <span className="text-neutral-600 normal-case tracking-normal">
              reject if all holders appeared in last window
            </span>
          </label>
          <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
            <Field label="Min Holders" testid="gate-distribution-min-input"
                   hint="Token must have at least this many unique holders before the vacuum check applies."
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
              <HelpHint label="Socials gate">
                Requires the token to have at least one social link (twitter/telegram/website) AND a Pump.fun reply count ≥ the threshold below. Filters out totally faceless launches.
              </HelpHint>
            </span>
            <span className="text-neutral-600 normal-case tracking-normal">
              twitter / telegram / website + reply_count
            </span>
          </label>
          <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
            <Field label="Min Reply Count" testid="gate-min-replies-input"
                   hint="Pump.fun comment-thread reply count required. Pure spam tokens usually have 0–2 replies."
                   value={local.gate_min_reply_count}
                   onChange={(v) => setLocal({ ...local, gate_min_reply_count: parseInt(v, 10) || 0 })} step="5" />
          </div>
        </div>

        {/* Greylist Sniper */}
        <div className="mt-3 border-t border-neutral-800 pt-2.5">
          <label className="flex items-center justify-between text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-400 cursor-pointer">
            <span className="flex items-center gap-1.5">
              <input
                type="checkbox"
                data-testid="greylist-snipe-enabled-checkbox"
                checked={local.greylist_snipe_enabled}
                onChange={(e) => setLocal({ ...local, greylist_snipe_enabled: e.target.checked })}
              />
              Greylist Sniper
              <HelpHint label="Greylist Sniper">
                Fires on every NEW launch where the creator scored ≥ Min Score on the greylist. Bypasses momentum gates (growth/inflow/buyers/velocity) since greylisted creators rarely pump organically — the entire point is sniping their predictable curve. Still honors kill switch + max-positions + cooldowns.
              </HelpHint>
            </span>
            <span className="text-neutral-600 normal-case tracking-normal">
              snipe greylist creators on every launch
            </span>
          </label>
          <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
            <Field label="Min Score" testid="greylist-snipe-min-score-input"
                   hint="Effective (decayed) greylist score required to fire. 45 = hybrid threshold, 70 = aggressive threshold."
                   value={local.greylist_snipe_min_score}
                   onChange={(v) => setLocal({ ...local, greylist_snipe_min_score: parseFloat(v) || 0 })} step="5" />
            <Field label="Max/hr" testid="greylist-snipe-max-per-hour-input"
                   hint="Rolling 1h fire cap. Safety net so a wave of greylist launches can't blow through the wallet."
                   value={local.greylist_snipe_max_per_hour}
                   onChange={(v) => setLocal({ ...local, greylist_snipe_max_per_hour: parseInt(v, 10) || 0 })} step="1" />
            <Field label="Settle (s)" testid="greylist-snipe-settle-seconds-input"
                   hint="Wait this long after launch detection before buying. Lets the tracking bucket populate first-seen price / liquidity."
                   value={local.greylist_snipe_settle_seconds}
                   onChange={(v) => setLocal({ ...local, greylist_snipe_settle_seconds: parseInt(v, 10) || 0 })} step="1" />
          </div>
          <div className="mt-2 text-[10px] uppercase tracking-[0.15em] text-neutral-500">Pattern-based exits (no SL, no max-hold)</div>
          <div className="mt-1 grid grid-cols-4 gap-2 text-xs">
            <Field label="Peak MC %" testid="greylist-snipe-peak-mc-input"
                   hint="Exit when current MC reaches this % of the creator's typical peak MC. Lower = earlier exit before the predictable rug. 85% is a safe default."
                   value={local.greylist_snipe_peak_mc_proximity_pct}
                   onChange={(v) => setLocal({ ...local, greylist_snipe_peak_mc_proximity_pct: parseFloat(v) || 0 })} step="5" />
            <Field label="Curve Buffer pp" testid="greylist-snipe-curve-buf-input"
                   hint="Exit when curve fill % is within this many points of the creator's typical rug curve %. 5pp = exit at rug_pct - 5. Higher = earlier exit."
                   value={local.greylist_snipe_curve_buffer_pct}
                   onChange={(v) => setLocal({ ...local, greylist_snipe_curve_buffer_pct: parseFloat(v) || 0 })} step="1" />
            <Field label="Ripcord %" testid="greylist-snipe-ripcord-input"
                   hint="Emergency exit when price drops this much FROM OBSERVED PEAK (not from entry). 60% means the rug already happened — bail. NOT an entry-loss SL."
                   value={local.greylist_snipe_ripcord_drawdown_pct}
                   onChange={(v) => setLocal({ ...local, greylist_snipe_ripcord_drawdown_pct: parseFloat(v) || 0 })} step="5" />
            <Field label="Ripcord Grace (s)" testid="greylist-snipe-grace-input"
                   hint="Ripcord requires the drawdown to be sustained this many seconds before firing. Kills wick-driven false exits."
                   value={local.greylist_snipe_ripcord_grace_seconds}
                   onChange={(v) => setLocal({ ...local, greylist_snipe_ripcord_grace_seconds: parseInt(v, 10) || 0 })} step="1" />
          </div>

          {/* Profit ripcord + velocity decay exits */}
          <div className="mt-3 border-t border-dashed border-neutral-800 pt-2.5">
            <div className="text-[10px] uppercase tracking-[0.15em] text-emerald-400/80 mb-1.5 flex items-center gap-1.5">
              Profit Ripcord & Velocity Decay
              <HelpHint label="Snipe-only exits beyond pattern TP">
                These are sniper-only exit gates that run BEFORE the pattern/curve/peak-MC checks:
                <br /><br />
                <b>Profit Ripcord</b> — hard TP at X% above entry. Always wins over pattern TP. Defaults to 100% (lock the 2x before the rug erases it). Set to 0 to disable.
                <br /><br />
                <b>SOL-velocity decay</b> — exit when SOL inflow rate in the last N sec drops by X% vs the prior baseline window. The buying wave is exhausting → rug imminent.
                <br /><br />
                <b>New-holder velocity decay</b> — exit when fresh unique buyers / sec collapses vs baseline. FOMO has dried up → no one left to dump on.
                <br /><br />
                Both decay gates require a minimum number of buys in the baseline window to avoid cold-start false exits.
              </HelpHint>
            </div>
            <div className="grid grid-cols-3 gap-2 text-xs">
              <Field label="Profit Ripcord %" testid="greylist-snipe-profit-ripcord-input"
                     hint="Hard TP — exits the snipe when up X% from entry, regardless of pattern. 100 = +100% (2x). Set 0 to disable."
                     value={local.greylist_snipe_profit_ripcord_pct}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_profit_ripcord_pct: parseFloat(v) || 0 })} step="10" />
              <Field label="SOL Vel Drop %" testid="greylist-snipe-sol-vel-drop-input"
                     hint="Exit when recent SOL inflow rate is below (100% − this) of the baseline rate. 70 = exit when SOL/s falls to 30% or less of baseline."
                     value={local.greylist_snipe_sol_vel_drop_pct}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_sol_vel_drop_pct: parseFloat(v) || 0 })} step="5" />
              <Field label="Holder Vel Drop %" testid="greylist-snipe-holder-vel-drop-input"
                     hint="Exit when fresh new-buyer rate falls by this % vs baseline. 70 = exit when new-holders/s falls to 30% or less of baseline."
                     value={local.greylist_snipe_holder_vel_drop_pct}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_holder_vel_drop_pct: parseFloat(v) || 0 })} step="5" />
            </div>
            <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
              <Field label="Recent Window (s)" testid="greylist-snipe-vel-window-input"
                     hint="Recent activity window for velocity comparison. Smaller = faster reaction. 15s default."
                     value={local.greylist_snipe_velocity_window_s}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_velocity_window_s: parseInt(v, 10) || 0 })} step="5" />
              <Field label="Baseline (s)" testid="greylist-snipe-vel-baseline-input"
                     hint="Prior baseline window length, ending at the start of the recent window. Larger = more stable comparison."
                     value={local.greylist_snipe_velocity_baseline_s}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_velocity_baseline_s: parseInt(v, 10) || 0 })} step="15" />
              <Field label="Min Baseline Buys" testid="greylist-snipe-vel-min-buys-input"
                     hint="Velocity decay only fires when the baseline window has ≥ this many buys. Cold-start guard. 8 default."
                     value={local.greylist_snipe_velocity_min_buys}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_velocity_min_buys: parseInt(v, 10) || 0 })} step="1" />
            </div>
            <label className="mt-2 flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-400 cursor-pointer">
              <input
                type="checkbox"
                data-testid="greylist-snipe-velocity-exits-checkbox"
                checked={!!local.greylist_snipe_velocity_exits_enabled}
                onChange={(e) => setLocal({ ...local, greylist_snipe_velocity_exits_enabled: e.target.checked })}
              />
              Velocity Exits Enabled
              <span className="text-neutral-600 normal-case tracking-normal ml-1">(both gates on/off — profit ripcord stays independent)</span>
            </label>
          </div>

          {/* Research Mode — unpredictable-creator bimodal exploration */}
          <div className="mt-3 border-t border-dashed border-neutral-800 pt-2.5">
            <label className="flex items-center justify-between text-[10px] font-mono uppercase tracking-[0.15em] text-amber-400/80 cursor-pointer">
              <span className="flex items-center gap-1.5">
                <input
                  type="checkbox"
                  data-testid="greylist-snipe-research-mode-checkbox"
                  checked={!!local.greylist_snipe_research_mode}
                  onChange={(e) => setLocal({ ...local, greylist_snipe_research_mode: e.target.checked })}
                />
                Research Mode
                <HelpHint label="Research Mode">
                  EXPERIMENTAL. When ON, the sniper also fires on creators currently classified as `unpredictable_rug` (high curve_fill variance — would normally be blacklisted). Size is automatically halved via Research Size Mult. The bimodal detector still promotes tight 2-cluster creators to `bimodal_dump_tradeable` (full size) — research mode only catches the genuinely chaotic ones so we can collect win-rate data. `untradeable_rug` and `out_of_band` creators stay blocked.
                </HelpHint>
              </span>
              <span className="text-neutral-600 normal-case tracking-normal">
                snipe unpredictable creators at half size
              </span>
            </label>
            <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
              <Field label="Research Min Score" testid="greylist-snipe-research-min-score-input"
                     hint="Effective greylist score required for a RESEARCH snipe (separate floor from the normal Min Score). 35 is the recommended observation threshold — these creators are noisy but tradeable enough to learn from."
                     value={local.greylist_snipe_research_min_score}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_research_min_score: parseFloat(v) || 0 })} step="5" />
              <Field label="Research Size Mult" testid="greylist-snipe-research-size-mult-input"
                     hint="Multiplier applied on top of the risk-bucket size for research snipes. 0.5 = half size. Lower = safer experimental positions. Trades are stamped `is_research_snipe=true` for downstream PnL bucketing."
                     value={local.greylist_snipe_research_size_mult}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_research_size_mult: parseFloat(v) || 0 })} step="0.1" />
            </div>
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
                     hint="Minimum price growth (from first-seen price) to pass the band. Higher = momentum has to be more obvious before entry."
                     newTestid="scanner-growth-new-input"
                     newValue={local.scanner_min_growth_pct_new}
                     onNewChange={(v) => setLocal({ ...local, scanner_min_growth_pct_new: parseFloat(v) || 0 })}
                     seasonedTestid="scanner-growth-input"
                     seasonedValue={local.scanner_min_growth_pct}
                     onSeasonedChange={(v) => setLocal({ ...local, scanner_min_growth_pct: parseFloat(v) || 0 })}
                     step="5" />
            <GateRow label="Min Liquidity (SOL)"
                     hint="Minimum SOL liquidity on the bonding curve / pool. Filters out micro-cap noise where your own buy moves the price too much."
                     newTestid="min-liq-new-input"
                     newValue={local.min_curve_liquidity_sol_new}
                     onNewChange={(v) => setLocal({ ...local, min_curve_liquidity_sol_new: parseFloat(v) || 0 })}
                     seasonedTestid="min-liq-seasoned-input"
                     seasonedValue={local.min_curve_liquidity_sol}
                     onSeasonedChange={(v) => setLocal({ ...local, min_curve_liquidity_sol: parseFloat(v) || 0 })}
                     step="0.5" />
            {/* New-only: live mempool signals from Helius */}
            <GateRow label="Min Inflow (SOL/win)" newOnly
                     hint="Total SOL flowing into the bonding curve over the 'Inflow Win'. Real cash demand — not just price spikes."
                     newTestid="scanner-inflow-new-input"
                     newValue={local.scanner_min_recent_inflow_sol_new}
                     onNewChange={(v) => setLocal({ ...local, scanner_min_recent_inflow_sol_new: parseFloat(v) || 0 })}
                     step="0.5" />
            <GateRow label="Min new buyers (1m)" newOnly
                     hint="Distinct new wallets that bought in the last 60s. Filters fake pumps driven by one wallet round-tripping."
                     newTestid="scanner-newbuyers-new-input"
                     newValue={local.scanner_min_new_buyers_new}
                     onNewChange={(v) => setLocal({ ...local, scanner_min_new_buyers_new: parseInt(v, 10) || 0 })}
                     step="1" />
            <GateRow label="Min Total Holders" newOnly
                     hint="Minimum unique buyer count on the token. Low holder counts = high rug / one-wallet risk."
                     newTestid="min-buyers-new-input"
                     newValue={local.min_buyers_for_entry_new}
                     onNewChange={(v) => setLocal({ ...local, min_buyers_for_entry_new: parseInt(v, 10) || 0 })}
                     step="1" />
            {/* Seasoned-only: Pump.fun API polled signals */}
            <GateRow label="Min MC ($)" seasonedOnly
                     hint="Minimum USD market cap (from Pump.fun API). Bigger = lower rug risk, lower upside ceiling."
                     seasonedTestid="scanner-mc-seasoned-input"
                     seasonedValue={local.scanner_min_mc_usd_seasoned}
                     onSeasonedChange={(v) => setLocal({ ...local, scanner_min_mc_usd_seasoned: parseFloat(v) || 0 })}
                     step="1000" />
            <GateRow label="Min MC vel (5m %)" seasonedOnly last
                     hint="Minimum % market-cap velocity over the last 5 minutes. Catches Seasoned tokens that are still actively pumping."
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
                 hint="Maximum number of re-entry buys allowed on a single token after the original exit."
                 value={local.reentry_max_attempts}
                 onChange={(v) => setLocal({ ...local, reentry_max_attempts: parseInt(v, 10) || 0 })} step="1" />
          <Field label="Pullback (%)" testid="reentry-pullback-input"
                 hint="Required pullback (from post-exit local peak) before the bot re-enters. Bigger = wait for deeper dip."
                 value={local.reentry_pullback_pct}
                 onChange={(v) => setLocal({ ...local, reentry_pullback_pct: parseFloat(v) || 0 })} step="1" />
          <Field label="Window (s)" testid="reentry-window-input"
                 hint="Time window after exit during which re-entry is considered. After this, the token falls off the watchlist."
                 value={local.reentry_window_seconds}
                 onChange={(v) => setLocal({ ...local, reentry_window_seconds: parseInt(v, 10) || 0 })} step="30" />
          <Field label="Size ×" testid="reentry-size-input"
                 hint="Position size multiplier for re-entries (e.g., 0.5 = half size). Risk control on a token you already exited once."
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

      <ConfigSyncPanel onApplied={(cfg) => setLocal(cfg)} />
    </div>
  );
}

function Field({ label, value, onChange, step, testid, hint }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 inline-flex items-center gap-1">
        {label}
        {hint && <HelpHint label={`help: ${label}`}>{hint}</HelpHint>}
      </span>
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

function GateRow({ label, hint, newTestid, newValue, onNewChange, seasonedTestid, seasonedValue, onSeasonedChange, step, last, newOnly, seasonedOnly }) {
  const cell = "px-2 py-1 border-l border-neutral-800";
  const dim = "px-2 py-1 border-l border-neutral-800 text-[10px] font-mono text-neutral-700 italic text-center self-center";
  return (
    <div className={`grid grid-cols-[1.4fr_1fr_1fr] ${last ? "" : "border-b border-neutral-800"}`}>
      <div className="px-2 py-1 text-[10px] uppercase tracking-[0.1em] text-neutral-400 font-mono self-center inline-flex items-center gap-1">
        {label}
        {hint && <HelpHint label={`help: ${label}`}>{hint}</HelpHint>}
      </div>
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
