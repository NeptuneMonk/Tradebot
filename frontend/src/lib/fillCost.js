// Fill-cost chips: projected cost of the configured size on a candidate, and the cost actually paid on a position.
export const fillTone = (pct) => (pct > 6 ? "border-red-800 text-red-300" : pct > 3 ? "border-amber-800 text-amber-300" : "border-neutral-800 text-neutral-500");

export function projectedFillTitle(fc) {
  if (!fc) return "";
  return `Buying $${fc.size_usd} here would fill ≈ ${fc.total_pct.toFixed(1)}% over mid\n` +
    `impact ${fc.impact_pct.toFixed(2)}% (your size vs the pool's SOL) · protocol fee ${fc.fee_pct.toFixed(2)}% · slip ${fc.slip_pct.toFixed(2)}%\n` +
    `A position opens this far under water before the token moves — size down or raise the liquidity floor if it is red.`;
}

export function paidFillTitle(t) {
  if (t?.fill_cost_pct == null) return "";
  const n = (v) => (v == null ? "—" : Number(v).toFixed(2));
  return `Paid ${Number(t.fill_cost_pct).toFixed(2)}% over mid at entry (${t.fill_expected === false ? "live quote, worst-case tolerance" : "paper: expected fill"})\n` +
    `impact ${n(t.fill_impact_pct)}% · fee ${n(t.fill_fee_pct)}% · slip ${n(t.fill_slip_pct)}%` +
    (t.spot_price_sol_at_entry ? `\nmid at entry ${Number(t.spot_price_sol_at_entry).toExponential(3)} SOL` : "");
}
