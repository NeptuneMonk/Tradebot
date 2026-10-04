import { memo, useEffect, useState } from "react";
import { Play, Square, ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import { explainApiError } from "@/lib/api";

// Phase 2 (cut-the-fat): the six controls a first-time operator needs. Everything else lives in the Advanced drawer.
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
          <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-500">Speed</span>
          <Seg options={speedOpts} value={speed} onPick={(v) => patch({ speed_mode: v }, `Speed ${v}`)} testid="simple-speed" />
        </div>
        <div className="flex items-center gap-2 ml-auto">
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
