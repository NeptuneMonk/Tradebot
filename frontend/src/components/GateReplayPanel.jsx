import { useEffect, useState } from "react";
import { Search, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { api, explainApiError } from "@/lib/api";
import CopyAddr from "./CopyAddr";

const fmtTs = (iso) => (iso ? new Date(iso).toLocaleTimeString() : "—");
const fmtEpoch = (s) => (s ? new Date(s * 1000).toLocaleTimeString() : "—");

function Verdict({ ok }) {
  if (ok === null || ok === undefined) return <span className="text-neutral-600 font-mono text-[10px]">n/a</span>;
  return <span className={`font-mono text-[10px] px-1.5 py-0.5 border ${ok ? "border-emerald-700 text-emerald-300" : "border-red-800 text-red-300"}`}>{ok ? "PASS" : "FAIL"}</span>;
}

function GateRows({ rows }) {
  return (
    <table className="w-full text-[11px] font-mono" data-testid="replay-gate-table">
      <thead className="text-neutral-500 text-[9px] uppercase tracking-[0.12em]">
        <tr><th className="text-left py-1">gate</th><th className="text-left">live reading</th><th className="text-left">threshold</th><th className="text-left">verdict</th></tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.gate} className="border-t border-neutral-900" data-testid={`replay-row-${r.gate.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}`} title={r.note || ""}>
            <td className="py-1 text-neutral-200">{r.gate}{r.note ? <span className="block text-[9px] text-neutral-600 font-sans normal-case">{r.note}</span> : null}</td>
            <td className={r.pass === false ? "text-red-300" : "text-neutral-300"}>{String(r.reading ?? "—")}</td>
            <td className="text-neutral-500">{r.threshold}</td>
            <td><Verdict ok={r.pass} /></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function History({ data }) {
  const trades = data.trades || [];
  const decisions = data.decisions || [];
  return (
    <div className="grid md:grid-cols-2 gap-3 text-[11px] font-mono" data-testid="replay-history">
      <div>
        <div className="text-[9px] uppercase tracking-[0.12em] text-neutral-500 mb-1">our trades on this token</div>
        {trades.length === 0 && <div className="text-neutral-600">never entered</div>}
        {trades.map((t) => (
          <div key={t.id} className="border border-neutral-800 p-2 mb-1" data-testid={`replay-trade-${t.id}`}>
            <div className="flex justify-between"><span className="text-neutral-300">{fmtTs(t.entry_time)} → {fmtTs(t.exit_time)}</span>
              <span className={Number(t.pnl_usd) >= 0 ? "text-emerald-300" : "text-red-300"}>{t.pnl_usd != null ? `${Number(t.pnl_usd) >= 0 ? "+" : ""}$${Number(t.pnl_usd).toFixed(2)} (${Number(t.pnl_pct || 0).toFixed(1)}%)` : t.status}</span></div>
            <div className="text-neutral-500">entered via <span className="text-neutral-300">{t.classifier_action || "?"}</span> · {t.book} · {t.mode} · ${Number(t.entry_usd || 0).toFixed(2)}
              {t.expected_cost_pct != null ? ` · cost gate ${Number(t.expected_cost_pct).toFixed(1)}%` : ""}</div>
            {t.exit_reason && <div className="text-neutral-400">exit: <span className="text-amber-200">{t.exit_reason}</span></div>}
          </div>
        ))}
      </div>
      <div>
        <div className="text-[9px] uppercase tracking-[0.12em] text-neutral-500 mb-1">scanner verdict ledger (transitions)</div>
        {decisions.length === 0 && <div className="text-neutral-600">no verdict history recorded</div>}
        {decisions.map((d) => (
          <div key={`${d.ts}-${d.verdict}`} className="flex gap-2 text-neutral-400"><span className="text-neutral-600">{fmtEpoch(d.ts)}</span><span className="text-neutral-200">{d.verdict}</span>
            {d.price_sol ? <span className="text-neutral-600">{Number(d.price_sol).toExponential(2)} SOL</span> : null}</div>
        ))}
        {data.last_refusal && <div className="mt-2 text-amber-300">last entry refusal: {data.last_refusal.reason} — <span className="text-neutral-400">{data.last_refusal.detail}</span></div>}
      </div>
    </div>
  );
}

export default function GateReplayPanel({ initialMint = "" }) {
  const [mint, setMint] = useState(initialMint);
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);

  const run = async (m) => {
    const target = (m || mint).trim();
    if (!target) return;
    setBusy(true);
    try {
      setData(await api.gateReplay(target));
    } catch (e) {
      toast.error(explainApiError(e, "replay failed"));
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => { if (initialMint) { setMint(initialMint); run(initialMint); } }, [initialMint]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="border border-neutral-800 bg-neutral-950 p-3" data-testid="gate-replay-panel">
      <div className="flex items-center gap-2 mb-2">
        <Search className="w-3.5 h-3.5 text-neutral-500" />
        <input value={mint} onChange={(e) => setMint(e.target.value)} onKeyDown={(e) => e.key === "Enter" && run()} placeholder="paste a mint address…"
          className="flex-1 bg-neutral-900 border border-neutral-800 px-2 py-1 font-mono text-[11px] text-neutral-200" data-testid="replay-mint-input" />
        <button type="button" onClick={() => run()} disabled={busy} data-testid="replay-run-btn"
          className="px-3 py-1 border border-emerald-700 text-emerald-300 font-mono text-[10px] tracking-[0.1em] hover:bg-neutral-900 disabled:opacity-50">
          {busy ? <RefreshCw className="w-3 h-3 animate-spin" /> : "REPLAY"}
        </button>
      </div>
      <p className="text-[10px] text-neutral-500 mb-2">Every gate's live reading against its current threshold, exactly as the scanner computes it — plus why we entered or exited. Hover a row for the knob behind it.</p>
      {data && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-3 text-[11px] font-mono" data-testid="replay-summary">
            <span className="text-neutral-100">{data.symbol || "?"}</span> <CopyAddr value={data.mint} label="Mint" className="text-neutral-500" />
            <span className="text-neutral-500">{data.protocol} · {data.band ? `${data.band} band` : "no band"} · age {data.age_min} min · MC ${Number(data.mc_usd || 0).toLocaleString()}</span>
            <span className={`px-1.5 py-0.5 border ${data.would_enter ? "border-emerald-700 text-emerald-300" : "border-red-800 text-red-300"}`} data-testid="replay-would-enter">
              {data.would_enter ? "WOULD ENTER NOW" : `BLOCKED: ${data.blockers.join(", ")}`}
            </span>
            {!data.known && <span className="text-amber-300">not tracked and no history — readings are from chain only</span>}
            {data.gate_reason && <span className="text-neutral-500">scanner says: {data.gate_reason}{data.gate_detail ? ` — ${data.gate_detail}` : ""}</span>}
          </div>
          <GateRows rows={data.rows} />
          <History data={data} />
        </div>
      )}
    </div>
  );
}
