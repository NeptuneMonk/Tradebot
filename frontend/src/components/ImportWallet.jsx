import { useState } from "react";
import { KeyRound } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

// Make an existing Phantom / Solflare / CLI key THE bot wallet. Backend refuses while live trading is on or
// live positions are open; the key is stored for every pod and survives redeploys.
export default function ImportWallet({ onDone }) {
  const [open, setOpen] = useState(false);
  const [secret, setSecret] = useState("");
  const [sweep, setSweep] = useState(false);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    if (!secret.trim()) return;
    if (!window.confirm("IMPORT THIS KEY AS THE BOT WALLET?\n\nThe bot will trade from this wallet from now on. The current key file is retired (kept on disk), the new key is stored for every pod. Paste only a key you control.")) return;
    setBusy(true);
    try {
      const r = await api.walletImport(secret.trim(), sweep);
      if (r.unchanged) toast.info("That key is already the bot wallet");
      else toast.success(`Wallet imported → ${r.public_key.slice(0, 6)}…${r.swept?.lamports ? ` · swept ${(r.swept.lamports / 1e9).toFixed(4)} SOL` : ""}${r.swept?.error ? ` (sweep error: ${r.swept.error})` : ""}`, { duration: 12000 });
      setSecret("");
      setOpen(false);
      onDone && onDone();
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message, { duration: 9000 });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-2" data-testid="import-wallet">
      <button type="button" onClick={() => setOpen((o) => !o)} data-testid="import-wallet-toggle"
              className="inline-flex items-center gap-1 text-[10px] uppercase tracking-[0.15em] text-neutral-500 hover:text-neutral-200">
        <KeyRound className="w-3 h-3" /> {open ? "cancel import" : "import key"}
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          <textarea
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            rows={2}
            placeholder="base58 secret (Phantom / Solflare export) or [1,2,3,…] JSON array"
            className="w-full bg-neutral-950 border border-neutral-800 px-2 py-1 font-mono text-[11px] text-neutral-200 focus:border-blue-500 focus:outline-none"
            data-testid="import-wallet-secret"
            autoComplete="off"
            spellCheck={false}
          />
          <label className="flex items-center gap-2 text-[11px] text-neutral-400">
            <input type="checkbox" checked={sweep} onChange={(e) => setSweep(e.target.checked)} data-testid="import-wallet-sweep" />
            sweep SOL from the current bot wallet into the imported one
          </label>
          <button type="button" onClick={run} disabled={busy || !secret.trim()} data-testid="import-wallet-confirm"
                  className="border border-blue-700/60 text-blue-300 px-2 py-0.5 uppercase tracking-[0.12em] text-[10px] hover:bg-blue-900/30 disabled:opacity-40 disabled:cursor-not-allowed">
            {busy ? "importing…" : "import now"}
          </button>
          <div className="text-[10px] text-neutral-600 leading-snug">Requires paper mode and no open live positions. You can still sell positions or close token accounts from Phantom / Pump.fun — the bot books external sells from chain.</div>
        </div>
      )}
    </div>
  );
}
