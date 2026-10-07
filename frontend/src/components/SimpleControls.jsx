import { memo, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Play, Square, ShieldCheck, Activity } from "lucide-react";
import { toast } from "sonner";
import { explainApiError } from "@/lib/api";

// Phase 2 (cut-the-fat): the six controls a first-time operator needs. Everything else lives in the Advanced drawer.
// "Bounce" preset — catch the second leg on brand-new, tiny-MC launches: a first inflow, a drawback, then the bounce.
// Entry = new band ≤ 20 min, dip ≥ 25 % from the tracked peak that is recovering with buyers still expanding; holders,
// liquidity and inflow floors set low for the sub-$10k tape. Exits = tight SL, short clock, +3 % momentum floor so a
// red/flat position is never sold on "no momentum" (3 % ≈ the Pump.fun round trip). Books/sizes/mode are left alone.
export const BOUNCE_PRESET = {
  band_new_min_age_min: 0.5, band_new_max_age_min: 20,
  scanner_min_age_minutes: 2, scanner_window_hours: 1,
  scanner_second_impulse_enabled: true, scanner_second_impulse_dip_pct: 25,
  min_buyers_for_entry_new: 3, min_curve_liquidity_sol_new: 0,
  scanner_min_growth_pct_new: 0, scanner_min_recent_inflow_sol_new: 0.5, scanner_min_new_buyers_new: 2,
  scanner_recent_inflow_window_s: 120, scanner_holder_velocity_window_s: 60,
  no_momentum_min_profit_pct: 3, no_momentum_after_s: 20, no_momentum_min_mfe_pct: 3,
  live_doctor_entry_filter: false,   // the Doctor's likeness filter skips launches that look like recent exit liquidity — exactly the drawbacks Bounce buys
};
export const BOUNCE_SCALP_EXITS = { stop_loss_pct: 8, target_r: 1.5, trailing_stop_pct: 5, trailing_arm_pct: 8, hold_max_seconds: 60 };

// Is the live config still the Bounce preset? Returns the settings that drifted (empty = Bounce is ON).
export function bounceDrift(config) {
  if (!config) return [];
  const scalp = (config.book_exits || {}).scalp || {};
  const diff = [];
  for (const [k, want] of Object.entries(BOUNCE_PRESET)) if (config[k] !== undefined && Number(config[k]) !== Number(want) && config[k] !== want) diff.push(`${k}: ${String(config[k])} (bounce ${String(want)})`);
  for (const [k, want] of Object.entries(BOUNCE_SCALP_EXITS)) if (scalp[k] !== undefined && Number(scalp[k]) !== Number(want)) diff.push(`scalp ${k}: ${scalp[k]} (bounce ${want})`);
  return diff;
}

export const SAFE_PAPER_PRESET = {
  live_trading: false, rh_live_trading: false, max_trade_usd: 8, rh_max_trade_usd: 8, max_concurrent_positions: 2,
  book_scalp_enabled: true, book_hunt_enabled: true, book_runner_enabled: true, rh_paper_enabled: true, autopilot_enabled: false,
};

function Seg({ options, value, onPick, testid, danger }) {
  return (
    <div className="flex border border-neutral-800" data-testid={testid}>
      {options.map(([v, label]) => {
        const on = v === value;
        const tone = on ? (danger && v === danger ? "bg-red-950/60 text-red-300 border-red-800" : "bg-cyan-950/50 text-cyan-200 border-cyan-800") : "text-neutral-500 hover:text-neutral-200 border-transparent";
        return (
          <button key={v} type="button" onClick={() => onPick(v)} data-testid={`${testid}-${v}`}
            className={`px-3 py-1.5 text-[11px] uppercase tracking-[0.18em] border -m-px transition-colors ${tone}`}>{label}</button>
        );
      })}
    </div>
  );
}

function Money({ label, value, onCommit, testid, step = 1, hint }) {
  const [v, setV] = useState(value ?? "");
  useEffect(() => { setV(value ?? ""); }, [value]);
  const commit = () => { const n = parseFloat(v); if (Number.isFinite(n) && n !== value) onCommit(n); else setV(value ?? ""); };
  return (
    <label className="flex flex-col gap-1" title={hint}>
      <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-500">{label}</span>
      <span className="flex items-center border border-neutral-800 bg-neutral-950 px-2">
        <span className="text-neutral-600 text-sm">$</span>
        <input type="number" step={step} min="0" value={v} onChange={(e) => setV(e.target.value)} onBlur={commit}
          onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()} data-testid={testid}
          className="w-24 bg-transparent py-1.5 pl-1 text-sm text-neutral-100 outline-none" />
      </span>
    </label>
  );
}

// PnL stop — the one guard meant to stay on while you are logged out: realised + open PnL since ARM ≤ −limit → bot off,
// kill switch tripped, open positions flattened. Counts from the moment you arm it (not from midnight).
function PnlStop({ config, status }) {
  const snap = status?.pnl_stop;
  const [limit, setLimit] = useState(String(config.pnl_stop_usd || ""));
  useEffect(() => { setLimit(String(config.pnl_stop_usd || "")); }, [config.pnl_stop_usd]);
  const arm = (v) => api.pnlStopArm(v, config.pnl_stop_flatten !== false)
    .then((r) => toast.success(r.armed ? `PnL stop armed: −$${Number(r.limit_usd).toFixed(2)} from now (${r.mode})` : "PnL stop disarmed"))
    .catch((e) => toast.error(e?.response?.data?.detail || e.message));
  const armed = !!snap?.armed;
  const pnl = Number(snap?.pnl_usd || 0);
  return (
    <div className="flex flex-col gap-1" data-testid="pnl-stop">
      <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-500"
        title="Stop-loss on your PnL: realised + open PnL since you armed it. Hits the limit → bot OFF, kill switch, positions flattened. Set the $ you are willing to lose from NOW; 0 disarms.">
        PnL stop{armed ? <span className={`ml-2 normal-case tracking-normal ${pnl < 0 ? "text-rose-300" : "text-emerald-300"}`} data-testid="pnl-stop-pnl">{pnl >= 0 ? "+" : "−"}${Math.abs(pnl).toFixed(2)} / −${Number(snap.limit_usd).toFixed(0)}</span> : null}
      </span>
      <div className="flex items-center gap-1">
        <input type="number" min="0" step="5" value={limit} onChange={(e) => setLimit(e.target.value)} data-testid="pnl-stop-limit-input" placeholder="$"
          className="w-16 bg-neutral-950 border border-neutral-800 px-2 py-1 text-[11px] font-mono text-neutral-200" />
        <button type="button" onClick={() => arm(parseFloat(limit) || 0)} data-testid="pnl-stop-arm"
          className={`px-2 py-1.5 text-[11px] uppercase tracking-[0.18em] border transition-colors ${armed ? "border-rose-800 bg-rose-950/40 text-rose-200" : "border-neutral-800 text-neutral-400 hover:text-neutral-100"}`}>
          {armed ? "re-arm" : "arm"}
        </button>
        {armed ? <button type="button" onClick={() => arm(0)} data-testid="pnl-stop-disarm" className="px-2 py-1.5 text-[11px] uppercase tracking-[0.18em] border border-neutral-800 text-neutral-500 hover:text-neutral-200">off</button> : null}
      </div>
    </div>
  );
}

function Book({ k, label, on, onFlip, extra }) {
  return (
    <button type="button" onClick={() => onFlip(!on)} data-testid={`simple-book-${k}`}
      className={`px-3 py-1.5 text-[11px] uppercase tracking-[0.18em] border transition-colors ${on ? "border-emerald-800 bg-emerald-950/40 text-emerald-200" : "border-neutral-800 text-neutral-500 hover:text-neutral-200"}`}>
      {label}{extra ? <span className="ml-1 text-[9px] text-red-300">{extra}</span> : null}
    </button>
  );
}

function SimpleControls({ config, status, wallet, pl, onPatch, onStart, onStop }) {
  const [arm, setArm] = useState(null);   // "live" | "preset" two-click confirmation
  useEffect(() => { if (!arm) return; const t = setTimeout(() => setArm(null), 6000); return () => clearTimeout(t); }, [arm]);
  if (!config) return <div className="control-card text-neutral-500 text-sm">Loading…</div>;
  const running = !!status?.enabled;
  const patch = (p, okMsg) => onPatch(p).then((saved) => okMsg && toast.success(typeof okMsg === "function" ? okMsg(saved || {}) : okMsg)).catch((e) => toast.error(explainApiError?.(e) || "Save failed"));
  // the server floors max_trade_usd at min_trade_usd — bring the floor down with the ceiling so the cap really applies
  const capPatch = (n) => ({ max_trade_usd: n, rh_max_trade_usd: n, ...(Number(config.min_trade_usd) > n ? { min_trade_usd: n } : {}) });
  const pickMode = (m) => {
    if (m === "paper") return patch({ live_trading: false }, "Paper mode");
    if (arm !== "live") { setArm("live"); toast.message("Click LIVE again to arm real-money trading"); return; }
    setArm(null); patch({ live_trading: true }, "LIVE armed — real SOL at risk");
  };
  const applyPreset = () => {
    if (arm !== "preset") { setArm("preset"); toast.message("Click again to apply Safe paper (settings only — wallets and open positions untouched)"); return; }
    setArm(null); patch({ ...SAFE_PAPER_PRESET, ...(Number(config.min_trade_usd) > 8 ? { min_trade_usd: 8 } : {}) }, "Safe paper preset applied");
  };
  const applyBounce = () => {
    if (arm !== "bounce") { setArm("bounce"); toast.message("Click again to apply the Bounce preset (gates + scalp exits only — books, sizes and mode untouched)"); return; }
    setArm(null);
    const bx = { ...(config.book_exits || {}), scalp: { ...((config.book_exits || {}).scalp || {}), ...BOUNCE_SCALP_EXITS } };
    patch({ ...BOUNCE_PRESET, book_exits: bx }, "Bounce preset applied — new launches ≤20 min, dip ≥25 % then recovering, SL 8 %, clock 60 s, +3 % momentum floor");
  };
  const speed = config.speed_mode;
  const speedOpts = [["eco", "Eco"], ["normal", "Normal"], ...(speed !== "eco" && speed !== "normal" ? [[speed, `custom · ${speed}`]] : [])];
  const dailyPnl = pl?.daily_pnl_usd ?? 0;
  return (
    <div className="control-card space-y-4" data-testid="simple-controls">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="text-[10px] uppercase tracking-[0.25em] text-neutral-500">Controls</div>
        <div className="flex items-center gap-4 text-[11px] font-mono text-neutral-400" data-testid="simple-status-line">
          <span>wallet <span className="text-neutral-100">{wallet ? `${wallet.sol_balance.toFixed(3)} SOL` : "—"}</span></span>
          <span>today <span className={dailyPnl >= 0 ? "text-emerald-300" : "text-red-300"}>{dailyPnl >= 0 ? "+" : "-"}${Math.abs(dailyPnl).toFixed(2)}</span></span>
          <span>open <span className="text-neutral-100">{status?.active_trade_count ?? 0}</span></span>
        </div>
      </div>
      <div className="flex flex-wrap items-end gap-6">
        <div className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-500">Mode</span>
          <Seg options={[["paper", "Paper"], ["live", arm === "live" ? "Arm live?" : "Live"]]} value={config.live_trading ? "live" : "paper"} onPick={pickMode} testid="simple-mode" danger="live" />
        </div>
        <Money label="Max trade" value={config.max_trade_usd} onCommit={(n) => patch(capPatch(n), (saved) => `Max trade $${saved.max_trade_usd ?? n}`)} testid="simple-max-trade"
          hint="Per-trade cap in USD (Solana and Robinhood). R-sizing still decides the actual size underneath — this is the ceiling." />
        <Money label="Daily stop" value={config.daily_kill_switch_usd} onCommit={(n) => patch({ daily_kill_switch_usd: n }, `Daily stop $${n}`)} testid="simple-daily-stop"
          hint="Realised loss today that trips the kill switch and stops new entries." />
        <div className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-500">Books</span>
          <div className="flex gap-1">
            <Book k="scalp" label="Scalp" on={config.book_scalp_enabled !== false} onFlip={(v) => patch({ book_scalp_enabled: v })} />
            <Book k="hunt" label="Hunt" on={config.book_hunt_enabled !== false} onFlip={(v) => patch({ book_hunt_enabled: v })} />
            <Book k="runner" label="Runner" on={config.book_runner_enabled !== false} onFlip={(v) => patch({ book_runner_enabled: v })} />
            <Book k="rh" label="RH" on={!!config.rh_paper_enabled} onFlip={(v) => patch({ rh_paper_enabled: v })} extra={config.rh_live_trading ? "LIVE" : null} />
          </div>
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-500" title="reputation.family CRAZY-ranked devs: buy min stake with no gates, park as long-term hold — only while this dashboard is open and Autopilot is off">Crazy dev</span>
          <button type="button" onClick={() => patch({ dev_watch_enabled: config.dev_watch_enabled === false }, config.dev_watch_enabled === false ? "Crazy-dev watch ON — CRAZY devs go straight to LTH" : "Crazy-dev watch OFF")}
            data-testid="simple-dev-watch" aria-pressed={config.dev_watch_enabled !== false}
            className={`px-3 py-1.5 text-[11px] uppercase tracking-[0.18em] border transition-colors ${config.dev_watch_enabled !== false ? "border-cyan-700 bg-cyan-950/40 text-cyan-200" : "border-neutral-800 text-neutral-500 hover:text-neutral-200"}`}>
            {config.dev_watch_enabled !== false ? "→ LTH on" : "→ LTH off"}
          </button>
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-500" title="Inventory halt: after N stop-out/rug closes inside the window, no new Solana entries until it rolls off (or you LIFT it on the banner). Count and window are in Advanced.">Inv. halt</span>
          <Book k="inventory-halt" label={config.inventory_halt_enabled !== false ? `on · ${config.inventory_halt_n ?? 5}×` : "off"} on={config.inventory_halt_enabled !== false}
            onFlip={(v) => patch({ inventory_halt_enabled: v }, v ? "Inventory halt ON" : "Inventory halt OFF — streaks of stop-outs no longer pause Solana entries")} />
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-500" title="Live-Doctor entry filter: scores each launch against recent winners vs recent exit liquidity and skips / half-sizes the look-alikes ('live-doctor skip'). Runs even with Autopilot OFF. Turn it off for drawback strategies like Bounce — breakers and the hour profile keep running.">Doctor filter</span>
          <Book k="doctor-filter" label={config.live_doctor_entry_filter !== false ? "on" : "off"} on={config.live_doctor_entry_filter !== false}
            onFlip={(v) => patch({ live_doctor_entry_filter: v }, v ? "Doctor entry filter ON — look-alikes of recent exit liquidity are skipped / half-sized" : "Doctor entry filter OFF — every launch that passes the gates is sized full")} />
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-500" title="Live-Doctor breakers bench a book whose recent payoff is below YOUR target (a 0.4R target is judged against 0.4, not 1.0) or whose target looks unreachable. Off = never bench. Runs even with Autopilot OFF.">Doctor breakers</span>
          <Book k="doctor-breakers" label={config.live_doctor_breakers_enabled !== false ? "on" : "off"} on={config.live_doctor_breakers_enabled !== false}
            onFlip={(v) => patch({ live_doctor_breakers_enabled: v }, v ? "Doctor breakers ON — books can be benched on payoff vs your target" : "Doctor breakers OFF — books are never benched; any existing pause is lifted")} />
        </div>
        <PnlStop config={config} status={status} />
        <div className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-500">Speed</span>
          <Seg options={speedOpts} value={speed} onPick={(v) => patch({ speed_mode: v }, `Speed ${v}`)} testid="simple-speed" />
        </div>
        <div className="flex items-center gap-2 ml-auto">
          {(() => {
            const drift = bounceDrift(config);
            const on = drift.length === 0;
            return (
              <span data-testid="simple-bounce-state"
                title={on ? "Every Bounce gate and scalp exit is in place" : `Bounce is OFF — ${drift.length} setting(s) differ from the preset:\n${drift.join("\n")}\nClick Bounce twice to re-apply.`}
                className={`px-2 py-1 text-[10px] font-mono uppercase tracking-[0.18em] border ${on ? "border-emerald-700 text-emerald-300 bg-emerald-950/40" : "border-neutral-800 text-neutral-500"}`}>
                bounce {on ? "on" : `off · ${drift.length} differ`}
              </span>
            );
          })()}
          <button type="button" onClick={applyBounce} data-testid="simple-preset-bounce"
            title={"Bounce preset: catch the second leg on brand-new tiny-MC launches.\nNew band 0.5–20 min · dip ≥ 25 % from peak and recovering · buyers ≥ 3 · no liquidity floor · inflow ≥ 0.5 SOL / 2 min\nScalp exits: SL 8 % · target 1.5R · trail 5 % armed at +8 % · clock 60 s · no-momentum only above +3 %"}
            className={`flex items-center gap-1.5 px-3 py-2 text-[11px] uppercase tracking-[0.18em] border transition-colors ${arm === "bounce" ? "border-amber-700 text-amber-200" : "border-neutral-800 text-neutral-400 hover:text-neutral-100"}`}>
            <Activity className="w-3.5 h-3.5" /> {arm === "bounce" ? "Apply bounce?" : "Bounce"}
          </button>
          <button type="button" onClick={applyPreset} data-testid="simple-preset-safe-paper"
            className={`flex items-center gap-1.5 px-3 py-2 text-[11px] uppercase tracking-[0.18em] border transition-colors ${arm === "preset" ? "border-amber-700 text-amber-200" : "border-neutral-800 text-neutral-400 hover:text-neutral-100"}`}>
            <ShieldCheck className="w-3.5 h-3.5" /> {arm === "preset" ? "Apply safe paper?" : "Safe paper"}
          </button>
          <button type="button" onClick={running ? onStop : onStart} data-testid="simple-start-stop"
            className={`flex items-center gap-2 px-5 py-2 text-[12px] uppercase tracking-[0.2em] border transition-colors ${running ? "border-red-800 bg-red-950/50 text-red-200 hover:bg-red-950" : "border-emerald-700 bg-emerald-950/50 text-emerald-200 hover:bg-emerald-950"}`}>
            {running ? <><Square className="w-3.5 h-3.5" /> Stop</> : <><Play className="w-3.5 h-3.5" /> Start</>}
          </button>
        </div>
      </div>
    </div>
  );
}

export default memo(SimpleControls);
