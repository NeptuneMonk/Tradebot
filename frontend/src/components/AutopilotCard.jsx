import { useCallback, useEffect, useState, memo } from "react";
import { Bot, ShieldAlert, FlaskConical, Wallet, TrendingUp, Clock } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import HelpHint from "./HelpHint";
import ProfitSweepPanel from "./ProfitSweepPanel";
import { AutopsyPanel, ReplayPanel, FlushPanel } from "./DoctorAutopsyPanels";

const NON_BOOK_KEYS = new Set(["ride", "autopsy", "replay", "tick_store", "computed_at", "flush"]);

const usd = (v, d = 2) => (v == null ? "—" : `${v < 0 ? "-" : ""}$${Math.abs(Number(v)).toFixed(d)}`);
const signedUsd = (v) => (v == null ? "—" : `${v >= 0 ? "+" : "-"}$${Math.abs(Number(v)).toFixed(2)}`);
const signedR = (v) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(2)}R`);
const fmtWhen = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—");

export function AutopilotSwitch({ enabled, onChange }) {
  const [busy, setBusy] = useState(false);
  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await api.autopilotSet(!enabled);
      toast.success(r.autopilot_enabled
        ? "Autopilot ON — Doctor is driving (learning + auto-apply + bankroll sizing)"
        : "Autopilot OFF — Doctor back to advisory, sizing is yours");
      onChange && onChange(r.autopilot_enabled);
    } catch (e) {
      toast.error(`Autopilot toggle failed: ${e?.response?.data?.detail || e.message}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <button
      type="button"
      onClick={toggle}
      disabled={busy}
      data-testid="autopilot-toggle"
      title={enabled ? "Autopilot ON — click to hand control back" : "Autopilot OFF — click to let the Doctor drive"}
      className={`px-2.5 py-1 border text-[11px] font-mono uppercase tracking-wider transition-colors duration-100 inline-flex items-center gap-1.5 disabled:opacity-50 ${
        enabled ? "border-lime-600/70 text-lime-300 bg-lime-950/40 hover:bg-lime-900/40" : "border-neutral-700 text-neutral-400 hover:bg-neutral-800"
      }`}
    >
      <Bot className={`w-3 h-3 ${enabled ? "animate-pulse" : ""}`} />
      {enabled ? "autopilot" : "autopilot off"}
    </button>
  );
}

function Stat({ label, value, tone = "text-neutral-200", testid }) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-[0.15em] text-neutral-600">{label}</div>
      <div className={`font-mono text-sm ${tone}`} data-testid={testid}>{value}</div>
    </div>
  );
}

function AutopilotCard({ config, onConfigUpdate }) {
  const [s, setS] = useState(null);
  const load = useCallback(async () => {
    try { setS(await api.autopilotStatus()); } catch { /* best effort */ }
  }, []);
  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load, config?.autopilot_enabled, config?.max_trade_usd]);

  if (!s) return null;
  const b = s.bankroll || {};
  const sol = b.chains?.sol || {};
  const rh = b.chains?.rh || {};
  const canary = s.canary;
  const running = canary?.state === "running";
  const books = s.books || {};
  const gov = b.governor_active;

  const setRisk = async (patch) => {
    try {
      const upd = await api.updateConfig(patch);
      onConfigUpdate && onConfigUpdate(upd);
      toast.success("Risk settings saved — sizing recomputes within 60s");
      load();
    } catch (e) {
      toast.error(`Save failed: ${e?.response?.data?.detail || e.message}`);
    }
  };

  return (
    <div className={`control-card border ${s.driving ? "border-lime-800/60" : "border-neutral-800"}`} data-testid="autopilot-card">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-400">
          <Bot className={`w-3.5 h-3.5 ${s.driving ? "text-lime-300" : ""}`} /> Autopilot
          <HelpHint label="Autopilot">
            One switch: Doctor learning + auto-apply (paper AND live) + bankroll sizing. Each chain has ITS OWN bankroll — Solana from the SOL wallet (live) or its paper pool, Robinhood from the ETH wallet (live) or its paper pool — never mixed. Stake = that chain&apos;s bankroll × risk%, kill switch = bankroll × loss limit, recomputed every 60s. Robinhood stakes are also lifted to the fee floor (gas ≤ drag %) or the chain sits out when the bankroll can&apos;t fund it. The Doctor works TECHNIQUE FIRST: it replays each book&apos;s own fills against a TP/SL/hold grid and splits them by entry feature, proposing the measured best setting as a canary (per-book exits, so RH never moves Solana). Disabling a book or cutting risk% is the last resort, only on 3× the sample when no technique change helps.
          </HelpHint>
        </div>
        <span className={`text-[10px] font-mono uppercase tracking-wider px-2 py-0.5 border ${s.driving ? "border-lime-700 text-lime-300 bg-lime-950/40" : "border-neutral-800 text-neutral-500"}`} data-testid="autopilot-driving-badge">
          {s.driving ? "doctor is driving" : s.autopilot_enabled ? "armed · waiting" : "manual"}
        </span>
      </div>

      {s.driving && !s.bot_enabled && (
        <div className="mt-2 text-[10px] font-mono text-amber-300/90 border border-amber-900/60 bg-amber-950/20 px-2 py-1" data-testid="autopilot-bot-stopped">
          bot is STOPPED — autopilot sizes and tunes, but nothing trades until you press Start
        </div>
      )}
      {gov && (
        <div className="mt-2 flex items-center justify-between gap-2 text-[10px] font-mono text-rose-300 border border-rose-900/60 bg-rose-950/20 px-2 py-1" data-testid="autopilot-governor">
          <span className="inline-flex items-center gap-1.5"><ShieldAlert className="w-3 h-3" /> governor: {b.governor_reason} — that chain trades at {Math.round((b.governor_size_mult || 0.5) * 100)}% size until {fmtWhen(b.governor_until)}</span>
          <button type="button" onClick={async () => {
            try {
              const snap = await api.autopilotReleaseGovernor();
              const still = Object.entries(snap?.chains || {}).filter(([, c]) => c.governor_active).map(([k]) => k.toUpperCase());
              still.length ? toast.error(`Governor re-armed on ${still.join(", ")} — drawdown deepened past another step`)
                : toast.success("Governor released — sizing back to 100%, held for the governor window unless the drawdown deepens by another step");
            } catch (e) { toast.error(`Release failed: ${e?.response?.data?.detail || e.message}`); }
            load();
          }} className="px-1.5 border border-rose-800 hover:bg-rose-900/40 uppercase" data-testid="autopilot-governor-release">release</button>
        </div>
      )}

      <div className="mt-3 space-y-1.5" data-testid="autopilot-chains">
        {[["sol", "Solana", sol, `${usd(s.sizing?.max_trade_usd)} · ${s.sizing?.max_concurrent_positions} · ${usd(s.sizing?.daily_kill_switch_usd, 0)}`],
          ["rh", "Robinhood", rh, `${usd(s.sizing?.rh_max_trade_usd)} · ${s.sizing?.rh_max_positions} · ${usd(s.sizing?.rh_daily_kill_switch_usd, 0)}`]].map(([key, name, c, sizing]) => (
          <div key={key} className={`grid grid-cols-2 sm:grid-cols-5 gap-3 px-2 py-1.5 border ${c.governor_active ? "border-rose-900/60 bg-rose-950/10" : "border-neutral-800/70"}`} data-testid={`autopilot-chain-${key}`}>
            <div>
              <div className="text-[9px] uppercase tracking-[0.15em] text-neutral-600">{name} · {c.mode || "—"}</div>
              <div className="text-xs font-mono text-neutral-200">{c.bankroll_source || "—"}</div>
            </div>
            <Stat label="bankroll" value={usd(c.bankroll_usd)} testid={`autopilot-bankroll-${key}`} />
            <Stat label="24h P/L" value={signedUsd(c.pnl_24h_usd)} tone={(c.pnl_24h_usd ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300"} testid={`autopilot-pnl-${key}`} />
            <Stat label="24h drawdown" value={`${Number(c.drawdown_24h_pct ?? 0).toFixed(1)}%`} tone={(c.drawdown_24h_pct ?? 0) <= -(s.risk?.governor_drawdown_pct ?? 5) ? "text-rose-300" : "text-neutral-200"} />
            <Stat label="stake · cap · kill" value={sizing} testid={`autopilot-sizing-${key}`} />
          </div>
        ))}
        {rh.fee_floor && (
          <div className="text-[10px] font-mono text-neutral-500 px-2" data-testid="autopilot-rh-fee-floor">
            RH fee floor: gas ≈ {usd(rh.fee_floor.gas_round_trip_usd)} per round trip{rh.fee_floor.samples ? ` (median of ${rh.fee_floor.samples} live fills)` : " (estimate)"} + {rh.fee_floor.curve_fee_round_trip_pct}% curve fee → min viable stake <span className="text-neutral-200">{usd(rh.fee_floor.min_stake_usd)}</span> so gas stays ≤ {rh.fee_floor.max_gas_drag_pct}% (break-even ≈ +{rh.fee_floor.break_even_pct_at_min_stake}%)
            {rh.sitting_out && <span className="text-amber-300"> · RH bankroll too small for the floor — sitting out</span>}
          </div>
        )}
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2 text-[10px] font-mono">
        {[["risk / trade %", "risk_per_trade_pct", 0.1], ["max exposure %", "max_exposure_pct", 1], ["daily loss %", "daily_loss_limit_pct", 1]].map(([label, key, step]) => (
          <label key={key} className="block">
            <span className="text-[9px] uppercase tracking-[0.15em] text-neutral-600">{label}</span>
            <input
              type="number" step={step} min="0.1"
              defaultValue={config?.[key]}
              key={`${key}-${config?.[key]}`}
              onBlur={(e) => { const v = parseFloat(e.target.value); if (!Number.isNaN(v) && v !== config?.[key]) setRisk({ [key]: v }); }}
              onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
              className="mt-0.5 w-full px-2 py-1 bg-neutral-950 border border-neutral-800 text-neutral-200 tabular-nums"
              data-testid={`autopilot-${key}-input`}
            />
          </label>
        ))}
      </div>

      {s.search_ledger && (
        <div className="mt-3 text-[9px] font-mono text-neutral-400 flex flex-wrap gap-x-3 gap-y-0.5" data-testid="search-ledger"
          title={`7-day search vs harvest ledger (display only — no gate yet)\nseed $${s.search_ledger.seed_usd} + ${Math.round((s.search_ledger.search_budget_pct || 0) * 100)}% of harvest − |search losses| = remaining\nsearch = scalp/hunt/rh_pons closes that never became runners; harvest = runner closes + banked promotion proceeds`}>
          <span>cost/runner <span className={s.search_ledger.cost_per_runner_usd == null ? "text-neutral-500" : "text-amber-300"}>
            {s.search_ledger.cost_per_runner_usd == null ? "— (no runners yet)" : `$${s.search_ledger.cost_per_runner_usd.toFixed(2)}`}</span></span>
          <span>harvest <span className={s.search_ledger.harvest_realised_usd >= 0 ? "text-emerald-300" : "text-red-300"}>${s.search_ledger.harvest_realised_usd.toFixed(2)}</span> / {s.search_ledger.harvest_fills} fills</span>
          <span>search <span className={s.search_ledger.search_realised_usd >= 0 ? "text-emerald-300" : "text-red-300"}>${s.search_ledger.search_realised_usd.toFixed(2)}</span> / {s.search_ledger.search_fills} fills</span>
          <span>budget left <span className={s.search_ledger.remaining_usd > 0 ? "text-neutral-200" : "text-red-300"}>${s.search_ledger.remaining_usd.toFixed(2)}</span></span>
          {s.regime && <span>regime <span className={s.regime.regime === "dead" ? "text-red-300" : s.regime.regime === "hot" ? "text-amber-300" : "text-neutral-200"} data-testid="market-regime">{s.regime.regime}</span> ({s.regime.launch_rate_per_h}/h · hot {Math.round(s.regime.hot_share * 100)}% · SOL {s.regime.sol_1h_sign > 0 ? "↑" : s.regime.sol_1h_sign < 0 ? "↓" : "→"})</span>}
        </div>
      )}
      <div className="mt-3 flex flex-wrap gap-1.5 text-[9px] font-mono" data-testid="autopilot-books">
        {Object.entries(books).map(([k, v]) => (
          <span key={k} className={`px-1.5 py-0.5 border ${v >= 1 ? "border-neutral-700 text-neutral-300" : v > 0 ? "border-amber-800 text-amber-300" : "border-rose-900 text-rose-400 line-through"}`} data-testid={`book-chip-${k}`}>
            {k} ×{Number(v).toFixed(2)}
          </span>
        ))}
        {b.governor_size_mult && b.governor_size_mult < 1 && <span className="px-1.5 py-0.5 border border-rose-900 text-rose-300">governor ×{b.governor_size_mult}</span>}
      </div>
      {s.allocator && (
        <div className="mt-2 border border-neutral-800/70 p-2 space-y-1" data-testid="desk-allocator">
          <div className="flex items-center justify-between text-[9px] uppercase tracking-[0.15em] text-neutral-600">
            <span className="inline-flex items-center gap-1">desk allocator · capital per book
              <HelpHint label="help: desk allocator">Every Doctor cycle each book's size multiplier moves one step (±0.25) toward a target set by its rolling expectancy in R per fill (full ×2 at ≥ +0.30R): losing books shrink to an exploration floor of ×0.25 — never 0, so they keep producing fills and can earn their way back — and paying books scale up to ×2. Replaces the old &quot;disable the losing machine&quot; switch. Runs only while Autopilot drives.</HelpHint>
            </span>
            <span className={s.allocator.driving && s.allocator.enabled ? "text-lime-400" : "text-neutral-500"} data-testid="allocator-state">
              {!s.allocator.enabled ? "off" : s.allocator.driving ? "driving" : "advisory (autopilot off)"} · floor ×{s.allocator.floor} · cap ×{s.allocator.cap}
            </span>
          </div>
          {(s.allocator.rows || []).map((r) => (
            <div key={r.book} className="flex items-center justify-between gap-2 text-[10px] font-mono" data-testid={`allocator-row-${r.book}`}>
              <span className="text-neutral-400 truncate" title={r.reason}>
                <span className="text-neutral-200">{r.book}</span> ×{r.current} → ×{r.target}{r.change ? <span className="text-amber-300"> (next ×{r.next})</span> : ""} · {r.reason}
              </span>
              {r.current < 1 && (
                <button type="button" onClick={() => setRisk({ [r.key]: 1.0 })} data-testid={`allocator-restore-${r.book}`}
                        className="px-1.5 border border-neutral-700 hover:border-lime-600 hover:text-lime-300 uppercase text-[9px] whitespace-nowrap">restore ×1</button>
              )}
            </div>
          ))}
          {Object.entries(books).some(([, v]) => v > 0 && v < 1) && !s.allocator.driving && (
            <div className="text-[10px] font-mono text-amber-300" data-testid="allocator-reduced-note">a book is running below ×1 — the Doctor reduced it; restore above or let Autopilot drive the allocator</div>
          )}
          <div className="text-[9px] font-mono text-neutral-600 pt-1 border-t border-neutral-800/50" data-testid="immutable-rails">
            immutable rails (code, never learned): book size ×0.25–×2 · SL 5–40% · TP 8–200% · trail 2–25% · hold 20s–1h · slippage 1–15% · flush hold ≤30s · kill switches, live toggles, max stake and gas reserve are never touched · ≤6 changes/day
          </div>
        </div>
      )}

      {s.technique && Object.keys(s.technique).length > 0 && (
        <div className="mt-3 border border-neutral-800/70" data-testid="autopilot-technique">
          <div className="px-2 py-1 text-[9px] uppercase tracking-[0.15em] text-neutral-600 flex items-center gap-1.5">
            <FlaskConical className="w-3 h-3" /> technique lab · what each book&apos;s own fills say (7d)
          </div>
          {s.technique.ride && (
            <div className="px-2 py-1 border-t border-neutral-800/50 text-[10px] font-mono text-neutral-400" data-testid="technique-ride">
              ride scorecard: {s.technique.ride.n} rode-winner exits · <span className={s.technique.ride.gain_vs_clock_usd_per_ride >= 0 ? "text-lime-300" : "text-rose-300"}>{signedUsd(s.technique.ride.gain_vs_clock_usd_per_ride)}/ride vs the clock</span> · {s.technique.ride.rides_that_beat_clock}/{s.technique.ride.n} beat it · ride threshold {s.technique.ride.current_min_pnl_pct}%{s.technique.ride.proposal != null ? ` → ${s.technique.ride.proposal}%` : ""}
            </div>
          )}
          {Object.entries(s.technique).filter(([k]) => !NON_BOOK_KEYS.has(k)).map(([book, t]) => {
            const ex = t.current_exits || {};
            const wi = t.whatif || {};
            const best = wi.best;
            const gain = wi.gain_r_per_fill ?? 0;
            const split = (t.splits || []).find((x) => x.actionable) || (t.splits || [])[0];
            const pair = (t.pairs || []).find((x) => x.actionable) || (t.pairs || [])[0];
            return (
              <div key={book} className="px-2 py-1.5 border-t border-neutral-800/50 grid grid-cols-1 sm:grid-cols-[110px_1fr_1fr] gap-x-3 gap-y-0.5 text-[10px] font-mono" data-testid={`technique-${book}`}>
                <div>
                  <div className="text-neutral-200">{book}</div>
                  <div className="text-neutral-600">n={t.n} · SL {ex.stop_loss_pct}% · target {ex.target_r ? `${ex.target_r}R` : `${ex.take_profit_pct}%`} · trail {ex.trailing_stop_pct}% @ {ex.trailing_arm_pct}% · {ex.hold_max_seconds > 0 ? `clock ${ex.hold_max_seconds}s` : "no clock"}</div>
                </div>
                <div className="text-neutral-400">
                  {wi.n >= 1 && best
                    ? <>exits: now <span className="text-neutral-200">{signedR(wi.current?.expectancy_r)}</span>/fill → best <span className={gain > 0.02 ? "text-lime-300" : "text-neutral-300"}>{best.param} {best.value}</span> = {signedR(best.expectancy_r)}/fill ({gain >= 0 ? "+" : ""}{Number(gain).toFixed(3)}) · MFE {Number(wi.median_mfe).toFixed(0)}% / MAE {Number(wi.median_mae).toFixed(0)}%{wi.mae_recorded ? "" : " · trough not yet recorded — SL what-ifs pending"}{wi.peak_timing_recorded ? "" : " · peak timing pending — hold what-ifs pending"}</>
                    : <span className="text-neutral-600">exits: need {Math.max(0, (config?.doctor_learning_min_trades_per_book ?? 15) - (t.n || 0))} more fills</span>}
                </div>
                <div className="text-neutral-400">
                  {split
                    ? <>entry: {split.feature} &lt; {Number(split.split).toFixed(split.split > 100 ? 0 : 1)} → {signedR(split.low_expectancy_r)}/fill, above → <span className={split.actionable ? "text-lime-300" : "text-neutral-300"}>{signedR(split.high_expectancy_r)}</span>{split.actionable ? ` · raise ${split.key} ${split.current} → ${Number(split.split).toFixed(0)}` : " · no clean split"}</>
                    : <span className="text-neutral-600">entry: no feature splits yet (entry context recorded from now on)</span>}
                  {t.regime_exits && Object.entries(t.regime_exits).map(([reg, rw]) => rw.best && (
                    <div key={reg} data-testid={`technique-regime-exits-${book}-${reg}`}>
                      {reg}-hour exits (n={rw.n}): now {signedR(rw.current?.expectancy_r)}/fill → best <span className={(rw.gain_r_per_fill ?? 0) > 0.02 ? "text-lime-300" : "text-neutral-300"}>{rw.best.param} {rw.best.value}</span> = {signedR(rw.best.expectancy_r)}/fill
                    </div>
                  ))}
                  {t.regime && (
                    <div data-testid={`technique-regime-${book}`}>
                      regime: quiet (&lt;{Number(t.regime.threshold_per_h).toFixed(0)}/h) {signedR(t.regime.quiet.expectancy_r)}/fill (n={t.regime.quiet.n}) · busy {signedR(t.regime.busy.expectancy_r)}/fill (n={t.regime.busy.n}){t.regime.actionable ? <span className="text-lime-300"> · tighten {t.regime.losing}-hour gates ×{(t.regime.current_mult + 0.5).toFixed(1)}</span> : t.regime.current_mult > 1 ? ` · ${t.regime.losing} gates ×${t.regime.current_mult}` : ""}
                    </div>
                  )}
                  {pair && (
                    <div data-testid={`technique-pair-${book}`}>
                      pair: {pair.features[0]} ≥ {Number(pair.splits[0]).toFixed(0)} AND {pair.features[1]} ≥ {Number(pair.splits[1]).toFixed(0)} → <span className={pair.actionable ? "text-lime-300" : "text-neutral-300"}>{signedR(pair.high_high_expectancy_r)}</span>/fill (n={pair.high_high_n}), rest {signedR(pair.rest_expectancy_r)}{pair.actionable ? " · raise both gates" : ""}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
          <AutopsyPanel autopsy={s.technique.autopsy} />
          <FlushPanel flush={s.technique.flush} />
          <ReplayPanel replay={s.technique.replay} tickStore={s.technique.tick_store} />
        </div>
      )}

      <div className="mt-3 space-y-1 text-[10px] font-mono text-neutral-400">
        <div className="flex items-center gap-1.5" data-testid="autopilot-canary">
          <FlaskConical className="w-3 h-3 text-neutral-500" />
          {running
            ? <>canary in flight: <span className="text-lime-300">{canary.proposal?.key}={String(canary.proposal?.value)}</span> on {canary.book} since {new Date(canary.started_at).toLocaleTimeString()}</>
            : s.proposal
              ? <>proposal ready: <span className="text-lime-300">{s.proposal.key} → {String(s.proposal.value)}</span> ({s.proposal.book}) — {s.driving ? "applies next cycle" : "awaiting your apply"}</>
              : <>{s.note || "no structural edge found this cycle"}</>}
        </div>
        <div className="flex items-center gap-1.5" data-testid="autopilot-last-change">
          <TrendingUp className="w-3 h-3 text-neutral-500" />
          {s.last_change
            ? <>last change: <span className="text-neutral-200">{s.last_change.title}</span> · {s.last_change.status}{s.last_change.auto_applied ? " · auto" : ""} · {s.last_change.applied_at ? new Date(s.last_change.applied_at).toLocaleString() : ""}</>
            : <>no Doctor changes applied yet</>}
        </div>
        <div className="flex items-center gap-1.5">
          <Clock className="w-3 h-3 text-neutral-500" /> next review {fmtWhen(s.next_review_ts)} · <Wallet className="w-3 h-3 text-neutral-500" /> paper pools ${Number(config?.paper_bankroll_usd ?? 1000).toFixed(0)} per chain
          {s.kill_switch_tripped && <span className="text-rose-300"> · kill switch tripped</span>}
        </div>
      </div>

      <ProfitSweepPanel config={config} onConfigUpdate={onConfigUpdate} />
    </div>
  );
}

export default memo(AutopilotCard);
