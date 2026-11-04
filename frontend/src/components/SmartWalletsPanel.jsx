import { memo, useCallback, useEffect, useState } from "react";
import { Crosshair, Plus, RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import CopyAddr from "./CopyAddr";

const ago = (ts) => {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`;
};

const MODES = [
  ["hard", "Hard trigger", "A smart buy inside the window fires a hunt entry immediately (safety gates still apply)."],
  ["soft", "Soft boost", "A smart buy relaxes the scanner gates and boosts the rank; the scanner still decides."],
  ["both", "Both", "Fire the hunt entry AND stamp the soft boost (helps when the hard path is refused)."],
];

const KNOBS = [
  ["smart_wallet_min_buy_sol", "Min buy (SOL)", 0.05, "A smart wallet's buy must be at least this big to count."],
  ["smart_wallet_max_per_hour", "Fires / hour", 1, "Rate cap on hard-trigger entries."],
  ["smart_wallet_park_s", "Park (s)", 30, "A smart buy the bot could not take (slots full, cap…) stays in the scanner's Smart picks strip with a buy button this long."],
  ["smart_wallet_hit_ttl_s", "Hit TTL (s)", 10, "How long a smart buy keeps boosting the scanner."],
  ["smart_wallet_soft_boost", "Rank boost", 5, "Rank points per fresh smart buy (soft mode)."],
  ["smart_wallet_gate_relax_pct", "Gate relax %", 5, "Growth / inflow / new-buyer gates are reduced by this % while a hit is fresh."],
  ["smart_wallet_learn_enabled", "Auto-learn", "bool", "Grade every launch's first buyers off the tape and promote repeat winners."],
  ["smart_wallet_learn_early_s", "Early window (s)", 10, "Buys inside the first N seconds of a launch are graded."],
  ["smart_wallet_learn_horizon_s", "Grade after (s)", 30, "A launch's early buyers are graded this long after launch."],
  ["smart_wallet_learn_min_gain_pct", "Win = +%", 5, "Post-buy peak at least this % above the buy price counts as a win (graduation always wins)."],
  ["smart_wallet_learn_min_wins", "Promote: wins", 1, "Minimum wins before a learned wallet becomes a trigger."],
  ["smart_wallet_learn_min_win_rate", "Promote: win rate", 0.05, "Minimum win rate (0–1) for promotion."],
];

function ModeRow({ config, onConfigUpdate }) {
  const set = async (patch) => {
    try {
      const updated = await api.updateConfig(patch);
      onConfigUpdate && onConfigUpdate(updated);
    } catch (e) { toast.error(`save failed: ${e.message || e}`); }
  };
  const on = config?.smart_wallet_enabled !== false;
  return (
    <div className="flex items-center gap-2 flex-wrap" data-testid="smart-wallets-mode-row">
      <button type="button" data-testid="smart-wallets-enabled" onClick={() => set({ smart_wallet_enabled: !on })}
        className={`text-[10px] font-mono uppercase tracking-[0.2em] px-2 py-1 border ${on ? "border-emerald-700 text-emerald-300" : "border-neutral-700 text-neutral-500"}`}>
        {on ? "ON" : "OFF"}
      </button>
      {MODES.map(([id, label, hint]) => (
        <button key={id} type="button" title={hint} data-testid={`smart-wallets-mode-${id}`} onClick={() => set({ smart_wallet_mode: id })}
          className={`text-[10px] font-mono px-2 py-1 border transition-colors duration-100 ${(config?.smart_wallet_mode || "both") === id ? "border-amber-600 text-amber-300 bg-amber-950/30" : "border-neutral-800 text-neutral-500 hover:text-neutral-200"}`}>
          {label}
        </button>
      ))}
    </div>
  );
}

function Knobs({ config, onConfigUpdate }) {
  const [local, setLocal] = useState({});
  useEffect(() => { setLocal({}); }, [config]);
  const val = (k) => (local[k] !== undefined ? local[k] : config?.[k]);
  const dirty = Object.keys(local).some((k) => local[k] !== config?.[k]);
  const save = async () => {
    try {
      const updated = await api.updateConfig(local);
      onConfigUpdate && onConfigUpdate(updated);
      setLocal({});
      toast.success("Smart-wallet knobs saved");
    } catch (e) { toast.error(`save failed: ${e.message || e}`); }
  };
  return (
    <div data-testid="smart-wallets-knobs">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-x-3 gap-y-2">
        {KNOBS.map(([k, label, step, hint]) => (
          <label key={k} className="flex flex-col gap-0.5" title={hint}>
            <span className="text-[9px] uppercase tracking-[0.15em] text-neutral-400">{label}</span>
            {step === "bool" ? (
              <button type="button" data-testid={`sw-${k}`} onClick={() => setLocal({ ...local, [k]: !val(k) })}
                className={`text-[11px] font-mono px-2 py-1 border text-left ${val(k) ? "border-emerald-700 text-emerald-300" : "border-neutral-700 text-neutral-500"}`}>
                {val(k) ? "ON" : "OFF"}
              </button>
            ) : (
              <input type="number" step={step} data-testid={`sw-${k}`} value={val(k) ?? ""}
                onChange={(e) => setLocal({ ...local, [k]: e.target.value === "" ? 0 : Number(e.target.value) })}
                className="bg-neutral-950 border border-neutral-700 text-neutral-200 text-[11px] font-mono px-2 py-1 focus:border-amber-600 outline-none" />
            )}
          </label>
        ))}
      </div>
      <button type="button" disabled={!dirty} onClick={save} data-testid="smart-wallets-knobs-save"
        className={`mt-2 text-[10px] uppercase tracking-[0.2em] px-3 py-1 border ${dirty ? "border-amber-600 text-amber-300 hover:bg-amber-950/40" : "border-neutral-800 text-neutral-600"}`}>
        Save knobs
      </button>
    </div>
  );
}

function ManualList({ snap, onChange }) {
  const [wallet, setWallet] = useState("");
  const [label, setLabel] = useState("");
  const add = async () => {
    if (!wallet.trim()) return;
    try { onChange(await api.smartWalletAdd(wallet.trim(), label.trim())); setWallet(""); setLabel(""); toast.success("wallet added"); }
    catch (e) { toast.error(e?.response?.data?.detail || e.message || "add failed"); }
  };
  const remove = async (w) => {
    try { onChange(await api.smartWalletRemove(w)); } catch (e) { toast.error(e.message || "remove failed"); }
  };
  return (
    <div data-testid="smart-wallets-manual">
      <div className="text-[10px] uppercase tracking-[0.2em] text-neutral-400 mb-1">Manual list · {snap?.manual?.length || 0}</div>
      <div className="flex gap-1 flex-wrap">
        <input value={wallet} onChange={(e) => setWallet(e.target.value)} placeholder="wallet address" data-testid="smart-wallets-add-input"
          className="flex-1 min-w-[220px] bg-neutral-950 border border-neutral-700 text-neutral-200 text-[11px] font-mono px-2 py-1 focus:border-amber-600 outline-none" />
        <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="label (optional)" data-testid="smart-wallets-add-label"
          className="w-36 bg-neutral-950 border border-neutral-700 text-neutral-200 text-[11px] font-mono px-2 py-1 focus:border-amber-600 outline-none" />
        <button type="button" onClick={add} data-testid="smart-wallets-add-btn"
          className="flex items-center gap-1 text-[10px] uppercase tracking-[0.2em] px-3 py-1 border border-amber-600 text-amber-300 hover:bg-amber-950/40">
          <Plus className="w-3 h-3" /> add
        </button>
      </div>
      <ul className="mt-2 space-y-1">
        {(snap?.manual || []).map((m) => (
          <li key={m.wallet} className="flex items-center gap-2 text-[11px] font-mono" data-testid={`smart-wallet-manual-${m.wallet}`}>
            <CopyAddr value={m.wallet} label="Smart wallet" className="text-neutral-200" />
            {m.label && <span className="text-neutral-500">{m.label}</span>}
            <button type="button" onClick={() => remove(m.wallet)} data-testid={`smart-wallet-remove-${m.wallet}`}
              className="ml-auto text-neutral-600 hover:text-rose-400"><Trash2 className="w-3 h-3" /></button>
          </li>
        ))}
        {!(snap?.manual || []).length && <li className="text-[10px] text-neutral-500">no manual wallets yet — paste a top trader's address above</li>}
      </ul>
    </div>
  );
}

function LearnedList({ snap, onChange }) {
  const rows = snap?.learned || [];
  const forget = async (w) => {
    if (!w && !window.confirm(`Wipe all ${snap?.learned_total || 0} learned wallets? The tape will start grading from zero.`)) return;
    try { onChange(await api.smartWalletForget(w)); } catch (e) { toast.error(e.message || "failed"); }
  };
  return (
    <div data-testid="smart-wallets-learned">
      <div className="flex items-center gap-2 mb-1">
        <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-400">
          Learned · {snap?.promoted || 0} promoted / {snap?.learned_total || 0} graded
        </span>
        <span className="text-[10px] font-mono text-neutral-600">
          {snap?.stats?.graded_launches || 0} launches · {snap?.stats?.wins || 0}W {snap?.stats?.misses || 0}M
        </span>
        {rows.length > 0 && (
          <button type="button" onClick={() => forget(null)} data-testid="smart-wallets-forget-all"
            className="ml-auto text-[9px] uppercase tracking-[0.15em] text-neutral-500 hover:text-rose-400">wipe learned</button>
        )}
      </div>
      {rows.length === 0 ? (
        <div className="text-[10px] text-neutral-500">nothing graded yet — the tape grades each launch's first buyers after the learn horizon</div>
      ) : (
        <div className="max-h-72 overflow-y-auto border border-neutral-800/60">
        <table className="w-full text-[11px] font-mono">
          <thead className="text-[9px] uppercase tracking-[0.15em] text-neutral-500 sticky top-0 bg-neutral-950">
            <tr><th className="text-left font-normal">wallet</th><th className="text-right font-normal">W</th><th className="text-right font-normal">M</th>
              <th className="text-right font-normal">rate</th><th className="text-right font-normal">best</th><th className="text-left font-normal pl-2">last win</th><th /></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.wallet} data-testid={`smart-wallet-learned-${r.wallet}`} className={r.promoted ? "text-emerald-300" : "text-neutral-400"}>
                <td><CopyAddr value={r.wallet} label="Smart wallet" />{r.promoted && <span className="ml-1 text-[9px] border border-emerald-800 px-1">TRIGGER</span>}</td>
                <td className="text-right">{r.wins}</td><td className="text-right">{r.misses}</td>
                <td className="text-right">{Math.round(r.win_rate * 100)}%</td>
                <td className="text-right">+{Math.round(r.best_gain_pct || 0)}%</td>
                <td className="pl-2 text-neutral-500">{r.last_win || "—"}</td>
                <td className="text-right"><button type="button" onClick={() => forget(r.wallet)} data-testid={`smart-wallet-forget-${r.wallet}`} className="text-neutral-600 hover:text-rose-400"><Trash2 className="w-3 h-3" /></button></td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
    </div>
  );
}

function Recent({ snap }) {
  const rows = snap?.recent || [];
  return (
    <div data-testid="smart-wallets-recent">
      <div className="text-[10px] uppercase tracking-[0.2em] text-neutral-400 mb-1">
        Triggers · {snap?.fires_last_hour || 0} fired this hour · {snap?.stats?.hits || 0} hits · {snap?.stats?.opened || 0} opened
      </div>
      {rows.length === 0 ? <div className="text-[10px] text-neutral-500">no smart buys seen yet</div> : (
        <ul className="space-y-0.5 text-[11px] font-mono">
          {rows.slice(0, 12).map((e, i) => (
            <li key={`${e.mint}-${e.ts}-${i}`} className="flex gap-2">
              <span className="text-neutral-600 w-8">{ago(e.ts)}</span>
              <span className="text-neutral-200"><CopyAddr value={e.mint} label="Mint">{e.symbol || e.mint?.slice(0, 8)}</CopyAddr></span>
              <span className="text-neutral-500"><CopyAddr value={e.wallet} label="Smart wallet" /> · {e.kind} · {e.sol} SOL</span>
              <span className={`ml-auto ${e.result === "opened" ? "text-emerald-300" : "text-amber-400"}`}>{e.result}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function SmartWalletsPanel({ config, onConfigUpdate }) {
  const [snap, setSnap] = useState(null);
  const load = useCallback(async () => {
    try { setSnap(await api.smartWallets()); } catch { /* keep last */ }
  }, []);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);
  return (
    <div className="space-y-4" data-testid="smart-wallets-panel">
      <div className="flex items-center gap-2">
        <Crosshair className="w-4 h-4 text-amber-400" />
        <span className="text-[11px] uppercase tracking-[0.25em] text-neutral-300">Smart wallets</span>
        <span className="text-[10px] text-neutral-500">top-trader buys inside your time window as a trigger</span>
        <button type="button" onClick={load} data-testid="smart-wallets-refresh" className="ml-auto text-neutral-600 hover:text-neutral-300"><RefreshCw className="w-3 h-3" /></button>
      </div>
      <ModeRow config={config} onConfigUpdate={onConfigUpdate} />
      <div className="grid md:grid-cols-2 gap-4">
        <ManualList snap={snap} onChange={setSnap} />
        <Recent snap={snap} />
      </div>
      <LearnedList snap={snap} onChange={setSnap} />
      <div className="border-t border-neutral-800 pt-2"><Knobs config={config} onConfigUpdate={onConfigUpdate} /></div>
    </div>
  );
}

export default memo(SmartWalletsPanel);
