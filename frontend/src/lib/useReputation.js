import { useEffect, useState } from "react";
import { api } from "@/lib/api";

// Dev-reputation badge cache: components ask for a mint, requests are coalesced into one /reputation/batch call per
// animation frame (max 40 mints). Results stay for 15 min; an empty {} from the server means the adapter is dark.
const cache = new Map();            // mint -> { tier, fake_chart, ok, at }
const listeners = new Map();        // mint -> Set<fn>
let pending = new Set();
let scheduled = false;
let dark = false;
const TTL_MS = 15 * 60_000;

function flush() {
  scheduled = false;
  const mints = [...pending].slice(0, 40);
  pending = new Set([...pending].slice(40));
  if (!mints.length) return;
  api.reputationBatch(mints).then((res) => {
    const d = res || {};
    if (!Object.keys(d).length && mints.length) dark = true;
    for (const m of mints) {
      const r = d[m] || { tier: "UNKNOWN", fake_chart: false, ok: false, dark: !Object.keys(d).length };
      cache.set(m, { ...r, at: Date.now() });
      (listeners.get(m) || []).forEach((fn) => fn(cache.get(m)));
    }
  }).catch(() => { for (const m of mints) (listeners.get(m) || []).forEach((fn) => fn(null)); });
  if (pending.size && !scheduled) { scheduled = true; requestAnimationFrame(flush); }
}

export function useReputation(mint) {
  const [rep, setRep] = useState(() => cache.get(mint) || null);
  useEffect(() => {
    if (!mint || mint.startsWith("0x") || dark) return undefined;
    const hit = cache.get(mint);
    if (hit && Date.now() - hit.at < TTL_MS) { setRep(hit); return undefined; }
    if (!listeners.has(mint)) listeners.set(mint, new Set());
    listeners.get(mint).add(setRep);
    pending.add(mint);
    if (!scheduled) { scheduled = true; requestAnimationFrame(flush); }
    return () => { listeners.get(mint)?.delete(setRep); };
  }, [mint]);
  return rep;
}

export const isRepDark = () => dark;
