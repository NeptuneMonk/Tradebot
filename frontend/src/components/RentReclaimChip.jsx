import { useEffect, useState } from "react";
import { Coins, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

export function RentReclaimChip() {
  const [rent, setRent] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = (fresh = false) => api.walletRent(fresh).then(setRent).catch(() => {});
  useEffect(() => {
    load();
    const id = setInterval(() => load(), 120000);
    return () => clearInterval(id);
  }, []);

  const reclaim = async () => {
    if (!rent?.accounts) return;
    if (!window.confirm(`Close ${rent.accounts} empty/dust token accounts and reclaim ~${rent.reclaimable_sol.toFixed(4)} SOL?\n\nWorthless dust is burned so the account can close. Open positions are never touched.`)) return;
    setBusy(true);
    try {
      const r = await api.walletRentReclaim();
      if (r.ok) toast.success(`Reclaimed ${Number(r.reclaimed_sol).toFixed(5)} SOL from ${r.closed} accounts`);
      else toast.error(r.reason || (r.errors || []).join("; ") || "nothing reclaimed");
      load(true);
    } catch (e) {
      toast.error(e?.response?.data?.detail || String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!rent) return null;
  const ready = rent.reclaimable_sol >= rent.min_sol;
  return (
    <div className="border-t border-neutral-800 pt-3 flex items-center justify-between gap-2 text-[11px] font-mono" data-testid="rent-reclaim-chip">
      <div className="flex items-center gap-2 min-w-0">
        <Coins className={`w-3 h-3 shrink-0 ${ready ? "text-amber-300" : "text-neutral-500"}`} />
        <span className="text-neutral-500 uppercase tracking-[0.15em] text-[10px]">Rent</span>
        <span className={ready ? "text-amber-200" : "text-neutral-300"} data-testid="rent-reclaimable">
          {rent.reclaimable_sol.toFixed(4)} SOL
        </span>
        <span className="text-neutral-600 truncate">· {rent.accounts} accts · auto {rent.auto ? `≥${rent.min_sol} SOL` : "off"}</span>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <button onClick={() => load(true)} className="text-neutral-500 hover:text-neutral-200" title="Rescan" data-testid="rent-rescan-btn">
          <RefreshCw className="w-3 h-3" />
        </button>
        <button
          onClick={reclaim}
          disabled={busy || !rent.accounts}
          className="border border-amber-700/60 text-amber-300 px-2 py-0.5 uppercase tracking-[0.12em] text-[10px] hover:bg-amber-900/30 disabled:opacity-40 disabled:cursor-not-allowed"
          data-testid="rent-reclaim-btn"
        >
          {busy ? "Closing…" : "Reclaim"}
        </button>
      </div>
    </div>
  );
}
