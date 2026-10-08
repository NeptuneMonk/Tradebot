// Flush verdict at the stop (dip_forensics) → badge + one-line audit.
export function flushVerdict(t) {
  const f = t.dip_forensics;
  if (!f || typeof f !== "object") return null;
  const share = f.cohort_share != null ? `${Math.round(f.cohort_share * 100)}%` : null;
  const top = f.top_seller_share != null ? `${Math.round(f.top_seller_share * 100)}%` : "—";
  if (f.flush) {
    const kind = f.flush_kind === "cohort-unwind" ? "cohort unwind" : "single seller";
    const label = t.flush_held ? (f.hold_broke_floor ? "FLUSH·FLOOR" : "FLUSH·HELD") : "FLUSH";
    const tint = f.hold_broke_floor ? "border-amber-800 text-amber-300" : "border-lime-800 text-lime-300";
    const detail = `${kind}: ${f.n_sellers} seller(s) sold ${Number(f.sold_quote || 0).toFixed(2)} SOL` +
      (share ? ` · ${f.cohort_sellers}/${f.n_sellers} were the entry cohort (${share} of the SOL)` : ` · top seller ${top}`) +
      ` · ${f.buyers} buyer(s) still in` +
      (t.flush_held ? (f.hold_broke_floor ? ` · held from ${t.flush_hold_at_pnl_pct}% but price broke the floor` : ` · stop held from ${t.flush_hold_at_pnl_pct}%, hold budget ran out`) : "") +
      (t.flush_addon_usd ? ` · dip add-on $${Number(t.flush_addon_usd).toFixed(2)}` : "");
    return { label, tint, detail };
  }
  return { label: "DISTRIB", tint: "border-neutral-700 text-neutral-400",
           detail: `distribution: ${f.n_sellers} unrelated seller(s) sold ${Number(f.sold_quote || 0).toFixed(2)} SOL · top ${top}` +
                   (share ? ` · entry cohort only ${share}` : "") + ` · ${f.buyers} buyer(s) — sold at once` };
}

