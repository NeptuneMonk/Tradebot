import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Copy, ExternalLink, Send, KeyRound, Wallet } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import HelpHint from "./HelpHint";

const fmt = (v, d = 5) => (v == null ? "—" : Number(v).toFixed(d));

export default function RhWalletCard({ config }) {
  const [open, setOpen] = useState(false);
  const [w, setW] = useState(null);
  const [feed, setFeed] = useState(null);
  const [to, setTo] = useState("");
  const [amt, setAmt] = useState("");
  const [showImport, setShowImport] = useState(false);
  const [pk, setPk] = useState("");
  const [busy, setBusy] = useState(false);
  const [board, setBoard] = useState({ hot: [], positions: [] });

  const load = useCallback(async () => {
    try { setW(await api.rhWallet()); } catch { /* best effort */ }
    try {
      const st = await api.rhStatus();
      setFeed({ ...(st.seq_feed || {}), head: st.head });
      setBoard({ hot: (st.paper && st.paper.hot_board) || [], positions: (st.paper && st.paper.positions) || [] });
    } catch { /* best effort */ }
  }, []);
  useEffect(() => {
    load();
    const id = setInterval(load, open ? 30000 : 120000);
    return () => clearInterval(id);
  }, [load, open, config?.rh_live_trading]);

  const copy = () => { navigator.clipboard?.writeText(w.address); toast.success("Address copied"); };
  const send = async () => {
    if (!to || !amt) return;
    if (!window.confirm(`Send ${amt} ETH on Robinhood Chain to ${to}?`)) return;
    setBusy(true);
    try {
      const r = await api.rhWalletSend(to.trim(), parseFloat(amt));
      toast.success(`Sent · tx ${r.hash.slice(0, 10)}… ${r.ok ? "confirmed" : "FAILED on-chain"}`);
      setAmt(""); load();
    } catch (e) { toast.error(e?.response?.data?.detail || e.message); }
    finally { setBusy(false); }
  };
  const doImport = async () => {
    if (!pk || !window.confirm("Replace the RH hot wallet with this private key? The current key stays encrypted on disk but will no longer be used.")) return;
    setBusy(true);
    try {
      const r = await api.rhWalletImport(pk.trim());
      toast.success(`Wallet switched → ${r.address.slice(0, 8)}…`);
      setPk(""); setShowImport(false); load();
    } catch (e) { toast.error(e?.response?.data?.detail || e.message); }
    finally { setBusy(false); }
  };

  const live = !!config?.rh_live_trading;
  return (
    <div className={`control-card border ${live ? "border-rose-900/60" : "border-neutral-800"}`} data-testid="rh-wallet-card">
      <button type="button" onClick={() => setOpen((o) => !o)} className="w-full flex items-center justify-between gap-2 text-left" data-testid="rh-wallet-toggle">
        <span className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-400">
          {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
          <Wallet className="w-3.5 h-3.5 text-lime-300/80" /> Robinhood wallet
          <span className="text-neutral-600 normal-case tracking-normal">EVM · chain {w?.chain_id ?? 4663}</span>
          {feed && (
            <span
              className={`normal-case tracking-normal text-[9px] px-1.5 py-0.5 border ${feed.connected ? "border-lime-900 text-lime-300" : "border-rose-900 text-rose-400"}`}
              title={`Sequencer feed: sees every ordered tx before the RPC. curve sells seen ${feed.curve_sells} · rug alerts ${feed.rug_alerts} · reconnects ${feed.reconnects}${feed.last_error ? ` · ${feed.last_error}` : ""}`}
              data-testid="rh-seq-feed-pill"
            >
              seq feed {feed.connected ? `● +${Math.max(0, (feed.last_seq || 0) - (feed.head || 0))} blk ahead` : "○ down"} · ticks {feed.feed_ticks ?? 0} · rugs {feed.rug_alerts}{feed.feed_scored ? ` · est err ${Number(feed.feed_abs_err_ema_pct).toFixed(2)}% (${feed.feed_scored})` : ""}
            </span>
          )}
        </span>
        <span className="flex items-center gap-2 font-mono text-xs">
          <span className="text-neutral-200" data-testid="rh-wallet-balance">{w ? `${fmt(w.eth)} ETH` : "…"}</span>
          <span className="text-neutral-500">{w?.usd != null ? `$${w.usd.toFixed(2)}` : ""}</span>
          <span className={`text-[9px] uppercase px-1.5 py-0.5 border ${live ? "border-rose-800 text-rose-300" : "border-neutral-800 text-neutral-500"}`} data-testid="rh-wallet-mode">{live ? "live" : "paper"}</span>
        </span>
      </button>

      {open && w && (
        <div className="mt-3 space-y-3 text-[11px] font-mono">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-neutral-500 uppercase tracking-[0.15em] text-[9px]">deposit address</span>
            <code className="px-2 py-1 bg-neutral-950 border border-neutral-800 text-neutral-200 break-all" data-testid="rh-wallet-address">{w.address}</code>
            <button type="button" onClick={copy} className="p-1 border border-neutral-800 hover:bg-neutral-800" title="Copy" data-testid="rh-wallet-copy"><Copy className="w-3 h-3" /></button>
            <a href={w.explorer} target="_blank" rel="noreferrer" className="p-1 border border-neutral-800 hover:bg-neutral-800" title="Explorer" data-testid="rh-wallet-explorer"><ExternalLink className="w-3 h-3" /></a>
            <HelpHint label="Funding">{w.fund_hint}</HelpHint>
          </div>
          {!w.rpc_ok && <div className="text-rose-400">RPC error: {w.error}</div>}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <div><div className="text-[9px] uppercase tracking-[0.15em] text-neutral-600">balance</div><div className="text-neutral-200">{fmt(w.eth)} ETH · ${fmt(w.usd, 2)}</div></div>
            <div><div className="text-[9px] uppercase tracking-[0.15em] text-neutral-600">ETH/USD</div><div className="text-neutral-200">${fmt(w.eth_usd, 2)}</div></div>
            <div><div className="text-[9px] uppercase tracking-[0.15em] text-neutral-600">live RH P/L today</div><div className={(w.live_pnl_today_usd ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300"} data-testid="rh-wallet-pnl-today">{w.live_pnl_today_usd == null ? "—" : `${w.live_pnl_today_usd >= 0 ? "+" : "-"}$${Math.abs(w.live_pnl_today_usd).toFixed(2)}`}</div></div>
            <div><div className="text-[9px] uppercase tracking-[0.15em] text-neutral-600">status</div><div className={w.live_kill_tripped ? "text-rose-300" : "text-neutral-200"}>{w.live_kill_tripped ? "RH kill switch tripped" : live ? "live · gas reserve " + w.gas_reserve_eth + " ETH" : "paper only"}</div></div>
          </div>
          {w.last_live_error && <div className="text-rose-400/90 text-[10px]">last live error: {w.last_live_error}</div>}
          {w.fee_floor && (
            <div className={`text-[10px] ${w.stake_usd > 0 && w.stake_usd < w.fee_floor.min_stake_usd ? "text-amber-300" : "text-neutral-500"}`} data-testid="rh-wallet-fee-floor">
              stake ${Number(w.stake_usd ?? 0).toFixed(2)} · gas ≈ ${Number(w.fee_floor.gas_round_trip_usd).toFixed(2)}/round trip + {w.fee_floor.curve_fee_round_trip_pct}% curve fee → break-even ≈ +{(w.fee_floor.curve_fee_round_trip_pct + (w.stake_usd > 0 ? (w.fee_floor.gas_round_trip_usd / w.stake_usd) * 100 : 0)).toFixed(1)}%
              {w.stake_usd > 0 && w.stake_usd < w.fee_floor.min_stake_usd && <> · below the ${Number(w.fee_floor.min_stake_usd).toFixed(2)} fee floor — gas eats &gt;{w.fee_floor.max_gas_drag_pct}% of every trade</>}
            </div>
          )}
          {(board.positions.some((p) => p.riding) || board.hot.length > 0) && (
            <div className="mt-2 border border-neutral-800/70 p-2 space-y-1" data-testid="rh-hot-board">
              <div className="text-[9px] uppercase tracking-[0.15em] text-neutral-600">hot token board</div>
              {board.positions.filter((p) => p.riding).map((p) => (
                <div key={p.mint} className="text-[10px] font-mono text-lime-300" data-testid={`rh-riding-${p.mint}`}>
                  ▲ riding {p.symbol} · {(((p.last_price || 0) / (p.entry_price_quote || 1) - 1) * 100).toFixed(0)}% · pyramids {p.pyramids || 0}
                </div>
              ))}
              {board.hot.map((h) => (
                <div key={h.mint} className={`text-[10px] font-mono ${h.hot ? "text-amber-300" : "text-neutral-500"}`} data-testid={`rh-hot-${h.mint}`}>
                  {h.hot ? "●" : "○"} {h.symbol || h.mint.slice(0, 8)} · {h.attempts_left} re-entr{h.attempts_left === 1 ? "y" : "ies"} left · size ×{Number(h.size_multiplier || 0).toFixed(2)} · {Math.floor(h.seconds_left / 60)}m{String(h.seconds_left % 60).padStart(2, "0")}s{h.last_trigger ? ` · last ${h.last_trigger}` : ""}
                </div>
              ))}
            </div>
          )}

          <div className="border-t border-neutral-800 pt-3 grid grid-cols-1 sm:grid-cols-[2fr_1fr_auto] gap-2 items-end">
            <label className="block"><span className="text-[9px] uppercase tracking-[0.15em] text-neutral-600">send ETH to</span>
              <input value={to} onChange={(e) => setTo(e.target.value)} placeholder="0x…" className="mt-0.5 w-full px-2 py-1 bg-neutral-950 border border-neutral-800 text-neutral-200" data-testid="rh-wallet-send-to" /></label>
            <label className="block"><span className="text-[9px] uppercase tracking-[0.15em] text-neutral-600">amount ETH</span>
              <input type="number" step="0.001" min="0" value={amt} onChange={(e) => setAmt(e.target.value)} className="mt-0.5 w-full px-2 py-1 bg-neutral-950 border border-neutral-800 text-neutral-200" data-testid="rh-wallet-send-amount" /></label>
            <button type="button" onClick={send} disabled={busy || !to || !amt || !(w.eth > 0)} className="px-3 py-1.5 border border-lime-700 text-lime-300 hover:bg-lime-950/60 disabled:opacity-40 uppercase inline-flex items-center gap-1" data-testid="rh-wallet-send-btn"><Send className="w-3 h-3" /> send</button>
          </div>

          <div className="border-t border-neutral-800 pt-2">
            <button type="button" onClick={() => setShowImport((s) => !s)} className="text-[10px] uppercase tracking-wider text-neutral-500 hover:text-neutral-300 inline-flex items-center gap-1" data-testid="rh-wallet-import-toggle"><KeyRound className="w-3 h-3" /> import private key</button>
            {showImport && (
              <div className="mt-2 flex gap-2">
                <input type="password" value={pk} onChange={(e) => setPk(e.target.value)} placeholder="0x… private key (never leaves the server)" className="flex-1 px-2 py-1 bg-neutral-950 border border-neutral-800 text-neutral-200" data-testid="rh-wallet-import-input" />
                <button type="button" onClick={doImport} disabled={busy || !pk} className="px-3 py-1 border border-rose-800 text-rose-300 hover:bg-rose-950/60 disabled:opacity-40 uppercase" data-testid="rh-wallet-import-btn">replace</button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
