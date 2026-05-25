import { useState } from "react";
import { Key, X, Eye, EyeOff, Copy, Check, AlertTriangle } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { copyToClipboard } from "@/lib/clipboard";

/**
 * RevealPrivateKey — danger-gated dialog that exposes the bot wallet's
 * private key (base58 + JSON-array forms). The user can copy either to
 * import the wallet into Phantom / Solflare / a CLI signer and recover
 * stranded tokens manually when the in-bot path can't land a tx.
 *
 * Two-step confirmation pattern: the trigger button reveals an inline
 * warning + a 6-second hold-to-reveal action. After fetch, the key is
 * masked by default; the user clicks the eye icon to actually see it.
 */
export default function RevealPrivateKey() {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [secrets, setSecrets] = useState(null);
  const [showB58, setShowB58] = useState(false);
  const [showJson, setShowJson] = useState(false);
  const [copiedB58, setCopiedB58] = useState(false);
  const [copiedJson, setCopiedJson] = useState(false);

  const reset = () => {
    setSecrets(null);
    setShowB58(false);
    setShowJson(false);
    setCopiedB58(false);
    setCopiedJson(false);
  };

  const close = () => {
    setOpen(false);
    reset();
  };

  const fetchKey = async () => {
    const ok = window.confirm(
      "REVEAL PRIVATE KEY?\n\n" +
        "Anyone with this key can drain your wallet. By continuing:\n" +
        "  • The key will display in your browser\n" +
        "  • Do not screenshot, paste in chat, or commit to git\n" +
        "  • Close this card when done\n\n" +
        "Continue?",
    );
    if (!ok) return;
    setLoading(true);
    try {
      const d = await api.walletExportPrivateKey();
      setSecrets(d);
    } catch (e) {
      toast.error(`Export failed: ${e?.response?.data?.detail || e.message}`);
    } finally {
      setLoading(false);
    }
  };

  const copyB58 = async () => {
    if (!secrets?.secret_key_b58) return;
    const ok = await copyToClipboard(secrets.secret_key_b58);
    if (ok) {
      setCopiedB58(true);
      toast.success("Base58 key copied — paste into Phantom / Solflare import");
      setTimeout(() => setCopiedB58(false), 2000);
    } else {
      toast.error("Clipboard blocked — long-press the key to select & copy manually");
    }
  };

  const copyJson = async () => {
    if (!secrets?.secret_key_json_array) return;
    const json = JSON.stringify(secrets.secret_key_json_array);
    const ok = await copyToClipboard(json);
    if (ok) {
      setCopiedJson(true);
      toast.success("JSON-array key copied — paste into a solana-keygen file");
      setTimeout(() => setCopiedJson(false), 2000);
    } else {
      toast.error("Clipboard blocked — long-press the JSON to select & copy manually");
    }
  };

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        data-testid="reveal-privkey-btn"
        className="w-full text-[10px] uppercase tracking-[0.2em] text-neutral-500 hover:text-amber-400 flex items-center justify-center gap-2 py-1 border border-dashed border-neutral-800 hover:border-amber-900 transition"
      >
        <Key className="w-3 h-3" />
        Export private key (manual recovery)
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4"
          data-testid="reveal-privkey-dialog"
          onClick={close}
        >
          <div
            className="w-full max-w-lg border border-red-900/60 bg-neutral-950 p-4 space-y-3"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-neutral-800 pb-2">
              <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.2em] text-red-400">
                <AlertTriangle className="w-3.5 h-3.5" />
                Wallet private key
              </div>
              <button
                type="button"
                onClick={close}
                data-testid="reveal-privkey-close"
                className="text-neutral-500 hover:text-neutral-200 transition"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {!secrets ? (
              <div className="space-y-3 py-2">
                <p className="text-[12px] text-neutral-300 leading-relaxed">
                  Export the bot wallet's private key so you can import it into{" "}
                  <span className="text-emerald-300">Phantom</span>,{" "}
                  <span className="text-emerald-300">Solflare</span>, or a Solana CLI signer
                  and manually sell any tokens the bot couldn't unstick.
                </p>
                <div className="border border-red-900/40 bg-red-950/30 px-3 py-2 text-[11px] text-red-300 font-mono leading-relaxed">
                  <span className="font-bold uppercase">Critical:</span> Anyone with this key
                  can drain every SOL and token in this wallet. Don't screenshot it,
                  paste it in chat, or commit it to git. Close the card when you're done.
                </div>
                <button
                  type="button"
                  onClick={fetchKey}
                  disabled={loading}
                  data-testid="reveal-privkey-fetch"
                  className="w-full px-3 py-2 border border-red-700 text-red-300 bg-red-950 hover:bg-red-900 font-mono text-[11px] uppercase tracking-[0.2em] disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {loading ? "Fetching…" : "I understand — reveal the key"}
                </button>
              </div>
            ) : (
              <div className="space-y-3 py-1">
                <div>
                  <div className="text-[9px] uppercase tracking-[0.15em] text-neutral-500 mb-1">
                    Public key (safe to share)
                  </div>
                  <code
                    className="block text-[10px] font-mono text-neutral-300 break-all bg-neutral-900 border border-neutral-800 px-2 py-1.5 select-all"
                    data-testid="reveal-privkey-pubkey"
                  >
                    {secrets.public_key}
                  </code>
                </div>

                <KeyRow
                  label="Base58 (Phantom / Solflare import)"
                  value={secrets.secret_key_b58}
                  show={showB58}
                  onToggle={() => setShowB58((v) => !v)}
                  onCopy={copyB58}
                  copied={copiedB58}
                  testidPrefix="privkey-b58"
                />

                <KeyRow
                  label="JSON array (solana-keygen / CLI signer)"
                  value={JSON.stringify(secrets.secret_key_json_array)}
                  show={showJson}
                  onToggle={() => setShowJson((v) => !v)}
                  onCopy={copyJson}
                  copied={copiedJson}
                  testidPrefix="privkey-json"
                />

                <div className="text-[10px] text-neutral-500 font-mono leading-relaxed pt-1">
                  <span className="text-amber-400">→</span> In Phantom: ⋮ menu → Add / Connect Wallet → Import private key → paste the Base58 string.
                </div>
                <div className="text-[10px] text-neutral-500 font-mono leading-relaxed">
                  <span className="text-amber-400">→</span> In CLI: save the JSON array to <code className="text-neutral-300">~/.config/solana/id.json</code> then{" "}
                  <code className="text-neutral-300">solana config set --keypair ~/.config/solana/id.json</code>.
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}

function KeyRow({ label, value, show, onToggle, onCopy, copied, testidPrefix }) {
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-[9px] uppercase tracking-[0.15em] text-neutral-500">{label}</span>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={onToggle}
            data-testid={`${testidPrefix}-toggle`}
            className="text-neutral-500 hover:text-neutral-200 transition"
            title={show ? "Hide" : "Reveal"}
          >
            {show ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
          </button>
          <button
            type="button"
            onClick={onCopy}
            data-testid={`${testidPrefix}-copy`}
            className="text-neutral-500 hover:text-neutral-200 transition"
            title="Copy"
          >
            {copied ? <Check className="w-3 h-3 text-emerald-500" /> : <Copy className="w-3 h-3" />}
          </button>
        </div>
      </div>
      <code
        className={`block text-[10px] font-mono break-all bg-neutral-900 border border-red-900/40 px-2 py-1.5 select-all ${show ? "text-red-200" : "text-neutral-700 blur-sm select-none"}`}
        data-testid={`${testidPrefix}-value`}
      >
        {show ? value : "•".repeat(64)}
      </code>
    </div>
  );
}
