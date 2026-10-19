import { useEffect, useState } from "react";
import { Magnet, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import { toast } from "sonner";

const short = (s) => (s ? `${s.slice(0, 4)}…${s.slice(-4)}` : "—");
const ago = (ts) => {
  if (!ts) return "—";
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
  return m < 60 ? `${m}m` : m < 1440 ? `${Math.round(m / 60)}h` : `${Math.round(m / 1440)}d`;
};

const KNOBS = [
  ["creator_smart_min_launches", "Launches with smart buys ≥", "int", 1],
  ["creator_smart_min_rate_pct", "Share of their launches ≥ %", "float", 5],
];

// Creators ranked by how often smart wallets buy their launches — the next launch by a "magnet" is favoured at t=0.
export default function SmartMagnetsPanel({ config, onConfigUpdate }) {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = async () => {
    setBusy(true);
    try { setData(await api.smartMagnets()); } catch { /* keep last */ } finally { setBusy(false); }
  };
  useEffect(() => { load(); const id = setInterval(load, 30000); return () => clearInterval(id); }, []);
  const set = async (k, v) => {
    try {
      const saved = await api.updateConfig({ [k]: v });
      onConfigUpdate && onConfigUpdate(saved);
      toast.success("Smart magnet setting saved");
    } catch {
      toast.error("Save failed");
    }
  };
  const items = data?.items || [];
  const magnets = items.filter((r) => r.magnet).length;
  return (
    <div className="flex flex-col gap-3 border-t border-neutral-800 pt-3" data-testid="smart-magnets-panel">
      <div className="flex items-center justify-between">
        <div className="text-[10px] uppercase tracking-[0.2em] text-neutral-500 flex items-center gap-1.5">
          <Magnet className="w-3 h-3 text-violet-300" /> Creator ↔ smart wallets · {items.length} tracked · <span className="text-violet-300">{magnets} magnet{magnets !== 1 ? "s" : ""}</span>
        </div>
        <button data-testid="smart-magnets-refresh" onClick={load} disabled={busy} className="text-neutral-500 hover:text-neutral-200"><RefreshCw className={`w-3 h-3 ${busy ? "animate-spin" : ""}`} /></button>
      </div>
      <p className="font-mono text-[10px] text-neutral-500 leading-relaxed">
        Every smart-wallet buy is credited to the token's creator. A creator whose launches smart wallets keep buying is a <span className="text-violet-300">magnet</span>:
        their next launch gets a <span className="text-emerald-300">favor</span> verdict and a <span className="text-violet-300">magnet n/m</span> chip at t=0 — before any smart wallet has had time to buy it.
      </p>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-[0.12em] text-neutral-500">Magnet favor</span>
          <select data-testid="smart-magnet-enabled" value={config?.creator_smart_magnet_enabled === false ? "off" : "on"} onChange={(e) => set("creator_smart_magnet_enabled", e.target.value === "on")}
                  className="bg-neutral-950 border border-neutral-800 px-2 py-1 font-mono text-sm"><option value="on">on</option><option value="off">off</option></select>
        </label>
        {KNOBS.map(([k, label, type, step]) => (
          <label key={k} className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-[0.12em] text-neutral-500 truncate" title={label}>{label}</span>
            <input data-testid={`smart-magnet-${k.replace(/_/g, "-")}`} type="number" step={step} defaultValue={config?.[k] ?? ""} key={`${k}-${config?.[k]}`}
                   onBlur={(e) => { const v = type === "int" ? parseInt(e.target.value, 10) : parseFloat(e.target.value); if (!Number.isNaN(v) && v !== config?.[k]) set(k, v); }}
                   className="bg-neutral-950 border border-neutral-800 px-2 py-1 font-mono text-sm focus:border-blue-500 focus:outline-none" />
          </label>
        ))}
      </div>
      <div className="max-h-72 overflow-y-auto border border-neutral-800/70" data-testid="smart-magnets-list">
        {items.length === 0 && <div className="p-3 font-mono text-[11px] text-neutral-600">nothing learned yet — appears as smart wallets buy launches</div>}
        {items.length > 0 && (
          <table className="w-full font-mono text-[10px]">
            <thead><tr className="text-neutral-500 uppercase tracking-[0.1em] text-left">
              <th className="px-2 py-1">creator</th><th className="text-right px-1">smart / launches</th><th className="text-right px-1">rate</th><th className="text-right px-1">buys</th>
              <th className="text-right px-1">grad / fail</th><th className="px-2">recurring wallets</th><th className="text-right px-2">last</th></tr></thead>
            <tbody>
              {items.map((r) => (
                <tr key={r.creator} className={`border-t border-neutral-900 ${r.magnet ? "bg-violet-950/20" : ""}`} data-testid={`smart-magnet-row-${r.creator}`}>
                  <td className="px-2 py-1 text-neutral-200">
                    {r.magnet && <span className="mr-1 px-1 border border-violet-700 text-violet-300 text-[9px] uppercase">magnet</span>}
                    {r.blacklisted && <span className="mr-1 px-1 border border-red-800 text-red-300 text-[9px] uppercase">blacklisted</span>}
                    <span title={r.creator}>{short(r.creator)}</span>{r.pattern && <span className="ml-1 text-neutral-500">{r.pattern}</span>}
                  </td>
                  <td className="text-right px-1 text-neutral-200">{r.smart_launches}/{r.launches}</td>
                  <td className={`text-right px-1 ${r.rate_pct >= 50 ? "text-emerald-300" : "text-neutral-400"}`}>{r.rate_pct}%</td>
                  <td className="text-right px-1 text-amber-300">{r.hits}{r.manual_hits ? <span className="text-neutral-500"> ({r.manual_hits}m)</span> : null}</td>
                  <td className="text-right px-1 text-neutral-400">{r.graduated} / {r.failed}</td>
                  <td className="px-2 text-neutral-400 truncate max-w-[220px]">{(r.recurring_wallets || []).map((w) => `${short(w.wallet)}×${w.hits}`).join(" ") || "—"}</td>
                  <td className="text-right px-2 text-neutral-500">{ago(r.last_smart_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
