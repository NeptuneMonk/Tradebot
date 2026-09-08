import { useEffect, useState } from "react";
import { Minus, ChevronDown } from "lucide-react";

export const MINIMIZE_ALL_EVENT = "ui.minimize.all";
const EVT = MINIMIZE_ALL_EVENT;

function readPref(key) {
  try { return localStorage.getItem(`ui.min.${key}`) === "1"; } catch { return false; }
}

/** Minimize-all / expand-all broadcast used by the dashboard header. */
export function setAllMinimized(minimized) {
  window.dispatchEvent(new CustomEvent(EVT, { detail: { minimized } }));
}

/**
 * Wraps any dashboard window with a top-right minimize control. Minimized cards
 * collapse to a one-line strip: title + headline stat. Preference persists per card.
 */
export default function MinimizableCard({ id, title, stat, children }) {
  const [min, setMin] = useState(() => readPref(id));

  useEffect(() => {
    try { localStorage.setItem(`ui.min.${id}`, min ? "1" : "0"); } catch { /* ignore */ }
  }, [id, min]);

  useEffect(() => {
    const onAll = (e) => setMin(!!e.detail?.minimized);
    window.addEventListener(EVT, onAll);
    return () => window.removeEventListener(EVT, onAll);
  }, []);

  if (min) {
    return (
      <section className="col-span-full border border-neutral-800 bg-neutral-950 rounded-sm overflow-hidden" data-testid={`min-strip-wrap-${id}`}>
        <button
          type="button"
          onClick={() => setMin(false)}
          data-testid={`min-strip-${id}`}
          className="w-full flex items-center justify-between px-3 md:px-4 py-2.5 text-left hover:bg-neutral-900/60 transition-colors duration-100"
          title="Expand"
        >
          <span className="flex items-center gap-2 min-w-0">
            <ChevronDown className="w-4 h-4 text-neutral-500 -rotate-90 flex-shrink-0" />
            <span className="text-xs font-mono uppercase tracking-[0.2em] text-neutral-300 truncate">{title}</span>
            {stat != null && stat !== "" && (
              <span className="text-[10px] font-mono px-1.5 py-0.5 border border-neutral-800 bg-neutral-900 text-neutral-400 truncate" data-testid={`min-stat-${id}`}>{stat}</span>
            )}
          </span>
        </button>
      </section>
    );
  }

  return (
    <div className="relative group" data-testid={`min-wrap-${id}`}>
      <button
        type="button"
        onClick={() => setMin(true)}
        data-testid={`min-btn-${id}`}
        aria-label={`Minimize ${title}`}
        title="Minimize"
        className="absolute -top-2 -right-2 z-10 w-5 h-5 inline-flex items-center justify-center border border-neutral-700 bg-neutral-950 text-neutral-500 hover:text-neutral-100 hover:border-neutral-500 opacity-60 group-hover:opacity-100 transition-opacity duration-100"
      >
        <Minus className="w-3 h-3" />
      </button>
      {children}
    </div>
  );
}
