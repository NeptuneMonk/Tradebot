import { useEffect, useState, useCallback, useMemo } from "react";
import { AlertTriangle, RefreshCw, Loader2, Search } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

function shortMint(m) {
  return m ? `${m.slice(0, 4)}…${m.slice(-4)}` : "—";
}

export default function StuckPositions() {
  const [stuck, setStuck] = useState([]);
  const [walletTokens, setWalletTokens] = useState([]);
  const [loading, setLoading] = useState(true);
  const [walletLoading, setWalletLoading] = useState(false);
  const [selected, setSelected] = useState(() => new Set());
  const [recovering, setRecovering] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const d = await api.stuckTrades();
      setStuck(d.stuck || []);
      setSelected(prev => {
        const ids = new Set((d.stuck || []).map(s => s.id));
        const next = new Set();
        prev.forEach(id => { if (ids.has(id)) next.add(id); });
        return next;
      });
    } catch {
      // silent
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshWallet = useCallback(async () => {
    setWalletLoading(true);
    try {
      const d = await api.walletTokenScan();
      setWalletTokens(d.tokens || []);
    } catch (e) {
      toast.error(`Wallet scan failed: ${e?.response?.data?.detail || e.message}`);
    } finally {
      setWalletLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 30_000);
    return () => clearInterval(id);
  }, [refresh]);

  const totalUsd = useMemo(
    () => stuck.reduce((s, p) => s + (p.current_usd || 0), 0),
    [stuck]
  );
  const selectedRows = useMemo(
    () => stuck.filter(p => selected.has(p.id)),
    [stuck, selected]
  );
  const selectedUsd = selectedRows.reduce((s, p) => s + (p.current_usd || 0), 0);

  const walletTotalUsd = useMemo(
    () => walletTokens.reduce((s, p) => s + (p.current_usd || 0), 0),
    [walletTokens]
  );
  // After 2026-05-25, the backend recover endpoints route graduated tokens
  // through PumpSwap AMM, so they ARE sellable — include them in the
  // selectable wallet-tokens list. `current_usd > 0` is the only filter.
  const sellableWalletTokens = walletTokens.filter(p => p.current_usd > 0);

  const toggleOne = (id) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    if (selected.size === stuck.length && stuck.length > 0) setSelected(new Set());
    else setSelected(new Set(stuck.map(p => p.id)));
  };

  const recoverSelected = async () => {
    if (selected.size === 0) return;
    const ids = Array.from(selected);
    const valued = selectedRows.filter(r => r.current_usd > 0).length;
    const zeros = ids.length - valued;
    const ok = window.confirm(
      `Recover ${ids.length} stuck position${ids.length > 1 ? "s" : ""}?\n` +
      `  • ${valued} will attempt on-chain sell\n` +
      `  • ${zeros} have zero balance and will be auto-closed`
    );
    if (!ok) return;
    setRecovering(true);
    try {
      const res = await api.recoverStuckBatch(ids);
      toast.success(`Recovered ${res.recovered} · Closed ${res.auto_closed} · Errors ${res.errors}`);
      setSelected(new Set());
      await refresh();
      await refreshWallet();
    } catch (e) {
      toast.error(`Recover failed: ${e?.response?.data?.detail || e.message}`);
    } finally {
      setRecovering(false);
    }
  };

  const recoverWalletAll = async () => {
    const mints = sellableWalletTokens.map(t => t.mint);
    if (mints.length === 0) return;
    const ok = window.confirm(
      `Sell ALL ${mints.length} pump.fun tokens in your wallet for ~$${walletTotalUsd.toFixed(2)}?\n\n` +
      `Runs 3 sells in parallel. Each batch ~25s. Total ~${Math.ceil(mints.length/3) * 25}s.`
    );
    if (!ok) return;
    setRecovering(true);
    try {
      const res = await api.walletRecoverMints(mints);
      const failures = (res.results || []).filter(r => !r.ok);
      if (failures.length > 0) {
        toast.warning(`Recovered ${res.recovered}/${res.total} · ${failures.length} failed (see refresh)`);
      } else {
        toast.success(`Recovered all ${res.recovered}!`);
      }
    } catch (e) {
      // 520 / 524 / timeout: backend probably still finishing in background
      const code = e?.response?.status;
      if (code === 520 || code === 524 || e?.code === "ECONNABORTED") {
        toast.info("Recovery is taking longer than expected — backend is still processing. Re-scan in 30s.");
      } else {
        toast.error(`Recover failed: ${e?.response?.data?.detail || e.message}`);
      }
    } finally {
      setRecovering(false);
      // Re-scan after a delay so the user sees the resulting wallet state
      setTimeout(refreshWallet, 5000);
    }
  };

  if (loading) return null;

  const hasStuck = stuck.length > 0;
  const hasWalletTokens = walletTokens.length > 0;
  if (!hasStuck && !hasWalletTokens && !walletLoading) {
    // Show just the "Scan wallet" button so user can discover stranded tokens
    return (
      <div className="border-t border-neutral-800 pt-3" data-testid="stuck-positions-section">
        <button
          type="button"
          onClick={refreshWallet}
          data-testid="scan-wallet-btn"
          className="w-full text-[10px] uppercase tracking-[0.2em] text-neutral-500 hover:text-amber-400 flex items-center justify-center gap-2 py-1 border border-dashed border-neutral-800 hover:border-amber-900 transition"
        >
          <Search className="w-3 h-3" />
          Scan wallet for stranded Pump.fun tokens
        </button>
      </div>
    );
  }
  const allSelected = selected.size === stuck.length;

  return (
    <div className="border-t border-neutral-800 pt-3 space-y-4" data-testid="stuck-positions-section">
      {/* DB-tracked stuck positions */}
      {hasStuck && (
        <div>
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-amber-400">
              <AlertTriangle className="w-3 h-3" />
              Stuck positions ({stuck.length})
            </div>
            <button type="button" onClick={refresh} className="text-neutral-500 hover:text-neutral-200 transition" title="Refresh" data-testid="stuck-refresh-btn">
              <RefreshCw className="w-3 h-3" />
            </button>
          </div>
          <div className="text-[10px] font-mono text-neutral-400 mb-2">
            Total: <span className="text-amber-300">${totalUsd.toFixed(4)}</span>
          </div>
          <div className="border border-neutral-800 rounded-sm overflow-hidden">
            <div className="grid grid-cols-[24px_1fr_70px_80px] gap-2 px-2 py-1.5 bg-neutral-900/60 text-[9px] uppercase tracking-wider text-neutral-500">
              <input type="checkbox" checked={allSelected} onChange={toggleAll} data-testid="stuck-select-all" className="w-3 h-3 accent-amber-500" />
              <span>Token / Mint</span>
              <span className="text-right">% held</span>
              <span className="text-right">Value</span>
            </div>
            <div className="max-h-40 overflow-y-auto divide-y divide-neutral-800/60">
              {stuck.map((p) => {
                const isSelected = selected.has(p.id);
                const isZero = (p.current_usd || 0) <= 0;
                return (
                  <label key={p.id} className={`grid grid-cols-[24px_1fr_70px_80px] gap-2 px-2 py-1.5 text-[11px] font-mono cursor-pointer hover:bg-neutral-900/40 ${isSelected ? "bg-amber-950/20" : ""}`} data-testid={`stuck-row-${p.id}`}>
                    <input type="checkbox" checked={isSelected} onChange={() => toggleOne(p.id)} className="w-3 h-3 accent-amber-500" data-testid={`stuck-checkbox-${p.id}`} />
                    <div className="min-w-0">
                      <div className="text-neutral-200 truncate" title={p.mint}>{p.symbol || shortMint(p.mint)}</div>
                      <div className="text-[9px] text-neutral-600 truncate">{shortMint(p.mint)} {p.graduated && <span className="ml-1 text-blue-400">grad</span>}</div>
                    </div>
                    <div className="text-right text-neutral-400 self-center">{p.entry_pct_held ? `${p.entry_pct_held.toFixed(0)}%` : "—"}</div>
                    <div className={`text-right self-center ${isZero ? "text-neutral-600" : "text-amber-300"}`}>${(p.current_usd || 0).toFixed(4)}</div>
                  </label>
                );
              })}
            </div>
          </div>
          <div className="flex items-center justify-between gap-2 mt-2">
            <div className="text-[10px] font-mono text-neutral-500">{selected.size > 0 ? `${selected.size} selected · $${selectedUsd.toFixed(4)}` : "Select positions to recover"}</div>
            <button type="button" onClick={recoverSelected} disabled={selected.size === 0 || recovering} data-testid="recover-selected-btn" className="px-3 py-1 text-[10px] uppercase tracking-wider font-mono border border-amber-700/60 text-amber-300 hover:bg-amber-950/40 disabled:opacity-40 disabled:cursor-not-allowed inline-flex items-center gap-1.5">
              {recovering ? <><Loader2 className="w-3 h-3 animate-spin" /> Recovering…</> : <>Recover selected</>}
            </button>
          </div>
        </div>
      )}

      {/* Wallet-wide scan */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-blue-400">
            <Search className="w-3 h-3" />
            Wallet token scan {hasWalletTokens && `(${sellableWalletTokens.length} sellable)`}
          </div>
          <button type="button" onClick={refreshWallet} disabled={walletLoading} className="text-neutral-500 hover:text-neutral-200 transition disabled:opacity-40" title="Re-scan wallet" data-testid="wallet-rescan-btn">
            {walletLoading ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
          </button>
        </div>
        {hasWalletTokens && (
          <>
            <div className="text-[10px] font-mono text-neutral-400 mb-2">
              Total recoverable: <span className="text-blue-300">${walletTotalUsd.toFixed(4)}</span>
            </div>
            <div className="border border-neutral-800 rounded-sm overflow-hidden">
              <div className="grid grid-cols-[1fr_60px_80px] gap-2 px-2 py-1.5 bg-neutral-900/60 text-[9px] uppercase tracking-wider text-neutral-500">
                <span>Mint</span>
                <span className="text-right">Tokens</span>
                <span className="text-right">Value</span>
              </div>
              <div className="max-h-40 overflow-y-auto divide-y divide-neutral-800/60">
                {walletTokens.map((p) => (
                  <div key={p.mint} className="grid grid-cols-[1fr_60px_80px] gap-2 px-2 py-1.5 text-[11px] font-mono" data-testid={`wallet-row-${p.mint}`}>
                    <div className="min-w-0">
                      <div className="text-neutral-200 truncate" title={p.mint}>{p.symbol || shortMint(p.mint)}</div>
                      <div className="text-[9px] text-neutral-600 truncate">
                        {shortMint(p.mint)}
                        {p.graduated && <span className="ml-1 text-blue-400">grad</span>}
                        {p.token_program === "Token-2022" && <span className="ml-1 text-neutral-700">t22</span>}
                      </div>
                    </div>
                    <div className="text-right text-neutral-500 self-center text-[10px]">{(p.amount_ui >= 1e6 ? (p.amount_ui/1e6).toFixed(1) + "M" : p.amount_ui >= 1e3 ? (p.amount_ui/1e3).toFixed(1) + "K" : p.amount_ui.toFixed(0))}</div>
                    <div className={`text-right self-center ${p.current_usd > 0 ? "text-blue-300" : "text-neutral-600"}`}>${(p.current_usd || 0).toFixed(4)}</div>
                  </div>
                ))}
              </div>
            </div>
            <div className="flex items-center justify-between gap-2 mt-2">
              <div className="text-[10px] font-mono text-neutral-500">
                {sellableWalletTokens.length === 0 ? "Nothing sellable" : `${sellableWalletTokens.length} on bonding curve · $${walletTotalUsd.toFixed(4)}`}
              </div>
              <button type="button" onClick={recoverWalletAll} disabled={sellableWalletTokens.length === 0 || recovering} data-testid="wallet-recover-all-btn" className="px-3 py-1 text-[10px] uppercase tracking-wider font-mono border border-blue-700/60 text-blue-300 hover:bg-blue-950/40 disabled:opacity-40 disabled:cursor-not-allowed inline-flex items-center gap-1.5">
                {recovering ? <><Loader2 className="w-3 h-3 animate-spin" /> Selling…</> : <>Sell all</>}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

