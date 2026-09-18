import { useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";

/** One-line explanation always visible; the long form mounts only while open. */
export default function Explain({ short, children, testid, className = "" }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`text-[10px] font-mono text-neutral-500 leading-relaxed ${className}`} data-testid={testid}>
      <span>{short}</span>
      {children && (
        <button type="button" onClick={() => setOpen((o) => !o)} data-testid={testid ? `${testid}-toggle` : undefined}
          className="ml-1.5 inline-flex items-center gap-0.5 text-neutral-400 hover:text-emerald-300 transition-colors duration-100 align-baseline">
          {open ? "less" : "more"}{open ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        </button>
      )}
      {open && <div className="mt-1.5 pl-2 border-l border-neutral-800 text-neutral-400 whitespace-pre-wrap" data-testid={testid ? `${testid}-long` : undefined}>{children}</div>}
    </div>
  );
}
