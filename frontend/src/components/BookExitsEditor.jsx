import { Layers, RotateCcw } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import HelpHint from "./HelpHint";

// Defaults mirror backend/book_params.BOOK_DEFAULTS — the ONLY exit parameters; there are no global TP/SL keys.
export const BOOK_DEFAULTS = {
  scalp: { stop_loss_pct: 12, target_r: 1.5, trailing_stop_pct: 6, trailing_arm_pct: 12, hold_max_seconds: 40, ladder_1r_sell_pct: 0, ladder_2r_sell_pct: 0 },
  hunt: { stop_loss_pct: 20, target_r: 2.0, trailing_stop_pct: 8, trailing_arm_pct: 0, hold_max_seconds: 0, ladder_1r_sell_pct: 35, ladder_2r_sell_pct: 30 },
  rh_pons: { stop_loss_pct: 12, target_r: 0, take_profit_pct: 20, trailing_stop_pct: 6, trailing_arm_pct: 12, hold_max_seconds: 35 },
};
const BOOK_META = {
  scalp: { label: "Scalp", hint: "momentum + manual · single exit at +target·R or −1R · trail once armed · clock allowed" },
  hunt: { label: "Hunt", hint: "greylist snipe + re-entry · pattern rip-cord first · +1R sell leg 1 & stop → breakeven+cost · +2R sell leg 2 · trail the runner · NO clock (0 = disabled)" },
  rh_pons: { label: "RH · PONS", hint: "Robinhood Chain curves · own exits, never shares Solana values · cost gate + R sizing apply" },
};
const FIELDS = [
  ["stop_loss_pct", "SL %", "1R in price terms (plus expected exit slip). R sizing derives size from this."],
  ["target_r", "Target R", "Scalp: full exit at +target·R. Hunt: cost gate uses +1R for the first leg; runner trails. RH: 0 = use TP %."],
  ["take_profit_pct", "TP %", "RH only — legacy % target used when Target R is 0."],
  ["trailing_stop_pct", "Trail %", "Give-back from peak that closes the runner."],
  ["trailing_arm_pct", "Arm %", "Peak gain required before the trail arms (hunt arms after leg 1 regardless)."],
  ["hold_max_seconds", "Clock s", "Scalp / RH clock stop. Hunt must stay 0 — a hunt is never killed by a timer."],
  ["ladder_1r_sell_pct", "+1R sell %", "Hunt ladder leg 1 size."],
  ["ladder_2r_sell_pct", "+2R sell %", "Hunt ladder leg 2 size."],
];

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
                  const na = dflt === undefined || (book !== "rh_pons" && k === "take_profit_pct") || (book !== "hunt" && k.startsWith("ladder"));
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
