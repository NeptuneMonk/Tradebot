import { useCallback, useEffect, useState } from "react";
import { PiggyBank, ArrowUpRight } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import HelpHint from "./HelpHint";

const usd = (v) => (v == null ? "—" : `$${Number(v).toFixed(2)}`);
const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");
const short = (a) => (a ? `${a.slice(0, 4)}…${a.slice(-4)}` : "—");

export default function ProfitSweepPanel({ config, onConfigUpdate }) {
  const [s, setS] = useState(null);
  const [wallet, setWallet] = useState(config?.sweep_cold_wallet || "");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try { setS(await api.sweepStatus()); } catch { /* best effort */ }
  }, []);
  useEffect(() => { load(); const id = setInterval(load, 60000); return () => clearInterval(id); }, [load, config?.sweep_enabled, config?.sweep_baseline_usd]);
  useEffect(() => { setWallet(config?.sweep_cold_wallet || ""); }, [config?.sweep_cold_wallet]);

  const save = async (patch, msg = "Sweep settings saved") => {
    try {
      const upd = await api.updateConfig(patch);
      onConfigUpdate && onConfigUpdate(upd);
      toast.success(msg);
      load();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message);
    }
  };
  const runNow = async () => {
    setBusy(true);
    try {
      const r = await api.sweepRunNow();
      toast.success(`Swept ${usd(r.sweep?.amount_usd)}${r.sweep?.amount_sol ? ` (${r.sweep.amount_sol} SOL)` : ""} · ${r.sweep?.mode}${r.sweep?.sig ? ` · ${r.sweep.sig.slice(0, 8)}…` : ""}`);
      load();
    } catch (e) {
      toast.error(`Sweep refused: ${e?.response?.data?.detail || e.message}`);
    } finally { setBusy(false); }
  };

  if (!s) return null;
  const enabled = !!config?.sweep_enabled;
  const numField = (label, key, step, testid) => (
    <label className="block">
      <span className="text-[9px] uppercase tracking-[0.15em] text-neutral-600">{label}</span>
      <input
        type="number" step={step} min="0"
        defaultValue={config?.[key]} key={`${key}-${config?.[key]}`}
        onBlur={(e) => { const v = parseFloat(e.target.value); if (!Number.isNaN(v) && v !== config?.[key]) save({ [key]: v }); }}
        onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
        className="mt-0.5 w-full px-2 py-1 bg-neutral-950 border border-neutral-800 text-neutral-200 tabular-nums"
        data-testid={testid}
      />
    </label>
  );

  return (
    <div className="mt-4 border-t border-neutral-800 pt-3" data-testid="profit-sweep-panel">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-400">
          <PiggyBank className="w-3.5 h-3.5" /> Profit sweep
          <HelpHint label="Profit sweep">
            Every {config?.sweep_interval_days ?? 7} days, move {config?.sweep_pct_of_profit ?? 50}% of the bankroll growth above the baseline to your cold wallet. Live = real SOL transfer from the hot wallet (a reserve stays for fees). Paper = ledger entry, subtracted from the paper bankroll. After each sweep the baseline moves to the post-sweep bankroll, so retained profit keeps compounding and only new growth is sliced.
          </HelpHint>
        </div>
        <label className="flex items-center gap-1.5 text-[10px] font-mono uppercase text-neutral-400 cursor-pointer">
          <input
            type="checkbox" checked={enabled}
            onChange={(e) => {
              if (e.target.checked && !(config?.sweep_cold_wallet)) { toast.error("Set a cold wallet address first"); return; }
              save({ sweep_enabled: e.target.checked }, e.target.checked ? "Profit sweep ON" : "Profit sweep OFF");
            }}
            data-testid="sweep-enabled-checkbox"
          />
          {enabled ? "on" : "off"}
        </label>
      </div>

      <div className="mt-2 grid grid-cols-1 sm:grid-cols-[2fr_1fr_1fr_1fr] gap-2 text-[10px] font-mono">
        <label className="block">
          <span className="text-[9px] uppercase tracking-[0.15em] text-neutral-600">cold wallet (Solana address)</span>
          <input
            type="text" value={wallet} placeholder="paste destination address"
            onChange={(e) => setWallet(e.target.value.trim())}
            onBlur={() => wallet !== (config?.sweep_cold_wallet || "") && save({ sweep_cold_wallet: wallet }, "Cold wallet saved")}
            onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
            className={`mt-0.5 w-full px-2 py-1 bg-neutral-950 border text-neutral-200 ${wallet && s.cold_wallet_valid === false && wallet === config?.sweep_cold_wallet ? "border-rose-800" : "border-neutral-800"}`}
            data-testid="sweep-cold-wallet-input"
          />
        </label>
        {numField("% of profit", "sweep_pct_of_profit", 5, "sweep-pct-input")}
        {numField("every (days)", "sweep_interval_days", 1, "sweep-interval-input")}
        {numField("min sweep $", "sweep_min_usd", 5, "sweep-min-input")}
      </div>

      <div className="mt-2 grid grid-cols-2 sm:grid-cols-4 gap-3 text-[10px] font-mono">
        <div><div className="text-[9px] uppercase tracking-[0.15em] text-neutral-600">baseline</div><div className="text-neutral-200" data-testid="sweep-baseline">{usd(s.baseline_usd)}</div></div>
        <div><div className="text-[9px] uppercase tracking-[0.15em] text-neutral-600">profit above baseline</div><div className={s.profit_above_baseline_usd > 0 ? "text-emerald-300" : "text-neutral-400"} data-testid="sweep-profit">{usd(s.profit_above_baseline_usd)}</div></div>
        <div><div className="text-[9px] uppercase tracking-[0.15em] text-neutral-600">projected sweep</div><div className="text-lime-300" data-testid="sweep-projected">{usd(s.projected_sweep_usd)}</div></div>
        <div><div className="text-[9px] uppercase tracking-[0.15em] text-neutral-600">next due</div><div className="text-neutral-200" data-testid="sweep-next-due">{enabled ? when(s.next_due_ts) : "—"}</div></div>
      </div>

      <div className="mt-2 flex items-center gap-2 flex-wrap text-[10px] font-mono">
        <button type="button" onClick={runNow} disabled={busy || !s.cold_wallet_valid}
                className="px-2 py-1 border border-lime-700 text-lime-300 hover:bg-lime-950/60 disabled:opacity-40 uppercase inline-flex items-center gap-1"
                data-testid="sweep-run-now-btn" title="Sweep now — skips the schedule, keeps every safety check">
          <ArrowUpRight className="w-3 h-3" /> {busy ? "…" : "sweep now"}
        </button>
        <button type="button" onClick={() => api.sweepResetBaseline().then(() => { toast.success("Baseline re-anchored to current bankroll"); load(); api.config().then(onConfigUpdate); })}
                className="px-2 py-1 border border-neutral-700 text-neutral-400 hover:bg-neutral-800 uppercase"
                data-testid="sweep-reset-baseline-btn" title="Re-anchor the baseline to the current bankroll (e.g. after a deposit)">
          reset baseline
        </button>
        <span className="text-neutral-600">total swept ({s.mode}): <span className="text-neutral-300" data-testid="sweep-total">{usd(s.total_swept_usd)}</span>{s.last_error && <span className="text-rose-400"> · last error: {s.last_error}</span>}</span>
      </div>

      {s.history?.length > 0 && (
        <div className="mt-2 max-h-28 overflow-y-auto text-[10px] font-mono" data-testid="sweep-history">
          {s.history.map((h, i) => (
            <div key={i} className="flex items-center justify-between gap-2 py-0.5 border-t border-neutral-900 text-neutral-400">
              <span>{when(h.ts)} · {h.mode}{h.status === "failed" ? <span className="text-rose-400"> · failed</span> : ""}</span>
              <span className="text-neutral-200">{usd(h.amount_usd)}{h.amount_sol ? ` (${h.amount_sol} SOL)` : ""}</span>
              <span className="text-neutral-600">→ {short(h.cold_wallet)}{h.sig ? ` · ${h.sig.slice(0, 8)}…` : ""}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
