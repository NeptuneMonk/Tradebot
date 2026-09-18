import { memo } from "react";

const usd = (v, sign = false) => { const n = Number(v) || 0; return `${sign ? (n >= 0 ? "+" : "-") : n < 0 ? "-" : ""}$${Math.abs(n).toFixed(2)}`; };
const tone = (v) => (Number(v) >= 0 ? "text-emerald-300" : "text-red-300");

function Cell({ label, children, title, testid, grow }) {
  return (
    <div data-testid={testid} title={title} className={`px-3 py-2 border-r border-neutral-800 last:border-r-0 min-w-0 ${grow ? "flex-[1.4]" : "flex-1"}`}>
      <div className="text-[9px] uppercase tracking-[0.2em] text-neutral-500 whitespace-nowrap">{label}</div>
      <div className="font-mono text-sm md:text-base leading-tight whitespace-nowrap truncate mt-0.5">{children}</div>
    </div>
  );
}

function Dot({ on, warn }) {
  return <span className={`inline-block w-2 h-2 rounded-full mr-1.5 align-middle ${on ? "bg-emerald-400" : warn ? "bg-amber-400" : "bg-red-500"}`} />;
}

function feedSol(status, config) {
  const desired = status?.helius_tracker_enabled ?? config?.helius_tracker_enabled ?? true;
  if (!desired) return { text: "OFF", on: false, warn: true, why: "operator switch OFF (Control → feeds)" };
  if (status?.listener_connected) {
    const via = status.listener_via || "";
    const fb = via && !/helius/i.test(via);
    return { text: fb ? "FALLBACK" : "ON", on: !fb, warn: fb, why: `connected via ${via || "primary WSS"}` };
  }
  if (status?.helius_paused?.auto) return { text: "PAUSED", on: false, warn: true, why: `Doctor paused the feed: ${status.helius_paused.auto_reason || ""}` };
  return { text: "OFFLINE", on: false, why: status?.listener_last_error || "reconnecting…" };
}

function feedRh(status, config) {
  const desired = status?.rh_feed_enabled ?? config?.rh_feed_enabled;
  if (!desired) return { text: "OFF", on: false, warn: true, why: "RH feed switch OFF (Control → feeds)" };
  if (status?.rh_feed_alive) return { text: "ON", on: true, why: "sequencer head advancing" };
  return { text: status?.rh_feed_paused_reason ? "PAUSED" : "STALLED", on: false, warn: !!status?.rh_feed_paused_reason, why: status?.rh_feed_paused_reason || "head-stalled — no new RH blocks seen" };
}

function docState(status, books) {
  const benched = Object.keys(status?.books_paused || {});
  const hit = benched.filter((b) => books.includes(b));
  if (!hit.length) return { text: "ok", cls: "text-emerald-300" };
  const live = books.includes("rh_pons") ? status?.rh_live_trading : status?.live_trading;
  return { text: live ? "bench" : "adjust", cls: live ? "text-red-300" : "text-amber-300", books: hit };
}

function KpiStrip({ wallet, status, config, auto, pl }) {
  const chains = auto?.bankroll?.chains || {};
  const ledger = auto?.search_ledger;
  const dd = Math.min(...["sol", "rh"].map((c) => Number(chains[c]?.drawdown_24h_pct ?? 0)));
  const dd24 = ["sol", "rh"].reduce((a, c) => a + Number(chains[c]?.pnl_24h_usd ?? 0), 0);
  const slotsMax = Number(auto?.sizing?.max_concurrent_positions ?? config?.max_concurrent_positions ?? 0);
  const sol = feedSol(status, config), rh = feedRh(status, config);
  const tempo = status?.market_tempo || {};
  const hour = new Date().getUTCHours();
  const peak = (tempo.peak_hours || []).includes(hour);
  const docSol = docState(status, ["scalp", "hunt", "runner"]), docRh = docState(status, ["rh_pons"]);
  const daily = pl?.daily_pnl_usd ?? status?.daily_pnl_usd ?? 0;
  return (
    <div data-testid="kpi-strip" className="flex border border-neutral-800 bg-neutral-950/70 overflow-x-auto">
      <Cell label="wallet" testid="kpi-wallet" title={wallet ? `${wallet.sol_balance.toFixed(4)} SOL · SOL $${wallet.sol_price_usd.toFixed(2)}` : ""}>
        {wallet ? usd(wallet.usd_balance) : "—"} <span className="text-neutral-600 text-xs">{wallet ? `${wallet.sol_balance.toFixed(3)} SOL` : ""}</span>
      </Cell>
      <Cell label="p/l today" testid="kpi-pl" title={`live ${usd(status?.daily_pnl_live_usd, true)} · paper ${usd(status?.daily_pnl_paper_usd, true)}`}>
        <span className={tone(daily)}>{usd(daily, true)}</span>
      </Cell>
      <Cell label="harvest / search" testid="kpi-harvest" grow title={ledger ? `7-day ledger · harvest = runner closes + banked promotion proceeds (${ledger.harvest_fills} fills) · search = scalp/hunt/rh_pons closes that never became runners (${ledger.search_fills} fills) · cost per runner $${Number(ledger.cost_per_runner_usd).toFixed(0)}` : "search ledger loading"}>
        {ledger ? <><span className={tone(ledger.harvest_realised_usd)}>{usd(ledger.harvest_realised_usd, true)}</span><span className="text-neutral-600"> / </span><span className={tone(ledger.search_realised_usd)}>{usd(ledger.search_realised_usd, true)}</span></> : "—"}
      </Cell>
      <Cell label="dd 24h" testid="kpi-dd" title="24-hour P/L and drawdown across both paper/live bankrolls">
        <span className={tone(dd24)}>{usd(dd24, true)}</span> <span className="text-neutral-600 text-xs">{isFinite(dd) ? `${dd.toFixed(1)}%` : ""}</span>
      </Cell>
      <Cell label="slots" testid="kpi-slots" title="open positions / max concurrent positions">
        {status?.active_trade_count ?? 0}<span className="text-neutral-600">/{slotsMax || "—"}</span>
      </Cell>
      <Cell label="sol feed" testid="kpi-feed-sol" title={sol.why}><Dot on={sol.on} warn={sol.warn} /><span className={sol.on ? "text-emerald-300" : sol.warn ? "text-amber-300" : "text-red-300"}>{sol.text}</span></Cell>
      <Cell label="rh feed" testid="kpi-feed-rh" title={rh.why}><Dot on={rh.on} warn={rh.warn} /><span className={rh.on ? "text-emerald-300" : rh.warn ? "text-amber-300" : "text-red-300"}>{rh.text}</span></Cell>
      <Cell label="tempo" testid="kpi-tempo" title={`gate multiplier = √tempo · sol ${tempo.sol?.tempo ?? "—"}× (gates ${tempo.sol?.gate_mult ?? "—"}×) · rh ${tempo.rh?.tempo ?? "—"}× · peak UTC hours ${(tempo.peak_hours || []).join(",")}`}>
        {tempo.sol ? `${tempo.sol.tempo}×` : "—"}{peak && <span className="text-emerald-300 text-xs ml-1.5">PEAK</span>}
      </Cell>
      <Cell label="doc" testid="kpi-doc" title={`Live Doctor breakers · sol: ${docSol.books?.join(", ") || "clear"} · rh: ${docRh.books?.join(", ") || "clear"} — bench = no live entries, adjust = paper at ×0.5 size / gates ×1.25`}>
        <span className="text-neutral-500 text-xs">sol:</span><span className={docSol.cls}>{docSol.text}</span> <span className="text-neutral-500 text-xs">rh:</span><span className={docRh.cls}>{docRh.text}</span>
      </Cell>
    </div>
  );
}

export default memo(KpiStrip);
