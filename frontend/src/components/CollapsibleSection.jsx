import { useState, useEffect, useCallback } from "react";
import { ChevronDown } from "lucide-react";

/**
 * Lazy-loading collapsible wrapper for secondary dashboard panels.
 *
 * When `open` is false the children are NOT mounted at all (avoids the
 * polling/WebSocket-subscription/DOM cost). When toggled open the first
 * time, children mount and stay mounted until refresh — that way panels
 * preserve their internal state when collapsed/re-expanded.
 *
 * State is persisted to localStorage under `storageKey`. Pass `defaultOpen`
 * to set initial state when no prior preference exists.
 */
export default function CollapsibleSection({
  title,
  description,
  storageKey,
  defaultOpen = false,
  badge,
  testId,
  rightSlot,
  children,
}) {
  const readPref = useCallback(() => {
    if (!storageKey) return defaultOpen;
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw === null) return defaultOpen;
      return raw === "1";
    } catch {
      return defaultOpen;
    }
  }, [storageKey, defaultOpen]);

  const [open, setOpen] = useState(readPref);
  const [everOpened, setEverOpened] = useState(readPref);

  useEffect(() => {
    if (!storageKey) return;
    try { localStorage.setItem(storageKey, open ? "1" : "0"); } catch { /* ignore */ }
  }, [open, storageKey]);

  const toggle = () => {
    setOpen((p) => {
      const next = !p;
      if (next) setEverOpened(true);
      return next;
    });
  };

  return (
    <section
      className="border border-neutral-800 bg-neutral-950 rounded-sm overflow-hidden"
      data-testid={testId}
    >
      <button
        type="button"
        onClick={toggle}
        data-testid={testId ? `${testId}-toggle` : undefined}
        className="w-full flex items-center justify-between px-3 md:px-4 py-2.5 text-left hover:bg-neutral-900/60 transition-colors duration-100"
      >
        <div className="flex items-center gap-2 min-w-0">
          <ChevronDown
            className={`w-4 h-4 text-neutral-500 transition-transform duration-150 ${open ? "rotate-0" : "-rotate-90"}`}
          />
          <span className="text-xs font-mono uppercase tracking-[0.2em] text-neutral-300 truncate">
            {title}
          </span>
          {badge != null && (
            <span className="text-[10px] font-mono px-1.5 py-0.5 border border-neutral-800 bg-neutral-900 text-neutral-400">
              {badge}
            </span>
          )}
          {description && !open && (
            <span className="hidden md:inline text-[10px] text-neutral-600 font-mono tracking-wide truncate ml-2">
              {description}
            </span>
          )}
        </div>
        {rightSlot && (
          <span className="text-[10px] font-mono text-neutral-600 ml-2 flex-shrink-0">
            {rightSlot}
          </span>
        )}
      </button>
      {open && everOpened && (
        <div className="border-t border-neutral-900 p-3 md:p-4">
          {children}
        </div>
      )}
    </section>
  );
}
