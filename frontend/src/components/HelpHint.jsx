import { useEffect, useRef, useState } from "react";
import { HelpCircle, BookOpen } from "lucide-react";
import { Popover, PopoverTrigger, PopoverContent } from "@/components/ui/popover";
import { openWiki } from "@/lib/wikiNav";

/**
 * HelpHint — inline "?" help. Hover opens it on desktop; a tap (or a long-press on the label it sits next to, via
 * `longPressRef`) opens it on phones, where hover tooltips never fire. Every hint ends with a link into the WIKI tab:
 * `wiki="gates-sol"` jumps to that section (optionally `wikiTerm` highlights a row); without it the wiki opens with a
 * search for `label`.
 */
export default function HelpHint({ children, label = "help", side = "top", className = "", wiki, wikiTerm, longPressRef }) {
  const [open, setOpen] = useState(false);
  const pinned = useRef(false);
  const closeTimer = useRef(null);
  const hoverOpen = () => { clearTimeout(closeTimer.current); setOpen(true); };
  const hoverClose = () => { if (!pinned.current) closeTimer.current = setTimeout(() => setOpen(false), 120); };
  const toggle = (e) => { e.preventDefault(); e.stopPropagation(); pinned.current = !open; setOpen(!open); };

  useEffect(() => {
    const el = longPressRef?.current;
    if (!el) return undefined;
    let timer = null;
    const start = () => { timer = setTimeout(() => { pinned.current = true; setOpen(true); }, 450); };
    const cancel = () => clearTimeout(timer);
    el.addEventListener("touchstart", start, { passive: true });
    el.addEventListener("touchend", cancel);
    el.addEventListener("touchmove", cancel);
    el.addEventListener("touchcancel", cancel);
    return () => { cancel(); el.removeEventListener("touchstart", start); el.removeEventListener("touchend", cancel); el.removeEventListener("touchmove", cancel); el.removeEventListener("touchcancel", cancel); };
  }, [longPressRef]);

  const goWiki = (e) => {
    e.preventDefault(); e.stopPropagation();
    setOpen(false); pinned.current = false;
    if (wiki) openWiki(wiki, wikiTerm); else openWiki(null, label.replace(/^help:\s*/i, ""));
  };

  return (
    <Popover open={open} onOpenChange={(o) => { setOpen(o); if (!o) pinned.current = false; }}>
      <PopoverTrigger asChild>
        <span role="button" tabIndex={0} aria-label={label} data-testid={`help-${label.replace(/^help:\s*/i, "").toLowerCase().replace(/[^a-z0-9]+/g, "-")}`}
          onClick={toggle} onMouseEnter={hoverOpen} onMouseLeave={hoverClose}
          className={`inline-flex items-center align-middle text-neutral-600 hover:text-neutral-300 transition-colors cursor-help ${className}`}>
          <HelpCircle className="w-3 h-3" strokeWidth={2} />
        </span>
      </PopoverTrigger>
      <PopoverContent side={side} onMouseEnter={hoverOpen} onMouseLeave={hoverClose} onClick={(e) => e.stopPropagation()}
        className="max-w-[300px] w-auto bg-neutral-900 border border-neutral-700 text-neutral-200 font-mono text-[11px] leading-relaxed px-2.5 py-2 whitespace-normal shadow-xl">
        <div>{children}</div>
        <button type="button" onClick={goWiki} data-testid="help-wiki-link"
          className="mt-2 inline-flex items-center gap-1 text-[10px] uppercase tracking-[0.15em] text-emerald-300 hover:text-emerald-100 border-t border-neutral-800 pt-1.5 w-full transition-colors duration-100">
          <BookOpen className="w-3 h-3" /> full description in the wiki →
        </button>
      </PopoverContent>
    </Popover>
  );
}
