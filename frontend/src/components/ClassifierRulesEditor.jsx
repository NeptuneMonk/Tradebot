import { useState, useEffect } from "react";
import { Sliders, Save } from "lucide-react";
import { toast } from "sonner";

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
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500 mb-3">
        <Sliders className="w-3 h-3" /> Classifier Rules
      </div>
      <div className="space-y-2 text-xs">
        <Row label="Fast curve fill (%)" tooltip="If curve fills ≥ this % in window → EXIT EARLY">
          <NumField testid="rule-fast-curve-pct" value={local.fast_curve_fill_pct} step="1" onChange={set("fast_curve_fill_pct")} />
          <NumField testid="rule-fast-curve-window" value={local.fast_curve_window_s} step="1" onChange={set("fast_curve_window_s")} suffix="s" />
        </Row>
        <Row label="Many buyers" tooltip="If unique buyers ≥ X in window → HOLD BRIEFLY">
          <NumField testid="rule-many-buyers" value={local.many_buyers_count} step="1" onChange={set("many_buyers_count")} />
          <NumField testid="rule-many-buyers-window" value={local.many_buyers_window_s} step="1" onChange={set("many_buyers_window_s")} suffix="s" />
        </Row>
        <Row label="Low SOL inflow" tooltip="If SOL inflow < X in window → ABORT TRADE">
          <NumField testid="rule-low-inflow" value={local.low_inflow_sol} step="0.1" onChange={set("low_inflow_sol")} suffix="SOL" />
          <NumField testid="rule-low-inflow-window" value={local.low_inflow_window_s} step="1" onChange={set("low_inflow_window_s")} suffix="s" />
        </Row>
        <Row label="Creator rug threshold" tooltip="If creator has ≥ X prior rugs → ABORT">
          <NumField testid="rule-rug-threshold" value={local.creator_rug_threshold} step="1" onChange={set("creator_rug_threshold")} />
        </Row>
        <Row label="Min social score" tooltip="If token name's social trending score < this → ABORT entry. Set 0 to disable.">
          <NumField testid="rule-social-min" value={local.social_score_min ?? 0} step="5" onChange={set("social_score_min")} />
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

function Row({ label, children, tooltip }) {
  return (
    <div className="flex items-center justify-between gap-2 py-1 border-b border-neutral-900" title={tooltip}>
      <span className="text-[11px] uppercase tracking-[0.1em] text-neutral-400">{label}</span>
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
