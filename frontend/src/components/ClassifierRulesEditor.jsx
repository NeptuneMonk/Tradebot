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
          Real-time entry and mid-trade abort rules for <b>momentum_new</b> trades only. These do <b>NOT</b> affect Greylist Sniper buys, re-entries, or seasoned-momentum trades — the sniper has its own pattern-based exit ladder (Profit Ripcord, Curve-fill, Peak-MC, etc.).
          <br /><br />
          When a momentum_new candidate is being tracked, the bot re-classifies every 2s. If the live metrics breach any of these thresholds the verdict flips to <b>abort_trade</b> and the position exits immediately.
        </HelpHint>
      </div>
      <div className="text-[10px] text-neutral-600 font-mono mb-3 tracking-wide">
        affects: momentum_new only — sniper / reentry bypass these gates
      </div>
      <div className="space-y-2 text-xs">
        <Row label="Fast curve fill (%)" hint="ABORT EARLY if the bonding curve fills ≥ X% within the window. Left input = % threshold, right input = window seconds. Default: 25% in 30s → looks pumped, exit before the rug.">
          <NumField testid="rule-fast-curve-pct" value={local.fast_curve_fill_pct} step="1" onChange={set("fast_curve_fill_pct")} />
          <NumField testid="rule-fast-curve-window" value={local.fast_curve_window_s} step="1" onChange={set("fast_curve_window_s")} suffix="s" />
        </Row>
        <Row label="Many buyers" hint="HOLD BRIEFLY (give it room) if unique buyers ≥ X within the window. Signals organic interest. Left = buyer count, right = window seconds.">
          <NumField testid="rule-many-buyers" value={local.many_buyers_count} step="1" onChange={set("many_buyers_count")} />
          <NumField testid="rule-many-buyers-window" value={local.many_buyers_window_s} step="1" onChange={set("many_buyers_window_s")} suffix="s" />
        </Row>
        <Row label="Low SOL inflow" hint="ABORT if total SOL inflow into the curve is BELOW X SOL within the window. Means no real buying pressure. Left = SOL floor, right = window seconds.">
          <NumField testid="rule-low-inflow" value={local.low_inflow_sol} step="0.1" onChange={set("low_inflow_sol")} suffix="SOL" />
          <NumField testid="rule-low-inflow-window" value={local.low_inflow_window_s} step="1" onChange={set("low_inflow_window_s")} suffix="s" />
        </Row>
        <Row label="Creator rug threshold" hint="ABORT if the creator has ≥ X prior rugged tokens. This is a coarse pre-greylist filter; the Greylist Sniper has more nuanced scoring. Set 0 to disable.">
          <NumField testid="rule-rug-threshold" value={local.creator_rug_threshold} step="1" onChange={set("creator_rug_threshold")} />
        </Row>
        <Row label="Min social score" hint="ABORT entry if the token's social trending score is below this floor. Currently computed from token-name keyword heuristics. Set 0 to disable.">
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
