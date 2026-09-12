import { useState } from "react";
import { RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

// Retire the Solana hot key (it was once committed to git) and hot-swap a fresh one. Backend refuses while
// live trading is on or live positions are open; sweeps the old key's SOL into the new address.
export default function RotateWallet({ onDone }) {
  const [arm, setArm] = useState(false);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      const r = await api.walletRotate();
      toast.success(`Wallet rotated → ${r.new_public_key.slice(0, 6)}… swept ${(r.swept.lamports / 1e9).toFixed(4)} SOL${r.swept.error ? ` (sweep error: ${r.swept.error})` : ""}`, { duration: 12000 });
      setArm(false);
      onDone && onDone();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message, { duration: 8000 });
    } finally {
      setBusy(false);
    }
  };
  return (
    <span className="inline-flex items-center gap-1" data-testid="rotate-wallet">
      {!arm ? (
        <button type="button" onClick={() => setArm(true)} data-testid="rotate-wallet-arm"
          className="text-[10px] uppercase tracking-wider text-neutral-500 hover:text-amber-300 inline-flex items-center gap-1">
          <RefreshCw className="w-3 h-3" /> rotate key
        </button>
      ) : (
        <>
          <span className="text-[10px] text-amber-300">paper only · sweeps SOL to the new key · tokens stay on the retired key file</span>
          <button type="button" onClick={run} disabled={busy} data-testid="rotate-wallet-confirm"
            className="px-2 py-0.5 border border-amber-700 text-amber-300 hover:bg-amber-950/60 disabled:opacity-40 uppercase text-[10px]">
            {busy ? "rotating…" : "rotate now"}
          </button>
          <button type="button" onClick={() => setArm(false)} className="text-[10px] text-neutral-500 hover:text-neutral-300" data-testid="rotate-wallet-cancel">cancel</button>
        </>
      )}
    </span>
  );
}
