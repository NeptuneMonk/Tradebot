import { Layers, RotateCcw } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import HelpHint from "./HelpHint";

// Defaults mirror backend/book_params.BOOK_DEFAULTS — the ONLY exit parameters; there are no global TP/SL keys.
export const BOOK_DEFAULTS = {
  scalp: { stop_loss_pct: 12, target_r: 1.5, take_profit_pct: 0, trailing_stop_pct: 6, trailing_arm_pct: 12, hold_max_seconds: 40, ladder_1r_sell_pct: 0, ladder_2r_sell_pct: 0 },
  hunt: { stop_loss_pct: 20, target_r: 2.0, take_profit_pct: 0, trailing_stop_pct: 8, trailing_arm_pct: 0, hold_max_seconds: 0, ladder_1r_sell_pct: 35, ladder_2r_sell_pct: 30 },
  rh_pons: { stop_loss_pct: 12, target_r: 0, take_profit_pct: 20, trailing_stop_pct: 6, trailing_arm_pct: 12, hold_max_seconds: 35 },
  runner: { stop_loss_pct: 25, target_r: 0, take_profit_pct: 0, trailing_stop_pct: 15, trailing_arm_pct: 0, hold_max_seconds: 0, ladder_1r_sell_pct: 0, ladder_2r_sell_pct: 0 },
};
const BOOK_META = {
  scalp: { label: "Scalp", hint: "momentum + manual · exits: SL (−1R) · TP % · +target·R · trail once armed at Arm % · clock (0 = off) · manual holds: target R only" },
  hunt: { label: "Hunt", hint: "greylist snipe + re-entry · pattern rip-cord first · +1R / +2R sell legs (stop → breakeven+cost after leg 1) · trail (arms after leg 1 or at Arm %) · TP % · target R (only when both legs are 0) · clock (0 = off)" },
  rh_pons: { label: "RH · PONS", hint: "Robinhood Chain curves · own exits, never shares Solana values · cost gate + R sizing apply" },
  runner: { label: "Runner", hint: "promoted winners · every % measured from the PROMOTION price · giveback trail (Trail %, armed at Arm % or +1R when 0) · SL · TP % · target R · +3R chip 25% · exhausted after dead flow · clock since entry (0 = off) · cap 1" },
};
const FIELDS = [
  ["stop_loss_pct", "SL %", "1R in price terms (plus expected exit slip). R sizing derives size from this."],
  ["target_r", "Target R", "Full exit at +target·R (1R = SL + slip). Scalp / runner (from promotion) / hunt when both ladder legs are 0. RH: 0 = use TP %. 0 = off."],
  ["take_profit_pct", "TP %", "Fixed % exit on every book — fires before the R target (runner: % from promotion). 0 = off."],
  ["trailing_stop_pct", "Trail %", "Give-back from peak that closes the position (runner: peak since promotion). Used exactly as set unless the trail ratchet switch is on."],
  ["trailing_arm_pct", "Arm %", "Peak gain (from entry; runner: from promotion) before the trail arms. Hunt also arms after leg 1. Runner 0 = arm at +1R."],
  ["hold_max_seconds", "Clock s", "Max hold since entry, every book. 0 = no clock. Manual / LTH holds ignore it."],
  ["ladder_1r_sell_pct", "+1R sell %", "Hunt ladder leg 1 size."],
  ["ladder_2r_sell_pct", "+2R sell %", "Hunt ladder leg 2 size."],
];

const PROMO_FIELDS = [
  ["runner_promo_min_r", "min R", 0.25, 1.0, "Open PnL (realized + unrealized) in R the scalp / hunt must show before it may become a runner. Default 1."],
  ["runner_promo_min_mfe_r", "min MFE R", 0.25, 1.5, "Peak since entry, in R, the position must have reached. Default 1.5."],
  ["runner_promo_max_exit_liq_pct", "max exit-liq %", 5, 70, "Live Doctor exit-liquidity likeness must stay BELOW this (higher = more permissive). Default 70."],
  ["runner_promo_max_exit_cost_pct", "max exit cost %", 1, 8, "Cost to flatten the remainder (depth, slippage, fees) must stay below this. Default 8."],
  ["runner_cap", "cap", 1, 1, "Runner slots open at once on the Solana book. While a runner is open the hunt cap drops to 1. Default 1."],
];

function RunnerPromotion({ local, setLocal }) {
  const set = (k, v) => setLocal({ ...local, [k]: v });
  return (
    <div className="mb-3 p-2 border border-neutral-800/80 bg-neutral-950/40" data-testid="runner-promotion">
      <div className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 mb-1.5 flex items-center gap-1.5">
        Runner promotion
        <HelpHint label="help: runner promotion">Nothing opens as a runner cold. A live scalp (when its target exit would fire — or any time it is ≥ min R with the switch below) or a hunt (after its +1R leg) is promoted when ALL hold: PnL ≥ min R · peak ≥ min MFE R · flow still expanding (buyers and inflow above entry) · exit-liquidity likeness below max · exit cost below max · a runner slot is free. A promoted scalp banks 45% first; the remainder rides on the Runner row's exits.</HelpHint>
      </div>
      <div className="flex flex-wrap items-end gap-x-3 gap-y-2 text-[10px] font-mono">
        {PROMO_FIELDS.map(([k, label, step, dflt, hint]) => {
          const cur = local[k];
          return (
            <label key={k} className="flex flex-col gap-0.5 text-neutral-500 uppercase tracking-[0.08em]" title={hint}>
              {label}
              <input type="number" step={step} min="0" value={cur ?? dflt} data-testid={`${k.replace(/_/g, "-")}-input`}
                onChange={(e) => set(k, k === "runner_cap" ? Math.max(0, parseInt(e.target.value, 10) || 0) : Math.max(0, parseFloat(e.target.value) || 0))}
                className={`w-16 bg-neutral-950 border px-1 py-0.5 text-right ${cur != null && cur !== dflt ? "border-amber-700 text-amber-200" : "border-neutral-800 text-neutral-300"}`} />
            </label>
          );
        })}
        <label className="flex items-center gap-1.5 text-neutral-400 uppercase tracking-[0.08em] pb-1"
          title="OFF: a scalp is only examined for promotion at the instant its target exit would fire. ON: examined every 2 s whenever it is ≥ min R, so a token that blows through the target can still be caught.">
          <input type="checkbox" data-testid="runner-scalp-promo-anytime-checkbox" checked={!!local.runner_scalp_promo_anytime}
            onChange={(e) => set("runner_scalp_promo_anytime", e.target.checked)} />
          scalp: check any time ≥ min R
        </label>
      </div>
    </div>
  );
}

export default function BookExitsEditor({ local, setLocal, onRestored }) {
  const bx = local.book_exits || {};
  const set = (book, key, v) => setLocal({ ...local, book_exits: { ...bx, [book]: { ...(bx[book] || {}), [key]: v } } });
  const drifted = Object.keys(BOOK_DEFAULTS).some((b) => Object.entries(bx[b] || {}).some(([k, v]) => BOOK_DEFAULTS[b][k] !== undefined && v !== BOOK_DEFAULTS[b][k]));
  const restore = async () => {
    if (!window.confirm("Restore every book's exits to the spec defaults? Unsaved edits to exits are discarded.")) return;
    try {
      const r = await api.restoreBookExits();
      onRestored?.(r.book_exits);
      toast.success("Book exits restored to defaults");
    } catch (e) {
      toast.error("Restore failed");
    }
  };
  return (
    <div className="border-t border-neutral-800 pt-3 mt-1" data-testid="book-exits-editor">
      <div className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 mb-2 flex items-center gap-1.5">
        <Layers className="w-3 h-3" /> Exits · per book
        <HelpHint label="help: per-book exits">Every exit parameter lives on its book. Nothing is shared: a hunt bag is never clipped by the scalp clock, and a scalp never inherits the hunt ladder. Sizes come from R (bankroll × risk % ÷ SL) inside the operator cap.</HelpHint>
        {drifted && <span className="ml-1 px-1.5 py-0.5 border border-amber-800 text-amber-300 text-[9px] tracking-[0.1em]" data-testid="book-exits-drifted">drifted from defaults</span>}
        <button type="button" onClick={restore} data-testid="book-exits-restore-btn"
          className="ml-auto inline-flex items-center gap-1 px-2 py-0.5 border border-neutral-700 text-neutral-300 hover:bg-neutral-900 text-[9px] uppercase tracking-[0.15em]">
          <RotateCcw className="w-3 h-3" /> Restore book defaults
        </button>
      </div>
      <RunnerPromotion local={local} setLocal={setLocal} />
      <div className="overflow-x-auto">
        <table className="w-full text-[10px] font-mono">
          <thead><tr className="text-neutral-500 uppercase tracking-[0.1em]"><th className="text-left py-1">Book</th>
            {FIELDS.map(([k, l, h]) => <th key={k} className="text-right px-1" title={h}>{l}</th>)}</tr></thead>
          <tbody>
            {Object.keys(BOOK_META).map((book) => (
              <tr key={book} className="border-t border-neutral-800/60" data-testid={`book-exits-row-${book}`}>
                <td className="py-1 text-neutral-200" title={BOOK_META[book].hint}>{BOOK_META[book].label}</td>
                {FIELDS.map(([k]) => {
                  const dflt = BOOK_DEFAULTS[book][k];
                  const na = dflt === undefined || (book !== "hunt" && k.startsWith("ladder"));
                  const cur = (bx[book] || {})[k];
                  return (
                    <td key={k} className="text-right px-1">
                      {na ? <span className="text-neutral-700">—</span> : (
                        <input type="number" step={k === "target_r" ? 0.25 : 1} data-testid={`book-exit-${book}-${k}`}
                          value={cur ?? dflt}
                          onChange={(e) => set(book, k, parseFloat(e.target.value) || 0)}
                          className={`w-14 bg-neutral-950 border px-1 py-0.5 text-right ${cur != null && cur !== dflt ? "border-amber-700 text-amber-200" : "border-neutral-800 text-neutral-300"}`} />
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
