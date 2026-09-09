import { useEffect, useState } from "react";
import { Grid3x3, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

const fmtR = (v) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(2)}R`);

// Scorecard = the learning memory: book × pattern × band × 4h UTC bucket × cost bucket.
export default function ScorecardPanel() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(null);
  const load = () => api.scorecard().then(setData).catch(() => {});
  useEffect(() => { load(); }, []);
  const toggle = async (cell, disabled) => {
    setBusy(cell);
    try {
      await api.scorecardCell(cell, disabled);
      toast.success(`${cell} ${disabled ? "disabled — entries in this cell skip" : "re-enabled"}`);
      await load();
    } catch (e) {
      toast.error("Scorecard toggle failed");
    } finally {
      setBusy(null);
    }
  };
  const cells = data?.cells || [];
  return (
    <div className="border border-neutral-800 bg-neutral-950 p-3" data-testid="scorecard-panel">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-[0.2em] text-neutral-500 inline-flex items-center gap-1.5"><Grid3x3 className="w-3 h-3" /> Scorecard · situation cells</div>
        <button type="button" onClick={load} className="text-neutral-500 hover:text-neutral-300" data-testid="scorecard-refresh" title="refresh"><RefreshCw className="w-3 h-3" /></button>
      </div>
      <div className="text-[9px] font-mono text-neutral-600 mb-2">
        n ≥ {data?.min_n ?? 30} and E[R] &lt; 0 → cell disabled (new entries skip) · E[R] ≥ {data?.upweight_r ?? 0.3} → allocator upweight · disabled cells reopen only after {data?.reopen_after_h ?? 72}h + {data?.reopen_min_paper ?? 10} fresh paper fills
      </div>
      {cells.length === 0 ? (
        <div className="text-[10px] font-mono text-neutral-600 py-3 text-center" data-testid="scorecard-empty">no closed fills scored yet — cells appear as trades close</div>
      ) : (
        <div className="overflow-x-auto max-h-72 overflow-y-auto">
          <table className="w-full text-[10px] font-mono">
            <thead><tr className="text-neutral-500 uppercase tracking-[0.1em] text-left"><th className="py-1">cell</th><th className="text-right">n</th><th className="text-right">E[R]</th><th className="text-right">wr</th><th className="text-right">avg W / L</th><th className="text-right">state</th><th></th></tr></thead>
            <tbody>
              {cells.map((c) => (
                <tr key={c.cell} className="border-t border-neutral-800/60" data-testid={`scorecard-row-${c.cell}`}>
                  <td className="py-1 text-neutral-300">{c.cell}</td>
                  <td className="text-right text-neutral-400">{c.n}</td>
                  <td className={`text-right ${(c.expectancy_r ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300"}`}>{fmtR(c.expectancy_r)}</td>
                  <td className="text-right text-neutral-400">{c.wr != null ? `${Math.round(c.wr * 100)}%` : "—"}</td>
                  <td className="text-right text-neutral-400">{fmtR(c.avg_win_r)} / {fmtR(c.avg_loss_r)}</td>
                  <td className="text-right">
                    {c.disabled ? <span className="text-rose-300" data-testid={`scorecard-state-${c.cell}`}>disabled</span>
                      : c.upweight_eligible ? <span className="text-lime-300" data-testid={`scorecard-state-${c.cell}`}>upweight</span>
                      : <span className="text-neutral-500" data-testid={`scorecard-state-${c.cell}`}>open</span>}
                  </td>
                  <td className="text-right">
                    <button type="button" disabled={busy === c.cell} onClick={() => toggle(c.cell, !c.disabled)} data-testid={`scorecard-toggle-${c.cell}`}
                      className="px-1.5 py-0.5 border border-neutral-700 text-neutral-300 hover:bg-neutral-900 text-[9px] uppercase tracking-wider disabled:opacity-40">
                      {c.disabled ? "enable" : "disable"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
