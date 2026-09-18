import { useCallback, useRef, useState } from "react";

const MAX_ITEMS = 40;
const WINDOW_MS = 10 * 60 * 1000;
const DEDUP_MS = 60 * 1000;

/** Rolling feed of gate refusals for the footer ticker: scanner_skip events + candidate gate verdicts (both chains). */
export function useSkipFeed() {
  const [items, setItems] = useState([]);
  const lastSeen = useRef(new Map());     // `${mint}:${reason}` -> ts
  const tsLog = useRef([]);               // timestamps for the 10-minute counter
  const [count10m, setCount10m] = useState(0);

  const push = useCallback((mint, reason, extra = {}) => {
    if (!mint || !reason || reason === "pass" || reason === "tracking") return;
    const now = Date.now();
    const k = `${mint}:${reason}`;
    const prev = lastSeen.current.get(k);
    if (prev && now - prev < DEDUP_MS) return;
    lastSeen.current.set(k, now);
    if (lastSeen.current.size > 500) {
      for (const [kk, t] of lastSeen.current) if (now - t > WINDOW_MS) lastSeen.current.delete(kk);
    }
    tsLog.current.push(now);
    tsLog.current = tsLog.current.filter((t) => now - t < WINDOW_MS);
    setCount10m(tsLog.current.length);
    setItems((prev) => [{ key: `${k}:${now}`, mint, reason: String(reason), ts: now, chain: extra.chain || "sol", symbol: extra.symbol || null, detail: extra.detail || null },
      ...prev].slice(0, MAX_ITEMS));
  }, []);

  /** Wire into the Dashboard WS switch. `resolve(idOrMint)` maps a launch id / mint to {mint, symbol, chain}. */
  const onEvent = useCallback((type, data, resolve) => {
    if (type === "scanner_skip") {
      const meta = resolve(data.mint) || {};
      const d = data.details;
      push(data.mint, data.reason, { chain: data.chain || meta.chain || "sol", symbol: data.symbol || meta.symbol,
        detail: Array.isArray(d) ? d.filter(Boolean).join("; ") : d });
    } else if (type === "candidate" || type === "candidate_update") {
      const meta = resolve(data.id) || resolve(data.mint) || {};
      const mint = data.mint || meta.mint;
      const reason = data.rh_gate ?? data.gate;
      if (reason && reason !== "pass") push(mint, reason, { chain: data.chain || meta.chain, symbol: data.symbol || meta.symbol, detail: data.rh_gate_detail || data.gate_detail });
    }
  }, [push]);

  /** First paint: show the verdicts already stamped on the launch feed so the tape is never blank after a refresh (not counted). */
  const seeded = useRef(false);
  const seed = useCallback((launches) => {
    if (seeded.current || !launches?.length) return;
    seeded.current = true;
    const now = Date.now();
    const rows = launches.filter((l) => { const r = l.rh_gate ?? l.gate; return r && r !== "pass"; }).slice(0, 20)
      .map((l) => ({ key: `seed:${l.mint}`, mint: l.mint, reason: String(l.rh_gate ?? l.gate), ts: now, seeded: true, chain: l.chain || "sol", symbol: l.symbol, detail: l.rh_gate_detail || l.gate_detail || null }));
    if (rows.length) setItems((prev) => (prev.length ? prev : rows));
  }, []);

  return { items, count10m, onEvent, seed };
}
