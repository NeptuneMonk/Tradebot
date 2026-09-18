import { memo, useEffect, useRef, useState } from "react";
import { TokenDetailDialog } from "../TokenDetailDialog";
import { SOL_GATE_HINT, RH_GATE_HINT } from "../RecentLaunchesFeed";

const BASE_SPEED = 110;   // px/s at an empty queue
const GAP = 56;           // px between signs
const TAPE_H = 36;

/** DSEG14 has no lowercase / brackets — keep the sign honest: A–Z 0–9 - . : space. */
const led = (s) => String(s || "").toUpperCase().split(" (")[0].replace(/[^A-Z0-9 \-.:]/g, "-").replace(/-+/g, "-").trim();

function Sign({ it, onOpen, register }) {
  const base = it.reason.split(":")[0].split(" (")[0];
  const hint = SOL_GATE_HINT[it.reason] || RH_GATE_HINT[it.reason] || SOL_GATE_HINT[base] || RH_GATE_HINT[base] || "";
  return (
    <button type="button" ref={(el) => register(it.key, el)} data-testid={`ticker-chip-${it.mint}`}
      onClick={() => onOpen({ chain: it.chain, mint: it.mint, symbol: it.symbol })}
      title={`${it.symbol || it.mint} · ${it.chain} · ${it.reason}${it.detail ? `\n${it.detail}` : ""}${hint ? `\n${hint}` : ""}\nclick for details`}
      className="led absolute top-0 h-full flex items-center whitespace-nowrap px-2 hover:bg-red-950/30 will-change-transform"
      style={{ transform: "translateX(100vw)", visibility: "hidden" }}>
      <span className="led-bright">{led(it.symbol || `${it.mint.slice(0, 4)}-${it.mint.slice(-4)}`)}</span>
      <span className="led-sep" aria-hidden />
      <span className="led-dim">{led(it.reason)}</span>
    </button>
  );
}

/** One-way conveyor: every skip enters from the right once, crosses, leaves on the left and never comes back. */
function SkipTicker({ items, count10m, live }) {
  const [detail, setDetail] = useState(null);
  const [active, setActive] = useState([]);           // signs currently on the tape (render order = spawn order)
  const seen = useRef(new Set());
  const queue = useRef([]);                            // waiting to enter, oldest first
  const pos = useRef(new Map());                       // key -> { x, w, el }
  const lastKey = useRef(null);
  const paused = useRef(false);
  const trackRef = useRef(null);
  const [idle, setIdle] = useState(true);
  const [queued, setQueued] = useState(0);

  useEffect(() => {
    const fresh = (items || []).filter((it) => !seen.current.has(it.key)).reverse();   // items are newest-first
    if (!fresh.length) return;
    fresh.forEach((it) => seen.current.add(it.key));
    queue.current.push(...fresh);
    if (queue.current.length > 120) queue.current.splice(0, queue.current.length - 120);
    setQueued(queue.current.length);
  }, [items]);

  const register = (key, el) => { const p = pos.current.get(key); if (p && el) p.el = el; };

  useEffect(() => {
    let raf, last = performance.now();
    const step = (now) => {
      const dt = Math.min(0.1, (now - last) / 1000); last = now;
      const W = trackRef.current?.clientWidth || 0;
      if (!paused.current && W) {
        const speed = BASE_SPEED * (1 + Math.min(1.5, queue.current.length / 12));
        let gone = false;
        for (const [key, p] of pos.current) {
          if (p.el && !p.w) { p.w = p.el.offsetWidth; p.el.style.visibility = "visible"; }
          p.x -= speed * dt;
          if (p.el) p.el.style.transform = `translateX(${p.x}px)`;
          if (p.w && p.x + p.w < 0) { pos.current.delete(key); gone = true; }
        }
        if (gone) setActive((a) => a.filter((it) => pos.current.has(it.key)));
        const tail = lastKey.current ? pos.current.get(lastKey.current) : null;
        const clear = !tail || (tail.w && tail.x + tail.w + GAP < W);
        if (clear && queue.current.length) {
          const it = queue.current.shift();
          pos.current.set(it.key, { x: W, w: 0, el: null });
          lastKey.current = it.key;
          setActive((a) => [...a, it]);
          setQueued(queue.current.length);
        }
        setIdle(pos.current.size === 0 && queue.current.length === 0);
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, []);

  return (
    <div data-testid="skip-ticker" className="fixed bottom-0 inset-x-0 z-30 border-t border-neutral-800 bg-black flex items-stretch select-none" style={{ height: TAPE_H }}
      onMouseEnter={() => { paused.current = true; }} onMouseLeave={() => { paused.current = false; }}>
      <div data-testid="skip-ticker-counter" className="flex items-center gap-2 px-3 border-r border-neutral-800 flex-shrink-0 bg-neutral-950"
        title={`${count10m} gate refusals in the last 10 minutes${queued ? ` · ${queued} waiting to scroll` : ""} — no movement here means no flow (or a dead feed)`}>
        <span className={`w-1.5 h-1.5 rounded-full ${live ? "bg-red-500 animate-pulse" : "bg-neutral-700"}`} />
        <span className="led led-bright text-[13px]">{String(count10m).padStart(4, "0")}</span>
        <span className="text-[9px] font-mono text-neutral-500 uppercase tracking-[0.15em]">skips / 10m</span>
      </div>
      <div ref={trackRef} className="flex-1 relative overflow-hidden led-tape" data-testid="skip-ticker-track">
        {idle && <div className="absolute inset-0 flex items-center px-4 led led-idle" data-testid="skip-ticker-idle">{live ? "WAITING FOR FLOW" : "BOT STOPPED"}</div>}
        {active.map((it) => <Sign key={it.key} it={it} onOpen={setDetail} register={register} />)}
      </div>
      <TokenDetailDialog token={detail} onClose={() => setDetail(null)} />
    </div>
  );
}

export default memo(SkipTicker);
