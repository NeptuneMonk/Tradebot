import { useState } from "react";
import { Plus, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

const fmtUsd = (n) => { const v = Number(n) || 0; return v >= 1e6 ? `$${(v / 1e6).toFixed(2)}M` : v >= 1e3 ? `$${(v / 1e3).toFixed(1)}K` : `$${v.toFixed(0)}`; };

/** Operator pins an established RH token for the Graduate Ladder — no age gate, no MC gate, staircase + retry as usual. */
export function AddManualToken({ onAdded, count, max = 25 }) {
  const [open, setOpen] = useState(false);
  const [address, setAddress] = useState("");
  const [quote, setQuote] = useState("");
  const [busy, setBusy] = useState(false);
  const valid = /^0x[0-9a-fA-F]{40}$/.test(address.trim());
  const submit = async (e) => {
    e.preventDefault();
    if (!valid || busy) return;
    setBusy(true);
    try {
      const r = await api.addManualLadder(address.trim(), quote.trim() || null);
      toast.success(`${r.symbol || r.token.slice(0, 8)} pinned to the ladder`, {
        description: `pool vs ${r.quote_symbol} · MC ${fmtUsd(r.usd_market_cap)}${r.quote_priced ? "" : " · quote has no USD price yet — MC fills in when it does"}` });
      setAddress(""); setQuote(""); setOpen(false);
      onAdded?.(r);
    } catch (err) {
      toast.error(err?.response?.data?.detail || "Could not pin token");
    } finally { setBusy(false); }
  };
  if (!open) {
    return (
      <button type="button" data-testid="ladder-add-open" onClick={() => setOpen(true)} disabled={count >= max}
        title={count >= max ? `manual list is full (${max})` : "Pin an established RH token: paste the contract address, optionally the pool quote (ETH, TSLA… or the pair-token address)"}
        className="inline-flex items-center gap-1 px-2 py-0.5 border border-fuchsia-800 text-fuchsia-300 hover:bg-fuchsia-950/40 uppercase tracking-[0.15em] text-[10px] disabled:opacity-50">
        <Plus className="w-3 h-3" /> add rh token <span className="text-neutral-500 tracking-normal">{count}/{max}</span>
      </button>
    );
  }
  return (
    <form onSubmit={submit} data-testid="ladder-add-form" className="flex items-center gap-1.5 flex-wrap">
      <input data-testid="ladder-add-address" value={address} onChange={(e) => setAddress(e.target.value)} placeholder="0x… token contract" spellCheck={false} autoFocus
        className={`w-[340px] bg-neutral-950 border px-2 py-0.5 font-mono text-[11px] text-neutral-100 outline-none ${address && !valid ? "border-red-800" : "border-neutral-700 focus:border-fuchsia-700"}`} />
      <input data-testid="ladder-add-quote" value={quote} onChange={(e) => setQuote(e.target.value)} placeholder="pool quote (optional: ETH · TSLA · 0x…)" spellCheck={false}
        className="w-[240px] bg-neutral-950 border border-neutral-700 focus:border-fuchsia-700 px-2 py-0.5 font-mono text-[11px] text-neutral-100 outline-none" />
      <button type="submit" data-testid="ladder-add-submit" disabled={!valid || busy}
        className="inline-flex items-center gap-1 px-2 py-0.5 border border-fuchsia-700 text-fuchsia-200 hover:bg-fuchsia-950/40 uppercase tracking-[0.15em] text-[10px] disabled:opacity-40">
        {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Plus className="w-3 h-3" />} {busy ? "finding pool" : "pin"}
      </button>
      <button type="button" data-testid="ladder-add-cancel" onClick={() => setOpen(false)} className="px-2 py-0.5 text-[10px] text-neutral-500 hover:text-neutral-200 uppercase tracking-[0.15em]">cancel</button>
      <span className="text-[9px] font-mono text-neutral-600 basis-full">no age gate · no MC gate · staircase, adds, trails and re-entry exactly like a graduate · marked stale when quiet, never dropped</span>
    </form>
  );
}
