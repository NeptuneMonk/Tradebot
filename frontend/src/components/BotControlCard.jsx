import { useState, useEffect } from "react";
import { Power, Zap, Settings2 } from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { toast } from "sonner";

export default function BotControlCard({ status, config, onUpdate, onStart, onStop }) {
  const [local, setLocal] = useState(null);

  useEffect(() => { if (config) setLocal(config); }, [config]);

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

      <div className="grid grid-cols-2 gap-2 text-xs">
        <Field label="Min Trade ($)" testid="min-trade-input"
               value={local.min_trade_usd}
               onChange={(v) => setLocal({ ...local, min_trade_usd: parseFloat(v) || 0 })} step="0.1" />
        <Field label="Max Trade ($)" testid="max-trade-input"
               value={local.max_trade_usd}
               onChange={(v) => setLocal({ ...local, max_trade_usd: parseFloat(v) || 0 })} step="0.1" />
        <Field label="Slippage (bps)" testid="slippage-input"
               value={local.slippage_bps}
               onChange={(v) => setLocal({ ...local, slippage_bps: parseInt(v, 10) || 0 })} step="50" />
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
        <Field label="Priority µLamp" testid="prio-input"
               value={local.priority_fee_microlamports}
               onChange={(v) => setLocal({ ...local, priority_fee_microlamports: parseInt(v, 10) || 0 })} step="100000" />
      </div>

      {/* Entry filters */}
      <div className="border-t border-neutral-800 pt-3 mt-1">
        <div className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 mb-2">Entry Filters</div>
        <div className="grid grid-cols-2 gap-2 text-xs">
          <Field label="Min Liquidity (SOL)" testid="min-liq-input"
                 value={local.min_curve_liquidity_sol}
                 onChange={(v) => setLocal({ ...local, min_curve_liquidity_sol: parseFloat(v) || 0 })} step="0.5" />
          <Field label="Min Buyers" testid="min-buyers-input"
                 value={local.min_buyers_for_entry}
                 onChange={(v) => setLocal({ ...local, min_buyers_for_entry: parseInt(v, 10) || 0 })} step="1" />
          <Field label="Max Positions" testid="max-positions-input"
                 value={local.max_concurrent_positions}
                 onChange={(v) => setLocal({ ...local, max_concurrent_positions: parseInt(v, 10) || 0 })} step="1" />
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
