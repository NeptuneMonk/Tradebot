const BOOK_COLORS = { scalp: "border-sky-700 text-sky-300 bg-sky-950/40", hunt: "border-rose-700 text-rose-300 bg-rose-950/40", rh_pons: "border-violet-700 text-violet-300 bg-violet-950/40" };
const DOCTOR_COLORS = { full: "text-emerald-300", half: "text-amber-300", skip: "text-red-300" };

// Trade ticket: book · R · size · cost gate · live-doctor decision · scorecard cell (persisted at entry).
export function TradeTicket({ t, compact = false }) {
  if (!t) return null;
  const book = t.book || "scalp";
  const cost = t.expected_cost_pct != null ? `${t.expected_cost_pct.toFixed(1)}%` : null;
  return (
    <span className="inline-flex flex-wrap items-center gap-1 align-middle" data-testid={`ticket-${t.id}`}>
      <span className={`px-1 py-0 border text-[9px] font-mono uppercase ${BOOK_COLORS[book] || BOOK_COLORS.scalp}`} data-testid={`ticket-book-${t.id}`}
        title={book === "hunt" ? "Hunt book — rip-cord / R ladder / trail, no clock" : book === "scalp" ? "Scalp book — single exit at +target·R or −1R, clock allowed" : "RH · PONS book"}>{book}</span>
      {t.r_usd != null && (
        <span className="text-[9px] font-mono text-neutral-400" data-testid={`ticket-r-${t.id}`}
          title={`1R = actual cash at risk after the operator cap (size × (SL ${t.sl_pct ?? "?"}% + slip)). Nominal risk (bankroll × risk%) was $${(t.r_usd_nominal ?? 0).toFixed(2)}${t.size_clamped ? " — size CLAMPED by max trade" : ""}.`}>
          R ${t.r_usd.toFixed(3)}{t.size_usd != null ? ` · $${t.size_usd.toFixed(2)}` : ""}{t.size_clamped ? " ⌈cap⌉" : ""}
        </span>
      )}
      {t.cost_gate_pass != null && (
        <span className={`text-[9px] font-mono ${t.cost_gate_pass ? "text-neutral-400" : "text-red-300"}`} data-testid={`ticket-cost-${t.id}`}
          title={`Cost gate: expected round-trip friction ${cost ?? "?"} vs first target ${t.expected_target_pct != null ? t.expected_target_pct.toFixed(1) + "%" : "?"} (needs ≥ 2× cost)`}>
          cost {cost ?? "?"}
        </span>
      )}
      {t.doctor_decision && (
        <span className={`text-[9px] font-mono ${DOCTOR_COLORS[t.doctor_decision] || "text-neutral-400"}`} data-testid={`ticket-doctor-${t.id}`}
          title={`Live doctor: winner-likeness ${t.winner_likeness_pct ?? "?"}% · exit-liquidity-likeness ${t.exit_liquidity_likeness_pct ?? "?"}% → ${t.doctor_decision}`}>
          dr {t.doctor_decision}
        </span>
      )}
      {!compact && t.scorecard_cell && (
        <span className="text-[9px] font-mono text-neutral-600" data-testid={`ticket-cell-${t.id}`} title="Scorecard cell: book × pattern × band × 4h UTC bucket × cost bucket">{t.scorecard_cell}</span>
      )}
      {t.ladder_legs_done > 0 && (
        <span className="px-1 py-0 border border-cyan-700 text-cyan-300 bg-cyan-950/40 text-[9px] font-mono" data-testid={`ticket-ladder-${t.id}`}
          title={`Hunt ladder: ${t.ladder_legs_done} leg${t.ladder_legs_done > 1 ? "s" : ""} banked · stop at ${t.ladder_stop_pct != null ? (t.ladder_stop_pct >= 0 ? "+" : "") + t.ladder_stop_pct + "%" : "breakeven+cost"}`}>
          LEG {t.ladder_legs_done}
        </span>
      )}
    </span>
  );
}
