import { useEffect, useState } from "react";
import { ExternalLink, RefreshCw, ShoppingCart } from "lucide-react";
import { toast } from "sonner";
import { RepBadge } from "./RepBadge";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from "@/components/ui/sheet";
import { CreatorAuditPanel } from "@/components/CreatorAuditPanel";
import { ChainBadge } from "./ChainBadge";
import { api, explainApiError } from "@/lib/api";
import TokenChart from "./TokenChart";
import { flushVerdict } from "@/lib/flushVerdict";

const fmtUsd = (n) => {
  const v = Number(n);
  if (!isFinite(v)) return "—";
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toFixed(v < 1 ? 4 : 2)}`;
};
const fmtAge = (s) => (s == null ? "—" : s < 60 ? `${Math.floor(s)}s` : s < 3600 ? `${Math.floor(s / 60)}m` : `${(s / 3600).toFixed(1)}h`);
const pct = (v) => (v == null ? "—" : `${Number(v) >= 0 ? "+" : ""}${Number(v).toFixed(1)}%`);

function Stat({ label, value, tone }) {
  return (
    <div className="border border-neutral-800 px-2 py-1.5">
      <div className="text-[9px] uppercase tracking-[0.15em] text-neutral-500">{label}</div>
      <div className={`font-mono text-sm ${tone || "text-neutral-100"}`}>{value}</div>
    </div>
  );
}

export function TokenDetailDialog({ token, onClose }) {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const chain = token?.chain || "sol";
  const mint = token?.mint;

  const load = () => mint && api.tokenDetail(chain, mint).then(setData).catch(() => setData({ error: true }));
  useEffect(() => { setData(null); load(); const id = setInterval(load, 10000); return () => clearInterval(id); }, [chain, mint]); // eslint-disable-line react-hooks/exhaustive-deps

  const reenter = async () => {
    setBusy(true);
    try {
      const r = await api.scannerManualBuy(mint);
      r?.ok === false ? toast.error(r.reason || "entry refused") : toast.success("Manual re-entry sent");
      load();
    } catch (e) { toast.error(`Entry refused — ${explainApiError(e)}`, { duration: 9000 }); } finally { setBusy(false); }
  };

  const live = data?.live, m = data?.market, rx = data?.reentry;
  const links = chain === "rh"
    ? [["RH explorer", `https://explorer.mainnet.chain.robinhood.com/token/${mint}`], ["DexScreener", m?.url || `https://dexscreener.com/search?q=${mint}`]]
    : [["pump.fun", `https://pump.fun/coin/${mint}`], ["DexScreener", m?.url || `https://dexscreener.com/solana/${mint}`], ["Solscan", `https://solscan.io/token/${mint}`]];

  return (
    <Sheet open={!!token} onOpenChange={(o) => !o && onClose()}>
      <SheetContent side="right" className="w-full sm:max-w-none sm:w-[min(1280px,94vw)] bg-neutral-950 border-l border-neutral-800 text-neutral-100 p-0 overflow-y-auto flex flex-col" data-testid="token-detail-dialog">
        <SheetHeader className="px-4 pt-4 pb-2 border-b border-neutral-800 pr-12">
          <SheetTitle className="flex items-center gap-2 font-mono text-base">
            <ChainBadge chain={chain} protocol={live?.protocol} mint={mint} />
            {token?.symbol || live?.symbol || "?"}
            {chain !== "rh" && <RepBadge mint={mint} creator={token?.creator || live?.creator} />}
            <span className="text-neutral-500 text-xs font-normal truncate">{token?.name || live?.name}</span>
            <span className="ml-auto flex items-center gap-2">
              {links.map(([l, h]) => (
                <a key={l} href={h} target="_blank" rel="noreferrer" className="text-[10px] font-mono uppercase tracking-[0.15em] text-cyan-300 hover:underline inline-flex items-center gap-1">{l}<ExternalLink className="w-3 h-3" /></a>
              ))}
              <button onClick={load} className="p-1 border border-neutral-800 hover:bg-neutral-800" title="refresh" data-testid="token-detail-refresh"><RefreshCw className="w-3 h-3" /></button>
            </span>
          </SheetTitle>
          <SheetDescription className="font-mono text-[10px] text-neutral-500 break-all">{mint}</SheetDescription>
        </SheetHeader>

        <div className="px-4 py-3 grid grid-cols-1 lg:grid-cols-[minmax(300px,2fr)_3fr] gap-3 flex-1 min-h-0">
          <div className="w-full space-y-3 lg:overflow-y-auto lg:max-h-[calc(100vh-120px)] lg:pr-1">
            <div className="grid grid-cols-2 gap-1.5" data-testid="token-detail-stats">
              <Stat label={m?.mc_usd ? "market cap · dexscreener" : "market cap · our feed"} value={fmtUsd(m?.mc_usd ?? live?.mc_usd)} />
              <Stat label="price" value={m?.price_usd ? `$${Number(m.price_usd).toPrecision(4)}` : live?.price ? `${Number(live.price).toPrecision(4)} ${live.price_unit}` : "—"} />
              {m?.mc_usd && live?.mc_usd && Math.abs(live.mc_usd / m.mc_usd - 1) > 0.25 && (
                <div className="col-span-2 text-[10px] font-mono text-amber-300" data-testid="token-detail-mc-mismatch">our feed last saw MC {fmtUsd(live.mc_usd)} — stale vs DexScreener; pool reads refresh it while a position is open</div>
              )}
              <Stat label="liquidity" value={fmtUsd(m?.liquidity_usd)} />
              <Stat label="vol 1h / 24h" value={`${fmtUsd(m?.vol_1h)} / ${fmtUsd(m?.vol_24h)}`} />
              <Stat label="Δ 1h / 24h" value={`${pct(m?.chg_1h)} / ${pct(m?.chg_24h)}`} tone={Number(m?.chg_1h) >= 0 ? "text-emerald-400" : "text-red-400"} />
              <Stat label="txns 1h" value={m?.txns_1h ? `${m.txns_1h.buys}b / ${m.txns_1h.sells}s` : "—"} />
              <Stat label="holders seen" value={live?.holders ?? "—"} />
              <Stat label={chain === "rh" ? "curve / pool" : "protocol"} value={chain === "rh" ? (live?.graduated ? (live.pool_live ? "v4 pool live" : "graduated · no pool") : `${Number(live?.curve_fill_pct || 0).toFixed(1)}% curve`) : (live?.protocol || m?.dex || "—")} />
              <Stat label="tracked age" value={fmtAge(live?.age_s)} />
              <Stat label="gate" value={live?.gate || (data?.tracked ? "—" : "not tracked")} tone={live?.gate === "pass" ? "text-emerald-400" : "text-amber-300"} />
            </div>
            {live?.gate_detail && <div className="text-[10px] font-mono text-neutral-500">{live.gate_detail}</div>}

            <div className="border border-neutral-800 p-2 text-[10px] font-mono text-neutral-400 space-y-1" data-testid="token-detail-reentry">
              <div className="uppercase tracking-[0.15em] text-neutral-500">our record · {data?.summary?.n ?? 0} trades · <span className={(data?.summary?.pnl_usd || 0) >= 0 ? "text-emerald-400" : "text-red-400"}>${(data?.summary?.pnl_usd || 0).toFixed(2)}</span></div>
              {rx ? (
                <div>re-entry: {rx.attempts} attempt(s) · last exit {fmtAge(Date.now() / 1000 - rx.ts)} ago {rx.was_sl ? "· stop-out" : ""} {rx.hot ? "· hot" : ""} {rx.sl_until > Date.now() / 1000 ? <span className="text-amber-300">· SL cooldown {fmtAge(rx.sl_until - Date.now() / 1000)} left</span> : null}</div>
              ) : <div>no exit inside the re-entry window — a manual buy is a fresh entry</div>}
              {data?.watch && <div className="text-lime-300">{data.watch.flush ? `flush stop — re-buying the reclaim (${data.watch.flush_kind || "flush"})` : "on the pullback / breakout watch"} · attempts {data.watch.attempts}</div>}
              <ul className="max-h-28 overflow-auto space-y-0.5">
                {(data?.trades || []).map((t) => {
                  const fv = flushVerdict(t);
                  return (
                    <li key={t.id} className="flex flex-col gap-0" data-testid={`record-trade-${t.id}`}>
                      <div className="flex gap-2"><span className="text-neutral-500">{t.book}</span><span>{t.status}</span>
                        <span className={(t.pnl_pct || 0) >= 0 ? "text-emerald-400" : "text-red-400"}>{pct(t.pnl_pct)}</span><span className="truncate text-neutral-500">{t.exit_reason || ""}</span></div>
                      {fv && <div className="pl-2 text-[9px] text-neutral-500" data-testid={`record-flush-${t.id}`}><span className={fv.tint.replace(/border-\S+/, "")}>{fv.label}</span> · {fv.detail}</div>}
                    </li>
                  );
                })}
              </ul>
            </div>

            <button onClick={reenter} disabled={busy || data?.summary?.active} data-testid="token-detail-reenter"
              className="w-full inline-flex items-center justify-center gap-2 px-3 py-2 border border-emerald-700 text-emerald-300 hover:bg-emerald-950/40 disabled:opacity-40 font-mono text-xs uppercase tracking-[0.2em]">
              <ShoppingCart className="w-3.5 h-3.5" /> {data?.summary?.active ? "position open" : busy ? "sending…" : "re-enter manually"}
            </button>
            <div className="text-[9px] font-mono text-neutral-600">Manual entry bypasses the momentum gates; max positions, kill switches and the live/paper mode still apply. Manual holds exit on the scalp R target only (or promote to runner when flow is strong) — no SL, trailing stop, TP or clock; sell with ✕ anytime.</div>

            <CreatorAuditPanel key={`${chain}:${mint}`} chain={chain} mint={mint} initial={live?.creator_audit || null} />
            {chain === "sol" && data?.rugcheck !== undefined && (
              <div className="border border-neutral-800 p-2 space-y-1" data-testid="token-detail-rugcheck">
                <div className="flex items-center justify-between text-[10px] uppercase tracking-[0.15em] text-neutral-500">
                  <span>rugcheck · free summary</span>
                  {data.rugcheck ? (
                    <span className={`font-mono ${data.rugcheck.rugged ? "text-red-400" : (data.rugcheck.score_normalised ?? 0) >= 50 ? "text-amber-300" : "text-emerald-300"}`} data-testid="rugcheck-score">
                      {data.rugcheck.rugged ? "RUGGED" : `risk ${data.rugcheck.score_normalised ?? data.rugcheck.score ?? "—"}`}
                    </span>
                  ) : <span className="text-neutral-600">unavailable</span>}
                </div>
                {(data.rugcheck?.risks || []).slice(0, 6).map((r, i) => (
                  <div key={i} className="text-[10px] font-mono text-neutral-400 flex gap-2">
                    <span className={r.level === "danger" ? "text-red-400" : r.level === "warn" ? "text-amber-300" : "text-neutral-500"}>{r.level}</span>
                    <span className="text-neutral-300">{r.name}</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="min-h-[420px] lg:h-[calc(100vh-120px)] border border-neutral-800 bg-black overflow-y-auto" data-testid="token-detail-widget">
            {chain === "rh" ? (
              <>
                <TokenChart chain="rh" mint={mint} height={400} />
                {m?.embed && (
                  <details className="border-t border-neutral-800" data-testid="token-detail-dexscreener-details">
                    <summary className="cursor-pointer px-2 py-1 text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-500 hover:text-neutral-300">
                      DexScreener widget · {m.dex} {(m.labels || []).join(" ")} · liq ${Number(m.liquidity_usd || 0).toLocaleString()} (their candle feed for Robinhood Chain is often empty)
                    </summary>
                    <iframe title="DexScreener" src={m.embed} className="w-full h-[460px]" allow="clipboard-write" loading="lazy" data-testid="token-detail-dexscreener" />
                  </details>
                )}
              </>
            ) : m?.embed ? (
              <iframe title="DexScreener" src={m.embed} className="w-full h-[460px] lg:h-full" allow="clipboard-write" loading="lazy" data-testid="token-detail-dexscreener" />
            ) : chain === "sol" ? (
              <iframe title="DexScreener" src={`https://dexscreener.com/solana/${mint}?embed=1&theme=dark&trades=0&info=0`} className="w-full h-[460px] lg:h-full" loading="lazy" />
            ) : (
              <div className="p-3 text-[10px] font-mono text-neutral-500 hidden" data-testid="token-detail-no-chart">
                {live?.graduated
                  ? "DexScreener has not indexed this Uniswap v4 pool yet (it usually appears a few minutes after graduation) — live pool numbers on the left come from our sequencer feed."
                  : "Still on the Robinhood launch curve — DexScreener only lists the Uniswap v4 pool after graduation. Live curve numbers on the left come from our sequencer feed."}
                <a className="block mt-2 text-cyan-300 hover:underline" href={`https://dexscreener.com/search?q=${mint}`} target="_blank" rel="noreferrer">search on DexScreener ↗</a>
                <a className="block mt-1 text-cyan-300 hover:underline" href={links[0][1]} target="_blank" rel="noreferrer">open in the RH explorer ↗</a>
              </div>
            )}
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
