import { memo, useEffect, useRef, useState } from "react";
import { ChainBadge } from "../ChainBadge";
import { RepBadge } from "../RepBadge";
import { TokenDetailDialog } from "../TokenDetailDialog";
import { api, explainApiError } from "@/lib/api";
import { toast } from "sonner";

const BAND_CLS = { new: "text-amber-300", seasoned: "text-cyan-300", rh_new: "text-lime-300" };
const BAND_LABEL = { new: "new", seasoned: "seasoned", rh_new: "rh pons" };
const WINDOW_MS = 10 * 60 * 1000;

/** Flatten /api/scanner/skips into reason → count (entry-path skips + pre-rank gates, both chains). */
function flatten(sk) {
  const out = {};
  const add = (obj) => Object.entries(obj || {}).forEach(([k, v]) => { if (k !== "pass" && typeof v === "number") out[k] = (out[k] || 0) + v; });
  ["seasoned", "new", "other"].forEach((b) => add(sk?.[b]));
  Object.values(sk?.prerank || {}).forEach(add);
  add(sk?.rh?.seasoned); add(sk?.rh?.curve);
  return out;
}

function useTopBlockers() {
  const [top, setTop] = useState({ rows: [], window: "start" });
  const hist = useRef([]);
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const cur = flatten(await api.scannerSkips());
        const now = Date.now();
        hist.current.push({ ts: now, cur });
        hist.current = hist.current.filter((h) => now - h.ts <= WINDOW_MS + 30000);
        const base = hist.current.find((h) => now - h.ts >= WINDOW_MS);
        const diff = Object.entries(cur).map(([k, v]) => [k, v - (base?.cur?.[k] || 0)]).filter(([, v]) => v > 0);
        const rows = (base ? diff : Object.entries(cur)).sort((a, b) => b[1] - a[1]).slice(0, 4);
        if (alive) setTop({ rows, window: base ? "10m" : "since start" });
      } catch { /* keep last */ }
    };
    tick();
    const id = setInterval(tick, 30000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  return top;
}

function Row({ c, onOpen }) {
  const growth = c.growth_pct ?? 0;
  const [buying, setBuying] = useState(false);
  const buyNow = async (e) => {
    e.stopPropagation();
    if (buying) return;
    setBuying(true);
    try {
      const r = await api.scannerManualBuy(c.mint);
      toast.success(`Bought ${r.symbol || c.symbol || c.mint.slice(0, 6)} · ${r.book || "rh_pons"} · ${r.mode || "paper"}${r.queued ? " · fill queued" : ""} — manual hold (R-only exit)`);
    } catch (err) {
      toast.error(`Entry refused — ${explainApiError(err)}`, { duration: 9000 });
    } finally { setBuying(false); }
  };
  return (
    <tr data-testid={`cockpit-cand-${c.mint}`} onClick={() => onOpen({ chain: c.chain || "sol", mint: c.mint, symbol: c.symbol, name: c.name })}
      className={`border-b border-neutral-900 cursor-pointer hover:bg-neutral-900/50 transition-colors duration-100 ${c.passes ? "" : "opacity-60"}`} title="Click for live market data, our record and a manual buy">
      <td className="py-1.5 pl-3"><span className={`font-mono text-[10px] uppercase tracking-[0.15em] ${BAND_CLS[c.band] || "text-neutral-500"}`}>{BAND_LABEL[c.band] || c.band}</span></td>
      <td className="font-mono text-xs"><ChainBadge chain={c.chain} mint={c.mint} /> <span className="text-neutral-100 ml-1">{c.symbol || "?"}</span> <RepBadge mint={c.mint} creator={c.creator} compact /></td>
      <td className="font-mono text-xs text-right"><span className={growth >= 0 ? "text-emerald-300" : "text-red-300"}>{growth >= 0 ? "+" : ""}{growth.toFixed(0)}%</span></td>
      <td className="text-right pr-3" data-testid={`cockpit-cand-gate-${c.mint}`}>
        {c.passes ? (
          <button type="button" onClick={buyNow} disabled={buying} data-testid={`cockpit-cand-buy-${c.mint}`}
            title={`gate ✓ — click to BUY NOW at standard sizing (${c.chain === "rh" ? "rh_pons" : "scalp"} book)${c.gate_detail ? `\n${c.gate_detail}` : ""}`}
            className="inline-flex items-center gap-1 px-1.5 py-0.5 border font-mono text-[10px] border-emerald-600 text-emerald-200 bg-emerald-950/40 hover:bg-emerald-800/50 disabled:opacity-50 transition-colors duration-100">
            {buying ? "buying…" : "gate ✓ · buy"}
          </button>
        ) : (
          <span className="inline-block px-1.5 py-0.5 border font-mono text-[10px] border-neutral-800 text-neutral-500" title={c.gate_detail || ""}>
            gate: {c.gate_reason || "watching"}
          </span>
        )}
      </td>
    </tr>
  );
}

function CompactCandidates({ candidates, limit = 10, scannerEnabled = true, onEnableScanner }) {
  const [detail, setDetail] = useState(null);
  const blockers = useTopBlockers();
  const sorted = [...(candidates || [])].sort((a, b) => (b.passes === true) - (a.passes === true) || (b.growth_pct ?? 0) - (a.growth_pct ?? 0));
  const passing = sorted.filter((c) => c.passes);
  const shown = passing.length ? passing.slice(0, limit) : sorted.slice(0, 5);
  return (
    <div className="control-card h-full flex flex-col !p-0" data-testid="cockpit-candidates">
      <div className="px-3 py-2 border-b border-neutral-800 text-[11px] font-mono tracking-[0.2em] text-neutral-200 flex items-center justify-between">
        <span>CANDIDATES <span className="text-neutral-500 tracking-normal">· {passing.length ? "passing" : "closest — none passing"}</span></span>
        <span className="text-neutral-500 tracking-normal">{sorted.length} tracked</span>
      </div>
      {!scannerEnabled && (
        <div className="px-3 py-1.5 border-b border-amber-900/60 bg-amber-950/30 text-[10px] font-mono text-amber-200 flex items-center justify-between gap-2" data-testid="scanner-off-banner">
          <span>SOL SCANNER OFF — gate ✓ rows are NOT auto-entered (Buy Now still works)</span>
          <button type="button" onClick={onEnableScanner} data-testid="scanner-off-enable-btn"
            className="px-1.5 py-0.5 border border-amber-600 text-amber-100 hover:bg-amber-800/50 transition-colors duration-100 uppercase tracking-[0.15em]">turn on</button>
        </div>
      )}
      <div className="flex-1 overflow-auto max-h-[280px]">
        {shown.length === 0 ? <div className="py-8 text-center text-[10px] uppercase tracking-[0.2em] text-neutral-600">scanner is warming up</div> : (
          <table className="w-full text-xs">
            <thead><tr className="text-[9px] uppercase tracking-[0.2em] text-neutral-500 border-b border-neutral-800">
              <th className="text-left py-1.5 pl-3 font-normal">book</th><th className="text-left font-normal">symbol</th><th className="text-right font-normal">growth</th><th className="text-right pr-3 font-normal">gate</th></tr></thead>
            <tbody>{shown.map((c) => <Row key={`${c.chain}-${c.mint}`} c={c} onOpen={setDetail} />)}</tbody>
          </table>
        )}
      </div>
      <div className="px-3 py-1.5 border-t border-neutral-800 font-mono text-[11px] flex items-center gap-2 whitespace-nowrap overflow-x-auto" data-testid="top-blockers"
        title="What refused entries most — entry-path skips + scanner pre-rank gates on both chains. Read this before loosening any gate.">
        <span className="text-neutral-500 tracking-[0.15em]">TOP BLOCKERS {blockers.window}:</span>
        {blockers.rows.length ? blockers.rows.map(([k, v], i) => <span key={k}>{i > 0 && <span className="text-neutral-700 mr-2">·</span>}<span className="text-amber-300">{k}</span> <span className="text-neutral-200">{v}</span></span>)
          : <span className="text-neutral-600">nothing refused yet</span>}
      </div>
      <TokenDetailDialog token={detail} onClose={() => setDetail(null)} />
    </div>
  );
}

export default memo(CompactCandidates);
