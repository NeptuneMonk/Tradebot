import { useState } from "react";
import { Copy, Wallet as WalletIcon, Check, Send } from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
import { toast } from "sonner";
import WithdrawDialog from "@/components/WithdrawDialog";
import StuckPositions from "@/components/StuckPositions";
import { copyToClipboard } from "@/lib/clipboard";

export default function WalletCard({ wallet }) {
  const [copied, setCopied] = useState(false);
  const [showQR, setShowQR] = useState(false);
  const [showWithdraw, setShowWithdraw] = useState(false);

  const copy = async () => {
    if (!wallet?.public_key) return;
    const ok = await copyToClipboard(wallet.public_key);
    if (ok) {
      setCopied(true);
      toast.success("Address copied");
      setTimeout(() => setCopied(false), 1500);
    } else {
      toast.error("Clipboard blocked — long-press the address to select & copy manually");
    }
  };

  return (
    <div className="control-card flex flex-col gap-3" data-testid="wallet-card">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
          <WalletIcon className="w-3 h-3" />
          Wallet
        </div>
        <div className="flex items-center gap-3 text-[10px] uppercase tracking-[0.15em]">
          <button
            onClick={() => setShowQR(s => !s)}
            className="text-neutral-500 hover:text-neutral-200 transition-colors duration-100"
            data-testid="toggle-qr-btn"
          >
            {showQR ? "Hide QR" : "Show QR"}
          </button>
          <button
            onClick={() => setShowWithdraw(true)}
            data-testid="open-withdraw-btn"
            disabled={!wallet || (wallet?.sol_balance ?? 0) <= 0}
            className="inline-flex items-center gap-1 text-blue-400 hover:text-blue-300 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Send className="w-3 h-3" /> Send
          </button>
        </div>
      </div>

      <div className="space-y-1">
        <div className="text-3xl font-mono font-semibold tracking-tight" data-testid="wallet-sol-balance">
          {wallet ? wallet.sol_balance.toFixed(4) : "—"} <span className="text-neutral-500 text-base">SOL</span>
        </div>
        <div className="text-xs font-mono text-neutral-400" data-testid="wallet-usd-balance">
          ${wallet ? wallet.usd_balance.toFixed(2) : "—"} <span className="text-neutral-600">@ ${wallet?.sol_price_usd?.toFixed(2) || "—"}/SOL</span>
        </div>
      </div>

      <div className="border-t border-neutral-800 pt-3">
        <div className="text-[10px] uppercase tracking-[0.2em] text-neutral-500 mb-1">Deposit address</div>
        <div className="flex items-center gap-2">
          <code
            className="text-[11px] font-mono text-neutral-300 break-all flex-1 select-all cursor-text"
            data-testid="wallet-public-key"
            onClick={(e) => {
              // Select all on tap/click for easy manual copy if needed
              const range = document.createRange();
              range.selectNodeContents(e.currentTarget);
              const sel = window.getSelection();
              sel.removeAllRanges();
              sel.addRange(range);
            }}
          >
            {wallet?.public_key || "loading..."}
          </code>
          <button
            onClick={copy}
            className="shrink-0 p-1.5 border border-neutral-800 hover:bg-neutral-800 transition-colors duration-100"
            data-testid="copy-address-btn"
          >
            {copied ? <Check className="w-3 h-3 text-emerald-500" /> : <Copy className="w-3 h-3 text-neutral-400" />}
          </button>
        </div>
        {showQR && wallet?.public_key && (
          <div className="mt-3 bg-white p-2 inline-block" data-testid="wallet-qr">
            <QRCodeSVG value={wallet.public_key} size={120} />
          </div>
        )}
      </div>

      <StuckPositions />

      <WithdrawDialog
        open={showWithdraw}
        onClose={() => setShowWithdraw(false)}
        balance={wallet?.sol_balance ?? 0}
      />
    </div>
  );
}
