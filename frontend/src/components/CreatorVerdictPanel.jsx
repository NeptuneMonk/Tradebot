import { useEffect, useState } from "react";
import { ShieldAlert, ShieldCheck, ShieldQuestion, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";

const TONE = {
  block: "border-red-800 text-red-300 bg-red-950/40",
  caution: "border-amber-700 text-amber-300 bg-amber-950/40",
  neutral: "border-neutral-700 text-neutral-400 bg-neutral-900/40",
  favor: "border-emerald-700 text-emerald-300 bg-emerald-950/40",
};

export function VerdictChip({ level, reasons, mint }) {
  if (!level) return null;
  const Icon = level === "block" ? ShieldAlert : level === "favor" ? ShieldCheck : ShieldQuestion;
  return (
    <span data-testid={`creator-verdict-${mint}`} title={(reasons || []).join("\n") || level}
          className={`inline-flex items-center gap-1 text-[9px] font-mono uppercase tracking-[0.15em] px-1.5 py-0.5 border ${TONE[level] || TONE.neutral}`}>
      <Icon className="w-3 h-3" /> creator {level}
    </span>
  );
}

const KNOBS = [
  ["creator_min_trades_for_rate", "Judge after N own trades", "int", 1],
  ["creator_min_win_rate_pct", "Min win rate % (else block)", "float", 5],
  ["creator_caution_size_mult", "Caution size ×", "float", 0.1],
  ["creator_caution_smart_override_n", "Smart wallets to lift caution", "int", 1],
  ["creator_dev_out_pct", "Dev already out ≥ %", "float", 5],
];

export default function CreatorVerdictPanel({ config, onConfigUpdate }) {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = async () => {
    setBusy(true);
    try { setData(await api.creatorVerdictRecent()); } catch { /* keep last */ } finally { setBusy(false); }
  };
  useEffect(() => { load(); const id = setInterval(load, 15000); return () => clearInterval(id); }, []);
  const set = (k, v) => onConfigUpdate && onConfigUpdate({ [k]: v });
  const items = data?.items || [];
  const blocked = data?.blocked || [];
  return (
    <div className="flex flex-col gap-3" data-testid="creator-verdict-panel">
      <div className="flex items-center justify-between">
        <div className="text-[10px] uppercase tracking-[0.2em] text-neutral-500">
          Creator verdict · one gate for every entry path · {items.length} recent · <span className="text-red-300">{blocked.length} blocked</span>
        </div>
        <button data-testid="creator-verdict-refresh" onClick={load} disabled={busy} className="text-neutral-500 hover:text-neutral-200"><RefreshCw className={`w-3 h-3 ${busy ? "animate-spin" : ""}`} /></button>
      </div>
      <p className="font-mono text-[10px] text-neutral-500 leading-relaxed">
        Greylist blacklist / rug patterns, our own win rate on the creator and a failed audit feed ONE verdict. After N closed trades with a creator, a win rate below your threshold is a <span className="text-red-300">block</span>; at or above it, a <span className="text-emerald-300">favor</span>. Any dev selling inside the launch window is a plus (sold tokens can't be dumped later); fully out ≥ N% → favor.
        BLOCK refuses scanner, smart-wallet, re-entry and add-on buys; manual buys show the reasons and need <span className="text-neutral-300">force</span>.
        A smart wallet <span className="text-neutral-300">you added</span> can turn a BLOCK into CAUTION (learned ones never); CAUTION is half size unless ≥ N smart wallets bought.
      </p>
      <div className="grid grid-cols-2 md:grid-cols-6 gap-2">
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-[0.12em] text-neutral-500">Gate</span>
          <select data-testid="creator-verdict-enabled" value={config?.creator_verdict_enabled === false ? "off" : "on"} onChange={(e) => set("creator_verdict_enabled", e.target.value === "on")}
                  className="bg-neutral-950 border border-neutral-800 px-2 py-1 font-mono text-sm"><option value="on">on</option><option value="off">off</option></select>
        </label>
        {KNOBS.map(([k, label, type, step]) => (
          <label key={k} className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-[0.12em] text-neutral-500 truncate" title={label}>{label}</span>
            <input data-testid={`creator-verdict-${k.replace(/_/g, "-")}`} type="number" step={step} defaultValue={config?.[k] ?? ""} key={`${k}-${config?.[k]}`}
                   onBlur={(e) => { const v = type === "int" ? parseInt(e.target.value, 10) : parseFloat(e.target.value); if (!Number.isNaN(v) && v !== config?.[k]) set(k, v); }}
                   className="bg-neutral-950 border border-neutral-800 px-2 py-1 font-mono text-sm focus:border-blue-500 focus:outline-none" />
          </label>
        ))}
        <label className="flex flex-col gap-1 col-span-2 md:col-span-1">
          <span className="text-[10px] uppercase tracking-[0.12em] text-neutral-500">Manual smart wallet lifts block</span>
          <select data-testid="creator-verdict-manual-override" value={config?.creator_block_manual_smart_override === false ? "off" : "on"} onChange={(e) => set("creator_block_manual_smart_override", e.target.value === "on")}
                  className="bg-neutral-950 border border-neutral-800 px-2 py-1 font-mono text-sm"><option value="on">on</option><option value="off">off</option></select>
        </label>
      </div>
      <div className="max-h-72 overflow-y-auto border border-neutral-800/70" data-testid="creator-verdict-list">
        {items.length === 0 && <div className="p-3 font-mono text-[11px] text-neutral-600">no verdicts yet — they appear as candidates reach the entry gate</div>}
        {items.map((v) => (
          <div key={v.mint} className="flex items-start gap-2 px-2 py-1.5 border-b border-neutral-900 last:border-0" data-testid={`creator-verdict-row-${v.mint}`}>
            <VerdictChip level={v.level} reasons={v.reasons} mint={`row-${v.mint}`} />
            <div className="min-w-0 flex-1">
              <div className="font-mono text-[11px] text-neutral-200 truncate">{v.symbol || v.mint.slice(0, 8)} <span className="text-neutral-600">{v.mint.slice(0, 6)}…</span>
                {v.size_mult !== 1 && <span className="ml-2 text-amber-300">size ×{v.size_mult}</span>}
                {v.smart_total > 0 && <span className="ml-2 text-fuchsia-300">smart {v.smart_total}{v.smart_manual ? ` (${v.smart_manual} manual)` : ""}</span>}
              </div>
              <div className="font-mono text-[10px] text-neutral-500 truncate" title={(v.reasons || []).join("\n")}>{(v.reasons || []).join(" · ") || "—"}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
