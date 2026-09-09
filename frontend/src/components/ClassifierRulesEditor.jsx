import { useState, useEffect } from "react";
import { Sliders, Save } from "lucide-react";
import { toast } from "sonner";
import HelpHint from "./HelpHint";

export default function ClassifierRulesEditor({ rules, onSave }) {
  const [local, setLocal] = useState(null);
  useEffect(() => { if (rules) setLocal(rules); }, [rules]);
  if (!local) return <div className="control-card text-neutral-500 text-sm">Loading…</div>;

  const dirty = JSON.stringify(local) !== JSON.stringify(rules);

  const set = (k) => (e) => {
    const v = e.target.type === "number" ? parseFloat(e.target.value) || 0 : e.target.value;
    setLocal({ ...local, [k]: v });
  };

  const save = async () => {
    try {
      await onSave(local);
      toast.success("Rules saved");
    } catch (e) {
      toast.error("Save failed");
    }
  };

  return (
    <div className="control-card" data-testid="classifier-rules-card">
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500 mb-1">
        <Sliders className="w-3 h-3" /> Classifier Rules
        <HelpHint label="Classifier Rules">
          Book router for Pump.fun launches. The verdict is one of <b>scalp</b>, <b>hunt</b> or <b>skip</b>: creators with a tradeable greylist pattern route to the <b>hunt</b> book, creators with untradeable rug history are kept out of <b>scalp</b>, and a late chase (fast curve fill) or a dead launch (low inflow) is a <b>skip</b> — never an entry. Nothing here exits a live position; exits live in the per-book ladders.
        </HelpHint>
      </div>
      <div className="text-[10px] text-neutral-600 font-mono mb-3 tracking-wide">
        routes scalp entries · hunt entries come from the Greylist Sniper / re-entry with their own gates
      </div>
      <div className="space-y-2 text-xs">
        <Row label="Fast curve fill (%)" hint="SKIP (late chase) if the bonding curve already filled ≥ X% within the window — a vertical first fill is exit liquidity, not an entry. Left = % threshold, right = window seconds.">
          <NumField testid="rule-fast-curve-pct" value={local.fast_curve_fill_pct} step="1" onChange={set("fast_curve_fill_pct")} />
          <NumField testid="rule-fast-curve-window" value={local.fast_curve_window_s} step="1" onChange={set("fast_curve_window_s")} suffix="s" />
        </Row>
        <Row label="Many buyers" hint="Lower the risk score when unique buyers ≥ X within the window (real interest → smaller risk, eligible for scalp). Left = buyer count, right = window seconds.">
          <NumField testid="rule-many-buyers" value={local.many_buyers_count} step="1" onChange={set("many_buyers_count")} />
          <NumField testid="rule-many-buyers-window" value={local.many_buyers_window_s} step="1" onChange={set("many_buyers_window_s")} suffix="s" />
        </Row>
        <Row label="Low SOL inflow" hint="SKIP if total SOL inflow into the curve is BELOW X SOL within the window. Means no real buying pressure. Left = SOL floor, right = window seconds.">
          <NumField testid="rule-low-inflow" value={local.low_inflow_sol} step="0.1" onChange={set("low_inflow_sol")} suffix="SOL" />
          <NumField testid="rule-low-inflow-window" value={local.low_inflow_window_s} step="1" onChange={set("low_inflow_window_s")} suffix="s" />
        </Row>
      </div>
      <button
        onClick={save}
        disabled={!dirty}
        data-testid="save-rules-btn"
        className="mt-4 w-full px-3 py-2 border border-blue-700 text-blue-300 bg-blue-950 hover:bg-blue-900 font-mono text-xs uppercase tracking-[0.2em] transition-colors duration-100 disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
      >
        <Save className="w-3 h-3" /> {dirty ? "Save Rules" : "Saved"}
      </button>
    </div>
  );
}

function Row({ label, children, hint }) {
  return (
    <div className="flex items-center justify-between gap-2 py-1 border-b border-neutral-900">
      <span className="text-[11px] uppercase tracking-[0.1em] text-neutral-400 inline-flex items-center gap-1">
        {label}
        {hint && <HelpHint label={label}>{hint}</HelpHint>}
      </span>
      <div className="flex items-center gap-2">{children}</div>
    </div>
  );
}

function NumField({ value, onChange, step, suffix, testid }) {
  return (
    <div className="flex items-center gap-1">
      <input
        data-testid={testid}
        type="number"
        step={step}
        value={value}
        onChange={onChange}
        className="w-20 bg-neutral-950 border border-neutral-800 px-2 py-1 font-mono text-xs text-right focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
      />
      {suffix && <span className="text-[10px] font-mono text-neutral-600">{suffix}</span>}
    </div>
  );
}
