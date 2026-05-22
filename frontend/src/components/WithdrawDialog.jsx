import { useState } from "react";
import { Send, X, AlertTriangle } from "lucide-react";
import { api } from "@/lib/api";
import { toast } from "sonner";

export default function WithdrawDialog({ open, onClose, balance, onSuccess }) {
  const [to, setTo] = useState("");
  const [amount, setAmount] = useState("");
  const [sending, setSending] = useState(false);
  const [confirmed, setConfirmed] = useState(false);

  if (!open) return null;

  const reset = () => {
    setTo(""); setAmount(""); setConfirmed(false); setSending(false);
  };

  const close = () => { reset(); onClose(); };

  const submit = async () => {
    const amt = parseFloat(amount);
    if (!to || !amt || amt <= 0) {
      toast.error("Enter valid recipient address & amount");
      return;
    }
    if (!confirmed) {
      toast.error("Please confirm the destination address");
      return;
    }
    setSending(true);
    try {
      const res = await api.sendSol(to, amt);
      toast.success(`Sent ${amt} SOL · sig ${res.signature?.slice(0, 8)}…`);
      onSuccess && onSuccess(res);
      close();
    } catch (e) {
      const msg = e?.response?.data?.detail || e.message || "send failed";
      toast.error(msg);
    } finally {
      setSending(false);
    }
  };

  const setMax = () => {
    // Leave ~0.005 SOL for fees
    const max = Math.max(0, (balance || 0) - 0.005);
    setAmount(max.toFixed(6));
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4" data-testid="withdraw-dialog">
      <div className="w-full max-w-md control-card relative">
        <button
          onClick={close}
          className="absolute top-3 right-3 text-neutral-500 hover:text-neutral-200"
          data-testid="withdraw-close-btn"
        >
          <X className="w-4 h-4" />
        </button>
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500 mb-3">
          <Send className="w-3 h-3" /> Withdraw SOL
        </div>

        <div className="border border-amber-800/50 bg-amber-950/30 px-3 py-2 mb-3 flex gap-2">
          <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
          <p className="text-[11px] text-amber-300 leading-relaxed">
            This signs a real Solana transaction. Double-check the destination — sends are irreversible.
          </p>
        </div>

        <label className="block text-[10px] uppercase tracking-[0.15em] text-neutral-500 mb-1">
          Recipient address
        </label>
        <input
          data-testid="withdraw-to-input"
          type="text"
          value={to}
          onChange={(e) => setTo(e.target.value)}
          placeholder="Solana address (base58)"
          className="w-full bg-neutral-950 border border-neutral-800 px-2 py-1.5 font-mono text-xs focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 mb-3"
        />

        <div className="flex items-center justify-between mb-1">
          <label className="text-[10px] uppercase tracking-[0.15em] text-neutral-500">Amount (SOL)</label>
          <button
            onClick={setMax}
            data-testid="withdraw-max-btn"
            className="text-[10px] font-mono text-blue-400 hover:text-blue-300 uppercase"
          >
            max (bal {balance?.toFixed(4) ?? "—"})
          </button>
        </div>
        <input
          data-testid="withdraw-amount-input"
          type="number"
          step="0.0001"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          placeholder="0.0"
          className="w-full bg-neutral-950 border border-neutral-800 px-2 py-1.5 font-mono text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 mb-3"
        />

        <label className="flex items-start gap-2 text-[11px] text-neutral-400 mb-4">
          <input
            type="checkbox"
            data-testid="withdraw-confirm-checkbox"
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
            className="mt-0.5"
          />
          <span>I have verified the destination address above and accept this transaction is irreversible.</span>
        </label>

        <div className="flex gap-2">
          <button
            onClick={close}
            data-testid="withdraw-cancel-btn"
            className="flex-1 px-3 py-2 border border-neutral-700 text-neutral-300 hover:bg-neutral-900 font-mono text-xs uppercase tracking-[0.15em]"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={sending || !confirmed || !to || !amount}
            data-testid="withdraw-submit-btn"
            className="flex-1 px-3 py-2 border border-blue-700 text-blue-200 bg-blue-950 hover:bg-blue-900 font-mono text-xs uppercase tracking-[0.15em] disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {sending ? "Sending…" : "Send SOL"}
          </button>
        </div>
      </div>
    </div>
  );
}
