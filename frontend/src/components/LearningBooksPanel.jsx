import { useEffect, useState, useCallback } from "react";
import { FlaskConical, BookOpen, Undo2 } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

const fmtUsd = (v) => (v == null ? "—" : `${v >= 0 ? "+" : ""}$${Number(v).toFixed(3)}`);
const BOOKS = ["momentum", "greylist_snipe", "reentry", "rh_pons"];
const LABEL = { momentum: "Momentum book", greylist_snipe: "Greylist snipe book", reentry: "Re-entry book", rh_pons: "RH · PONS book (paper)" };
const pct = (v, d = 0) => (v == null ? "—" : `${Number(v).toFixed(d)}%`);

export default function LearningBooksPanel() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setData(await api.doctorLearning()); } catch { /* panel is best-effort */ }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  const act = async (fn, okMsg) => {
    setBusy(true);
    try { await fn(); toast.success(okMsg); await load(); }
    catch (e) { toast.error(e?.response?.data?.detail || e.message); }
    finally { setBusy(false); }
  };

  if (!data) return null;
  const canary = data.canary;
  const running = canary?.state === "running";
  const books = data.books || {};
  const p = data.proposal;

  return (
    <div className="mt-3 border border-neutral-800 p-3 space-y-3" data-testid="learning-panel">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-400">
          <FlaskConical className="w-3 h-3" /> Learning loop
        </div>
        <span className="text-[9px] font-mono text-neutral-500">
          audits every book (SOL + RH) on $ expectancy per fill — profit, not win rate · one change at a time · paper-first canary
        </span>
      </div>

      {running && (
        <div className="border border-lime-800/60 bg-lime-950/30 px-3 py-2 text-[10px] font-mono text-lime-200 flex items-center justify-between gap-2 flex-wrap"
             data-testid="learning-canary-banner">
          <span>
            Canary running: <span className="text-lime-300">{canary.proposal?.key}={String(canary.proposal?.value)}</span>
            {" "}on <span className="uppercase">{canary.book}</span> · started {new Date(canary.started_at).toLocaleTimeString()}
            {" "}· promotes after {canary.n_req ?? "N"} trades / time window if fill expectancy beats {fmtUsd(canary.baseline_expectancy_usd ?? canary.baseline_expectancy_sol)}
          </span>
          <button type="button" disabled={busy} data-testid="learning-revert-btn"
                  onClick={() => act(api.doctorLearningRevert, "Canary reverted — baseline restored, proposal blacklisted 24h")}
                  className="inline-flex items-center gap-1 px-2 py-1 border border-rose-800 text-rose-300 hover:bg-rose-950/40 text-[9px] uppercase tracking-wider">
            <Undo2 className="w-3 h-3" /> Revert now
          </button>
        </div>
      )}

      {!running && canary && canary.state !== "idle" && (
        <div className={`px-3 py-1.5 text-[10px] font-mono border ${canary.state === "promote" ? "border-emerald-900 text-emerald-300" : "border-neutral-800 text-neutral-400"}`}
             data-testid="learning-canary-last">
          Last canary {canary.proposal?.key}={String(canary.proposal?.value)} → <span className="uppercase">{canary.state}</span>
          {canary.expectancy_since != null && <> · expectancy since {fmtUsd(canary.expectancy_since)} (n={canary.n_since}) vs baseline {fmtUsd(canary.baseline_expectancy_usd ?? canary.baseline_expectancy_sol)}</>}
          {canary.revert_reason && <> · {canary.revert_reason}</>}
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {BOOKS.map((b) => {
          const s = books[b] || { n: 0 };
          const pos = (s.expectancy_usd ?? 0) >= 0;
          const bt = s.by_trigger || {};
          return (
            <div key={b} className="border border-neutral-800 p-2.5" data-testid={`learning-book-${b}`}>
              <div className="flex items-center justify-between">
                <span className="text-[10px] uppercase tracking-[0.15em] text-neutral-300 inline-flex items-center gap-1.5">
                  <BookOpen className="w-3 h-3" /> {LABEL[b]}
                </span>
                <span className="text-[9px] font-mono text-neutral-500">n={s.n ?? 0} (24h) · {s.n_7d ?? 0} (7d)</span>
              </div>
              <div className="mt-1.5 grid grid-cols-3 gap-2 text-[10px] font-mono">
                <div><div className="text-neutral-600 text-[9px]">expectancy / fill</div>
                  <div className={pos ? "text-emerald-300" : "text-rose-300"} data-testid={`learning-exp-${b}`}>{fmtUsd(s.expectancy_usd)}</div></div>
                <div><div className="text-neutral-600 text-[9px]">win rate · payoff</div><div className="text-neutral-200">{pct(s.winrate)} · {s.payoff_ratio == null ? "—" : `${s.payoff_ratio.toFixed(2)}x`}</div></div>
                <div><div className="text-neutral-600 text-[9px]">total (24h)</div><div className={pos ? "text-emerald-300/80" : "text-rose-300/80"}>{fmtUsd(s.total_usd)}</div></div>
                <div><div className="text-neutral-600 text-[9px]">exits SL · TP</div><div className="text-neutral-200">{pct(s.sl_share)} · {pct(s.tp_share)}</div></div>
                <div><div className="text-neutral-600 text-[9px]">giveback (med)</div><div className="text-neutral-200">{s.median_giveback == null ? "—" : `${s.median_giveback.toFixed(1)}pp`}</div></div>
                <div><div className="text-neutral-600 text-[9px]">hold (med)</div><div className="text-neutral-200">{s.median_hold_s == null ? "—" : `${Math.round(s.median_hold_s)}s`}</div></div>
                <div><div className="text-neutral-600 text-[9px]">latency tax</div><div className="text-neutral-200">{s.median_latency_tax == null ? "—" : `${s.median_latency_tax.toFixed(1)}pp`}</div></div>
                <div><div className="text-neutral-600 text-[9px]">stale/timeout</div><div className="text-neutral-200">{pct(s.stale_timeout_share)}</div></div>
              </div>
              {b === "reentry" && Object.keys(bt).length > 0 && (
                <div className="mt-1.5 flex flex-wrap gap-2 text-[9px] font-mono" data-testid="learning-reentry-triggers">
                  {Object.entries(bt).map(([k, v]) => (
                    <span key={k} className={`px-1.5 py-0.5 border ${v.expectancy_usd >= 0 ? "border-emerald-900 text-emerald-300" : "border-rose-900 text-rose-300"}`}>
                      {k}: {fmtUsd(v.expectancy_usd)}/fill · n={v.n} · {pct(v.winrate)} wr
                    </span>
                  ))}
                </div>
              )}
              {p && p.book === b && !running && (
                <div className="mt-1.5 text-[9px] font-mono text-lime-300/90">last proposal: {p.key} → {String(p.value)}</div>
              )}
            </div>
          );
        })}
      </div>

      {p && !running && (
        <div className="border border-lime-900/60 bg-lime-950/20 px-3 py-2 text-[10px] font-mono flex items-center justify-between gap-2 flex-wrap"
             data-testid="learning-proposal">
          <span className="text-neutral-200">
            Proposal <span className="text-lime-300">{p.key} → {String(p.value)}</span> ({p.type}, {p.book}) — {p.reason}
          </span>
          <button type="button" disabled={busy} data-testid="learning-apply-btn"
                  onClick={() => act(api.doctorLearningApply, "Canary started — Doctor will promote or revert on measured expectancy")}
                  className="px-2 py-1 border border-lime-700 text-lime-200 hover:bg-lime-900/40 text-[9px] uppercase tracking-wider">
            Apply proposal
          </button>
        </div>
      )}
      {!p && !running && data.note && (
        <div className="text-[9px] font-mono text-neutral-500" data-testid="learning-note">{data.note}</div>
      )}
    </div>
  );
}
