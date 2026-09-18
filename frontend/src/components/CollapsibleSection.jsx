import { useState, useEffect, useCallback } from "react";
import { ChevronDown } from "lucide-react";
import { MINIMIZE_ALL_EVENT } from "@/components/MinimizableCard";

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
    const onAll = (e) => {
      const next = !e.detail?.minimized;
      setOpen(next);
      if (next) setEverOpened(true);
    };
    window.addEventListener(MINIMIZE_ALL_EVENT, onAll);
    return () => window.removeEventListener(MINIMIZE_ALL_EVENT, onAll);
  }, []);

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
      className={`border border-neutral-800 bg-neutral-950 rounded-sm overflow-hidden ${open ? "" : "col-span-full"}`}
      data-testid={testId}
    >
      <button
        type="button"
        onClick={toggle}
        data-testid={testId ? `${testId}-toggle` : undefined}
        title={description || undefined}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-neutral-900/60 transition-colors duration-100"
      >
        <div className="flex items-center gap-2 min-w-0">
          <ChevronDown
            className={`w-4 h-4 text-neutral-500 transition-transform duration-150 ${open ? "rotate-0" : "-rotate-90"}`}
          />
          <span className="text-[11px] font-mono uppercase tracking-[0.2em] text-neutral-200 truncate">
            {title}
          </span>
          {badge != null && (
            <span className="text-[10px] font-mono px-1.5 py-0.5 border border-neutral-800 bg-neutral-900 text-neutral-400">
              {badge}
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
        <div className="border-t border-neutral-900 p-3">
          {children}
        </div>
      )}
    </section>
  );
}
