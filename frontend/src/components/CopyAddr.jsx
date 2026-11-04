import { useRef } from "react";
import { toast } from "sonner";
import { copyToClipboard } from "@/lib/clipboard";

export const shortAddr = (s) => (s ? `${s.slice(0, 4)}…${s.slice(-4)}` : "—");

// Click (or long-press on touch) copies the full address; the click never bubbles to the row behind it.
export default function CopyAddr({ value, label, className = "", children, testid }) {
  const timer = useRef(null);
  const fired = useRef(false);
  if (!value) return <span className={className}>—</span>;
  const copy = async (e) => {
    e?.stopPropagation?.();
    e?.preventDefault?.();
    const ok = await copyToClipboard(value);
    ok ? toast.success(`${label || "Copied"}: ${shortAddr(value)}`) : toast.error("Copy failed — long-press and copy manually");
  };
  const start = () => { fired.current = false; timer.current = setTimeout(() => { fired.current = true; copy(); }, 450); };
  const end = (e) => { clearTimeout(timer.current); if (fired.current) { e.stopPropagation(); e.preventDefault(); } };
  return (
    <span role="button" tabIndex={0} title={`${value}\nclick / long-press to copy`}
      onClick={copy} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && copy(e)}
      onTouchStart={start} onTouchEnd={end} onTouchCancel={end} onContextMenu={(e) => e.preventDefault()}
      className={`cursor-copy select-none underline decoration-dotted decoration-neutral-700 underline-offset-2 hover:text-emerald-300 hover:decoration-emerald-600 transition-colors ${className}`}
      data-testid={testid || `copy-${value.slice(0, 6)}`}>
      {children ?? shortAddr(value)}
    </span>
  );
}
