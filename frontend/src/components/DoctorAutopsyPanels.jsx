import { Stethoscope, Globe, Waves } from "lucide-react";

const signedUsd = (v) => (v == null ? "—" : `${v >= 0 ? "+" : "-"}$${Math.abs(Number(v)).toFixed(2)}`);
const label = (c) => String(c || "").replace(/_/g, " ");

const CAUSE_HINT = {
  rugged: "creator/whale dumped or the sequencer flagged a rug",
  graduation: "lost on the post-graduation pool leg",
  fee_drag: "green before fees, red after — stake too small for the venue",
  slippage: "fill landed ≥5% worse than the trigger / decision price",
  chased: "entered after a ≥60% run-in from the first price",
  stopped_then_ran: "stopped out, then the token ran ≥20% without us",
  gave_back: "was up ≥10% and finished red — trail armed too late",
  deferred_loser: "the momentum gate held a loser past its trigger",
  dead_entry: "never moved ≥3% — no-momentum/timeout exit",
  stopped: "plain stop-loss, no run afterwards",
  other: "unclassified",
  runner: "peaked ≥50% above entry",
  left_on_table: "exited ≥20% below the peak",
  clean_tp: "take-profit hit",
  trail_capture: "trailing stop locked the gain",
  small_win: "small green",
};

export function AutopsyPanel({ autopsy }) {
  const books = Object.entries(autopsy || {}).filter(([, a]) => a && a.n > 0);
  if (!books.length) return null;
  return (
    <div className="border-t border-neutral-800/50" data-testid="doctor-autopsy">
      <div className="px-2 py-1 text-[9px] uppercase tracking-[0.15em] text-neutral-600 flex items-center gap-1.5">
        <Stethoscope className="w-3 h-3" /> loss autopsy · why each book&apos;s fills lost (7d)
      </div>
      {books.map(([book, a]) => (
        <div key={book} className="px-2 py-1.5 border-t border-neutral-800/30 grid grid-cols-1 sm:grid-cols-[110px_1fr] gap-x-3 text-[10px] font-mono" data-testid={`autopsy-${book}`}>
          <div>
            <div className="text-neutral-200">{book}</div>
            <div className="text-neutral-600">{a.n_loss} losses {signedUsd(a.loss_usd)} · {a.n_win} wins {signedUsd(a.win_usd)}</div>
          </div>
          <div className="space-y-0.5">
            <div className="flex flex-wrap gap-1">
              {(a.causes || []).map((c) => (
                <span key={c.cause} className="px-1.5 py-0.5 border border-rose-900/60 text-rose-300/90" title={`${CAUSE_HINT[c.cause] || ""} · avg ${signedUsd(c.avg_usd)} · avg hold ${c.avg_hold_s}s`} data-testid={`autopsy-cause-${book}-${c.cause}`}>
                  {label(c.cause)} {c.n}× {signedUsd(c.usd)} ({Math.round((c.share || 0) * 100)}%)
                </span>
              ))}
              {(a.wins || []).slice(0, 3).map((c) => (
                <span key={c.cause} className="px-1.5 py-0.5 border border-emerald-900/60 text-emerald-300/80" title={CAUSE_HINT[c.cause] || ""}>
                  {label(c.cause)} {c.n}× {signedUsd(c.usd)}
                </span>
              ))}
            </div>
            {a.proposal && (
              <div className="text-lime-300" data-testid={`autopsy-proposal-${book}`}>→ {a.proposal.key} → {String(a.proposal.value)} · {a.proposal.reason}{a.proposal.estimated ? " (estimated)" : ""}</div>
            )}
            {(a.notes || []).map((n, i) => <div key={i} className="text-neutral-500">· {n}</div>)}
            {a.n_loss > 0 && a.post_peaks_known === 0 && <div className="text-neutral-600">· post-exit paths not yet recorded — &quot;stopped then ran&quot; detection starts with the tick store</div>}
          </div>
        </div>
      ))}
    </div>
  );
}

export function FlushPanel({ flush }) {
  if (!flush || !flush.n) return null;
  const g = (x, name) => (
    <span>
      {name}: {x.n} stops{x.n_post_known ? <> · <span className={(x.ran_share || 0) >= 0.5 ? "text-amber-300" : "text-neutral-300"}>{x.ran_after}/{x.n_post_known} ran ≥20% after we sold</span> · avg post-peak {x.avg_post_peak_pct != null ? `${x.avg_post_peak_pct >= 0 ? "+" : ""}${x.avg_post_peak_pct}%` : "—"}</> : " · post-exit paths pending"} · avg P/L {x.avg_pnl_pct}%
    </span>
  );
  return (
    <div className="border-t border-neutral-800/50" data-testid="doctor-flush">
      <div className="px-2 py-1 text-[9px] uppercase tracking-[0.15em] text-neutral-600 flex items-center gap-1.5">
        <Waves className="w-3 h-3" /> flush scorecard · stops caused by one seller vs real distribution (RH, 7d)
      </div>
      <div className="px-2 py-1.5 border-t border-neutral-800/30 text-[10px] font-mono text-neutral-400 space-y-0.5">
        <div data-testid="flush-single">{g(flush.flush, "single-seller flush")}</div>
        <div data-testid="flush-distributed">{g(flush.distributed, "distributed selling")}</div>
        <div>
          holds granted: <span className="text-neutral-200">{flush.held_n}</span>{flush.held_avg_delta_pct != null ? <> · avg {flush.held_avg_delta_pct >= 0 ? "+" : ""}{flush.held_avg_delta_pct} pts vs selling at the trigger</> : ""} · hold {flush.hold_s}s
          {flush.proposal != null && <span className="text-lime-300"> → {flush.proposal}s · {flush.note}</span>}
        </div>
      </div>
    </div>
  );
}

export function ReplayPanel({ replay, tickStore }) {
  const books = Object.entries(replay || {});
  const ts = tickStore || {};
  return (
    <div className="border-t border-neutral-800/50" data-testid="doctor-replay">
      <div className="px-2 py-1 text-[9px] uppercase tracking-[0.15em] text-neutral-600 flex items-center gap-1.5">
        <Globe className="w-3 h-3" /> universe replay · entry gates measured on every tracked token (24h)
        {ts.docs != null && <span className="text-neutral-700 normal-case tracking-normal">· tick store {ts.docs} paths · {ts.samples} samples</span>}
      </div>
      {!books.length && (
        <div className="px-2 py-1.5 text-[10px] font-mono text-neutral-600" data-testid="replay-empty">
          recording tick paths for every launch — the first replay runs on the next Doctor cycle once paths exist
        </div>
      )}
      {books.map(([book, r]) => (
        <div key={book} className="px-2 py-1.5 border-t border-neutral-800/30 grid grid-cols-1 sm:grid-cols-[110px_1fr] gap-x-3 text-[10px] font-mono" data-testid={`replay-${book}`}>
          <div>
            <div className="text-neutral-200">{book}</div>
            <div className="text-neutral-600">{r.n_tokens} tokens · ${Number(r.stake_usd).toFixed(0)} stake</div>
          </div>
          <div className="space-y-0.5 text-neutral-400">
            <div>
              current gates → <span className="text-neutral-200">{r.current.fills} fills</span> · {signedUsd(r.current.total_usd)} total · {r.current.expectancy_usd != null ? `${signedUsd(r.current.expectancy_usd)}/fill` : "no fills"}
              {r.current.winrate != null ? ` · ${Math.round(r.current.winrate * 100)}% win` : ""}
            </div>
            {r.best
              ? <div className="text-lime-300" data-testid={`replay-best-${book}`}>best: {r.best.param} → {r.best.value} = {r.best.fills} fills · {signedUsd(r.best.total_usd)} ({signedUsd(r.best.gain_total_usd)} vs now) · {signedUsd(r.best.expectancy_usd)}/fill</div>
              : <div className="text-neutral-600">no gate change beats the current set over this window</div>}
            <div>
              missed winners: <span className="text-amber-300">{r.missed_winners_n}</span> ({signedUsd(r.missed_winners_usd)} left on the table) · dodged rugs: <span className="text-emerald-300">{r.dodged_rugs_n}</span>
              {(r.missed_winners || []).slice(0, 3).map((m) => (
                <span key={m.mint} className="ml-1.5 text-neutral-500" title={`would have paid ${signedUsd(m.pnl_usd)} via ${m.via} · MFE ${m.mfe_pct}%`}>{m.symbol || m.mint.slice(0, 6)} {signedUsd(m.pnl_usd)}</span>
              ))}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
