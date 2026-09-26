import { useState, useEffect, useRef, memo } from "react";
import { PauseCircle, Power, Zap, Settings2, ChevronDown, ChevronRight, Radio, Pause, Eye } from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { toast } from "sonner";
import { wikiSectionFor } from "@/lib/wikiNav";
import SpeedModeSlider from "./SpeedModeSlider";
import ConfigSyncPanel from "./ConfigSyncPanel";
import BrainSyncPanel from "./BrainSyncPanel";
import BookExitsEditor from "./BookExitsEditor";
import HelpHint from "./HelpHint";

// desired (operator toggle) vs actual (transport health). Three states, never "RUNNING":
//   OFF (amber) · ON · CONNECTING/OFFLINE (red) · ON · LIVE (green)
function feedHealth(desired, actual, extra = {}) {
  if (!desired) return { text: "OFF", dot: "bg-amber-500", cls: "text-amber-300", why: "operator switch OFF" };
  if (actual && extra.via) return { text: `ON · FALLBACK: ${extra.via.toUpperCase()}`, dot: "bg-emerald-500", cls: "text-emerald-300", why: `primary WSS quota exhausted — feed carried by ${extra.via} until the feed is toggled OFF→ON` };
  if (actual) return { text: "ON · LIVE", dot: "bg-emerald-500", cls: "text-emerald-300", why: extra.okTs ? `subscribed ${Math.max(0, Math.round(Date.now() / 1000 - extra.okTs))}s ago` : "subscribed" };
  if (extra.paused?.auto) return { text: "ON · PAUSED · DOCTOR", dot: "bg-amber-500", cls: "text-amber-300", why: extra.paused.auto_reason || "auto-paused" };
  const connecting = extra.attemptTs && Date.now() / 1000 - extra.attemptTs < 15;
  return { text: connecting ? "ON · CONNECTING" : "ON · OFFLINE", dot: "bg-red-500", cls: "text-red-300",
           why: extra.lastError || "reconnecting…" };
}

function BotControlCard({ status, config, onUpdate, onStart, onStop, onConfigLoaded }) {
  const [local, setLocal] = useState(null);
  // Baseline = last clean snapshot of config we've seen. The form is "dirty"
  // ONLY when `local !== baseline`. This lets backend-side changes (Doctor
  // Apply, ConfigSync, /api/config/apply-recommended) overwrite the form
  // when the user has no pending edits, instead of being silently rejected
  // because `config !== local`.
  const [baseline, setBaseline] = useState(null);
  const [showAdvancedFees, setShowAdvancedFees] = useState(false);
  const [savedDefaults, setSavedDefaults] = useState({ exists: false, saved_at: null });
  const inflight = useRef(new Set());

  // Probe whether the user has previously saved their own defaults so we
  // can render the "Restore my defaults" button + the "saved on …" hint.
  useEffect(() => {
    import("@/lib/api").then(m => m.api.savedDefaultsExists())
      .then(setSavedDefaults).catch(() => {});
  }, []);

  useEffect(() => {
    if (!config) return;
    setLocal((prev) => {
      // First load
      if (prev === null) {
        setBaseline(config);
        return config;
      }
      // User has unsaved edits — KEEP them (don't clobber via background poll)
      const userIsDirty = JSON.stringify(prev) !== JSON.stringify(baseline);
      if (userIsDirty) return prev;
      // Form is clean → accept the incoming config (Doctor Apply etc.)
      setBaseline(config);
      return config;
    });
  }, [config, baseline]);

  if (!local) return <div className="control-card text-neutral-500 text-sm">Loading...</div>;

  const dirty = JSON.stringify(local) !== JSON.stringify(baseline);
  const running = status?.enabled;

  // Only the keys the user actually changed go over the wire. Sending the whole form snapshot re-wrote
  // stale values (a feed toggle flipped by another click / the Doctor) — that's why feeds "switched themselves off".
  // Operator switches only ever travel through their own toggle (flipKey) — never inside a form save.
  const SWITCH_KEYS = new Set(["enabled", "helius_tracker_enabled", "rh_feed_enabled", "rh_paper_enabled", "rh_live_trading", "live_trading", "scanner_enabled", "scanner_seasoned_entries_enabled", "trail_ratchet_enabled", "ladder_enabled"]);
  const diff = (a, b) => Object.fromEntries(Object.entries(a || {}).filter(([k, v]) => !SWITCH_KEYS.has(k) && JSON.stringify(v) !== JSON.stringify((b || {})[k])));
  const save = async () => {
    try {
      const patch = diff(local, baseline);
      if (Object.keys(patch).length === 0) { toast.message("Nothing to save"); return; }
      await onUpdate(patch);
      setBaseline(local);  // promote current edit to baseline
      toast.success("Config saved");
    } catch (e) {
      toast.error("Save failed");
    }
  };

  // Optimistic single-key toggle: flip instantly, persist in the background,
  // revert on failure. Merged into the edit-in-progress so unsaved edits survive.
  const flipKey = async (key, next, onOk, onErr) => {
    if (inflight.current.has(key)) return;                 // double-click guard: one PUT per switch at a time
    inflight.current.add(key);
    setLocal((cur) => ({ ...cur, [key]: next }));
    setBaseline((b) => (b ? { ...b, [key]: next } : b));
    try {
      await onUpdate({ [key]: next });
      onOk?.();
    } catch (e) {
      setLocal((cur) => ({ ...cur, [key]: !next }));
      setBaseline((b) => (b ? { ...b, [key]: !next } : b));
      toast.error(onErr?.(e) || "Toggle failed");
    } finally {
      inflight.current.delete(key);
    }
  };

  return (
    <div className="control-card flex flex-col gap-3" data-testid="bot-control-card">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-neutral-500">
          <Settings2 className="w-3 h-3" />
          Bot Control
        </div>
        <div className="flex items-center gap-2 text-[10px] font-mono uppercase">
          <span className={local.live_trading ? "text-red-400" : "text-neutral-500"}>LIVE</span>
          <Switch
            data-testid="live-trading-switch"
            checked={!!local.live_trading}
            onCheckedChange={(v) => {
              if (v && !window.confirm("Enable SOL LIVE trading? Real SOL from the local wallet will buy Pump.fun / PumpSwap tokens that pass the gates. The daily kill switch and position caps apply.")) return;
              flipKey("live_trading", v,
                () => toast[v ? "warning" : "success"](v ? "SOL LIVE trading ON — real SOL in play" : "SOL live trading OFF — paper mode"),
                (e) => e?.response?.data?.detail);
            }}
          />
          <HelpHint label="LIVE toggle" side="left">
            <span className="block"><span className="text-red-300 font-semibold">LIVE</span> = sends real on-chain transactions with the local wallet's SOL. <span className="text-emerald-300 font-semibold">OFF</span> = paper-trade mode; signals fire and PnL is tracked but no funds are touched.</span>
          </HelpHint>
        </div>
      </div>

      {(() => {
        const stopping = status?.stopping_gracefully;
        const activeN = status?.active_trade_count || 0;
        if (stopping) {
          return (
            <div className="space-y-1">
              <button
                disabled
                data-testid="stopping-bot-indicator"
                className="w-full flex items-center justify-center gap-2 px-3 py-2.5 border border-amber-700 text-amber-300 bg-amber-950/50 font-mono text-xs uppercase tracking-[0.2em] cursor-not-allowed"
              >
                <Power className="w-3 h-3 animate-pulse" />
                Stopping · waiting on {activeN} position{activeN === 1 ? "" : "s"}
              </button>
              <div className="flex items-center justify-between text-[10px] font-mono uppercase tracking-[0.15em] px-1">
                <button
                  onClick={onStart}
                  data-testid="resume-bot-btn"
                  className="text-emerald-400 hover:text-emerald-300 transition-colors"
                >
                  ▸ resume trading
                </button>
                <button
                  onClick={async () => {
                    if (!window.confirm(`Force-close ${activeN} active position${activeN === 1 ? "" : "s"} right now? This skips TP/SL triggers.`)) return;
                    try {
                      const m = await import("@/lib/api");
                      await m.api.abortBot();
                      toast.success("Hard stop — all positions force-closed");
                    } catch {
                      toast.error("Abort failed");
                    }
                  }}
                  data-testid="abort-bot-btn"
                  className="text-red-400 hover:text-red-300 transition-colors"
                >
                  ✕ abort all
                </button>
              </div>
            </div>
          );
        }
        return (
          <button
            onClick={running ? onStop : onStart}
            disabled={status?.kill_switch_tripped}
            data-testid={running ? "stop-bot-btn" : "start-bot-btn"}
            className={`w-full flex items-center justify-center gap-2 px-3 py-2.5 border font-mono text-xs uppercase tracking-[0.2em] transition-colors duration-100 ${
              running
                ? "border-red-700 text-red-300 bg-red-950 hover:bg-red-900"
                : "border-emerald-700 text-emerald-300 bg-emerald-950 hover:bg-emerald-900"
            } disabled:opacity-40 disabled:cursor-not-allowed`}
          >
            <Power className="w-3 h-3" />
            {running ? "Stop Bot" : "Start Bot"}
          </button>
        );
      })()}

      {/* Helius tracker kill switch — single-click save (no need to hit
          "Save Config" first). This is intentionally placed right under
          the Start/Stop button because it's the user's primary tool for
          throttling Helius credit consumption when running preview +
          production simultaneously. */}
      <button
        type="button"
        data-testid="helius-tracker-toggle"
        onClick={() => {
          const next = !(local.helius_tracker_enabled ?? true);
          flipKey("helius_tracker_enabled", next, () => toast.success(next
            ? "Pump.fun feed ON — Solana trading resumes, listener reconnecting…"
            : "Pump.fun feed OFF — no new Solana entries; Robinhood keeps trading"));
        }}
        className={`w-full flex items-center justify-between px-3 py-2 border text-xs uppercase tracking-[0.15em] font-mono transition-colors duration-100 ${
          (local.helius_tracker_enabled ?? true)
            ? "border-emerald-800 text-emerald-300 bg-emerald-950/40 hover:bg-emerald-900/50"
            : "border-amber-700 text-amber-200 bg-amber-950/50 hover:bg-amber-900/50"
        }`}
      >
        <span className="flex items-center gap-2">
          {(local.helius_tracker_enabled ?? true) ? (
            <Radio className="w-3 h-3" />
          ) : (
            <Pause className="w-3 h-3" />
          )}
          Pump.fun Feed · Solana Trading
        </span>
        <span className="flex items-center gap-1.5">
          {(() => { const h = feedHealth(local.helius_tracker_enabled ?? true, status?.listener_connected,
              { paused: status?.helius_paused, lastError: status?.listener_last_error, okTs: status?.listener_last_ok_ts, attemptTs: status?.listener_last_attempt_ts, via: status?.listener_via }); return (
            <span className={`flex items-center gap-1 text-[9px] ${h.cls}`} data-testid="feed-pump-health" title={h.why}>
              <span className={`w-1.5 h-1.5 rounded-full ${h.dot}`} /> {h.text}
              {!status?.listener_connected && (local.helius_tracker_enabled ?? true) && (
                <span className="normal-case tracking-normal text-neutral-500 max-w-[220px] truncate" data-testid="feed-pump-why">— {h.why}</span>
              )}
            </span>
          ); })()}
          <span className={`text-[10px] ${(local.helius_tracker_enabled ?? true) ? "text-emerald-300" : "text-amber-300"}`} data-testid="feed-pump-desired">
            <span data-testid="helius-tracker-state">{(local.helius_tracker_enabled ?? true) ? "ON" : "OFF"}</span>
          </span>
          <HelpHint label="Pump.fun Feed · Solana Trading">
            <div className="space-y-1.5">
              <div><span className="text-emerald-300">ON</span>: the bot listens to Pump.fun launches via Helius, the momentum scanner fetches curve state, and Solana entries (new-band, seasoned momentum, greylist snipes, re-entries) can fire. Burns Helius credits proportional to chain activity.</div>
              <div><span className="text-amber-300">OFF — Robinhood only</span>: Pump.fun feed disconnects and <strong>no new Solana trades open</strong>. Robinhood paper/live trading, the sequencer feed and Autopilot are unaffected (they never touch Helius). <strong>Open Solana positions keep being monitored</strong> until they exit, so nothing is stranded.</div>
              <div className="text-neutral-400">Use OFF when you only want to run Robinhood trades, or to stop Helius credit burn on an environment you aren&apos;t trading on. Start/Stop still controls the whole bot.</div>
            </div>
          </HelpHint>
        </span>
      </button>

      {/* Robinhood Chain feed toggle — PONS launch feed polled
          from the RH public RPC. Zero Helius credits either way; this just
          stops the 1-req/2s poll when the user doesn't care about RH. */}
      <button
        type="button"
        data-testid="rh-feed-toggle"
        onClick={() => {
          const next = !(local.rh_feed_enabled ?? true);
          flipKey("rh_feed_enabled", next, () => toast.success(next ? "Robinhood Chain feed resumed" : "Robinhood Chain feed paused"));
        }}
        className={`w-full flex items-center justify-between px-3 py-2 border text-xs uppercase tracking-[0.15em] font-mono transition-colors duration-100 ${
          (local.rh_feed_enabled ?? true)
            ? "border-lime-800 text-lime-300 bg-lime-950/30 hover:bg-lime-900/40"
            : "border-neutral-700 text-neutral-400 bg-neutral-900/50 hover:bg-neutral-800/60"
        }`}
      >
        <span className="flex items-center gap-2">
          {(local.rh_feed_enabled ?? true) ? <Radio className="w-3 h-3" /> : <Pause className="w-3 h-3" />}
          Robinhood Chain Feed
        </span>
        <span className="flex items-center gap-1.5">
          {(() => { const h = feedHealth(local.rh_feed_enabled ?? true, status?.rh_feed_alive,
              { paused: status?.rh_feed_paused_reason ? { auto: true, auto_reason: status.rh_feed_paused_reason } : null, lastError: status?.rh_feed_alive ? null : "poll loop idle (head not moving)" }); return (
            <span className={`flex items-center gap-1 text-[9px] ${h.cls}`} data-testid="feed-rh-health" title={h.why}>
              <span className={`w-1.5 h-1.5 rounded-full ${h.dot}`} /> {h.text}
              {!status?.rh_feed_alive && (local.rh_feed_enabled ?? true) && (
                <span className="normal-case tracking-normal text-neutral-500 max-w-[220px] truncate" data-testid="feed-rh-why">— {h.why}</span>
              )}
            </span>
          ); })()}
          <span className={`text-[10px] ${(local.rh_feed_enabled ?? true) ? "text-lime-300" : "text-neutral-400"}`} data-testid="feed-rh-desired">
            {(local.rh_feed_enabled ?? true) ? "ON" : "OFF"}
          </span>
          <HelpHint label="Robinhood Chain Feed">
            <div className="space-y-1.5">
              <div>Polls Robinhood Chain&apos;s public RPC (one batched request every 2s) for PONS launchpad events — new launches, curve buys/sells, graduations. Tokens appear in Recent Launches with an <span className="text-lime-300">RH</span> badge and in the scanner&apos;s Robinhood band.</div>
              <div><strong>Paper by default, live when enabled.</strong> RH launches are paper-traded (auto + manual buy); flip "RH Live Trading" below to execute ETH-quoted curves with the Robinhood hot wallet.</div>
              <div className="text-neutral-400">Uses zero Helius credits in either state.</div>
            </div>
          </HelpHint>
        </span>
      </button>
      {/* Doctor pause → feeds: OFF (default) keeps the tape + learning flowing while books are paused;
          ON idles Helius/RH to save credits (nothing can trade anyway). */}
      <button
        type="button"
        data-testid="feed-autopause-toggle"
        onClick={() => {
          const next = !(local.feed_autopause_on_doctor ?? false);
          flipKey("feed_autopause_on_doctor", next, () => toast.success(next
            ? "Feeds will idle while the Doctor has the books paused (saves credits)"
            : "Feeds keep streaming while the Doctor has the books paused"));
        }}
        className={`w-full flex items-center justify-between px-3 py-1.5 border text-[10px] font-mono uppercase tracking-[0.15em] transition-colors ${
          (local.feed_autopause_on_doctor ?? false) ? "border-amber-800 bg-amber-950/20 text-amber-300" : "border-neutral-800 text-neutral-400 hover:bg-neutral-900/60"}`}
      >
        <span className="flex items-center gap-2"><PauseCircle className="w-3.5 h-3.5" /> Idle feeds while Doctor pauses books</span>
        <span className="flex items-center gap-1.5">
          <span data-testid="feed-autopause-state">{(local.feed_autopause_on_doctor ?? false) ? "ON" : "OFF"}</span>
          <HelpHint label="Idle feeds on Doctor pause">
            OFF (default): the Pump.fun WebSocket and the Robinhood poller keep streaming while the live-doctor breakers have
            the books paused — the tape, scanner and Doctor learning keep flowing, you just can&apos;t enter. ON: both feeds idle
            (amber PAUSED · DOCTOR) while every Solana / RH book is paused and nothing is open — saves Helius credits.
          </HelpHint>
        </span>
      </button>

      {/* Robinhood Chain PAPER trader — Phase B. Opt-in; only fires while
          the bot is Running. Separate rh_* gates live below the re-entry
          block; exits reuse the standard TP/SL/trailing/hold config. */}
      <button
        type="button"
        data-testid="rh-paper-toggle"
        onClick={() => {
          const next = !(local.rh_paper_enabled ?? false);
          flipKey("rh_paper_enabled", next, () => toast.success(next ? "RH paper trading ON — fires while bot is running" : "RH paper trading OFF"));
        }}
        className={`w-full flex items-center justify-between px-3 py-2 border text-xs uppercase tracking-[0.15em] font-mono transition-colors duration-100 ${
          (local.rh_paper_enabled ?? false)
            ? "border-lime-700 text-lime-200 bg-lime-950/50 hover:bg-lime-900/50"
            : "border-neutral-700 text-neutral-400 bg-neutral-900/50 hover:bg-neutral-800/60"
        }`}
      >
        <span className="flex items-center gap-2">
          <Zap className="w-3 h-3" />
          RH Paper Trading
        </span>
        <span className="flex items-center gap-1.5">
          <span className={`text-[10px] ${(local.rh_paper_enabled ?? false) ? "text-lime-200" : "text-neutral-400"}`}>
            {(local.rh_paper_enabled ?? false) ? (status?.enabled ? "ARMED" : "ARMED · BOT STOPPED") : "OFF"}
          </span>
          <HelpHint label="RH Paper Trading">
            <div className="space-y-1.5">
              <div>Simulates entries on PONS launches that pass the <strong>RH Paper Gates</strong> (below). Stake = Max Trade USD. Exits reuse your TP / SL / trailing / max-hold.</div>
              <div>Realism: 1% PONS curve fee both legs, launch-window snipe tax, RH gas, and your paper entry/exit latency — fills use the price seen <em>after</em> the delay.</div>
              <div className="text-neutral-400">Paper only. No wallet, no Helius. Trades land in Trade History / Active Trades / P&amp;L-by-source with an RH badge.</div>
            </div>
          </HelpHint>
        </span>
      </button>

      {/* RH LIVE trading — real ETH on ETH-quoted PONS curves via the RH hot wallet */}
      <button
        type="button"
        data-testid="rh-live-toggle"
        onClick={() => {
          const next = !(local.rh_live_trading ?? false);
          if (next && !window.confirm("Enable RH LIVE trading? Real ETH from the Robinhood hot wallet will buy ETH-quoted PONS curves that pass the RH gates. The RH daily kill switch and gas reserve apply.")) return;
          flipKey("rh_live_trading", next,
            () => toast[next ? "warning" : "success"](next ? "RH LIVE trading ON — real ETH in play" : "RH live trading OFF"),
            (e) => e?.response?.data?.detail);
        }}
        className={`w-full flex items-center justify-between px-3 py-2 border text-xs uppercase tracking-[0.15em] font-mono transition-colors duration-100 ${
          (local.rh_live_trading ?? false)
            ? "border-rose-700 text-rose-200 bg-rose-950/50 hover:bg-rose-900/50"
            : "border-neutral-700 text-neutral-400 bg-neutral-900/50 hover:bg-neutral-800/60"
        }`}
      >
        <span className="flex items-center gap-2">
          <Zap className="w-3 h-3" />
          RH Live Trading
        </span>
        <span className="flex items-center gap-1.5">
          <span className={`text-[10px] ${(local.rh_live_trading ?? false) ? "text-rose-200" : "text-neutral-400"}`}>
            {(local.rh_live_trading ?? false) ? (status?.enabled ? "LIVE" : "ARMED · BOT STOPPED") : "OFF"}
          </span>
          <HelpHint label="RH Live Trading">
            <div className="space-y-1.5">
              <div>Executes the same RH entries/exits with <strong>real ETH</strong> from the Robinhood hot wallet (see the RH Wallet card): direct <code>buy()</code>/<code>sell()</code> on the PONS curve, on-chain simulation before every send, slippage guard, receipt-confirmed fills. Gas + curve fee are booked into P/L so the Doctor learns true costs.</div>
              <div>ETH-quoted curves only — stock/USDG-quoted launches stay paper. Independent of Solana live trading. Auto-off when today's live RH loss hits the RH kill switch.</div>
            </div>
          </HelpHint>
        </span>
      </button>
      <div className="grid grid-cols-2 gap-2">
        <Field label="Rug sell ≥ $" testid="rh-rug-sell-usd-input" hint="Sequencer-feed rug detector: a single sell of at least this USD value on a curve you hold exits immediately, before the 2s poll sees it."
               value={local.rh_rug_sell_usd ?? 300} onChange={(v) => setLocal({ ...local, rh_rug_sell_usd: parseFloat(v) || 0 })} step="50" />
        <Field label="Rug sell ≥ % price drop" testid="rh-rug-sell-pct-input" hint="...or an ordered sell that the exact curve math says will knock the price down by at least this % (PONS curve: (net ETH + 1.68 virtual) × tokens = constant)."
               value={local.rh_rug_sell_curve_pct ?? 15} onChange={(v) => setLocal({ ...local, rh_rug_sell_curve_pct: parseFloat(v) || 0 })} step="5" />
      </div>
      {(local.rh_live_trading ?? false) && (
        <button
          type="button"
          data-testid="rh-live-erc20-toggle"
          onClick={() => {
            const next = !(local.rh_live_erc20_quotes ?? false);
            if (next && !window.confirm("Enable live buys on USDG / stock-quoted RH curves and pools? The wallet must already hold the quote asset (USDG, MSFT, NVDA…) — the bot spends what is there and never converts ETH into it. ETH is still needed for gas.")) return;
            flipKey("rh_live_erc20_quotes", next,
              () => toast[next ? "warning" : "success"](next ? "ERC-20 quote live buys ON" : "ERC-20 quote live buys OFF — ETH-quoted only"),
              (e) => e?.response?.data?.detail);
          }}
          className={`w-full flex items-center justify-between px-3 py-1.5 border text-[10px] uppercase tracking-[0.15em] font-mono transition-colors duration-100 ${
            (local.rh_live_erc20_quotes ?? false)
              ? "border-amber-700 text-amber-200 bg-amber-950/40 hover:bg-amber-900/40"
              : "border-neutral-800 text-neutral-500 bg-neutral-900/40 hover:bg-neutral-800/60"
          }`}
        >
          <span>Stock / USDG quotes live</span>
          <span className="flex items-center gap-1.5">
            <span>{(local.rh_live_erc20_quotes ?? false) ? "ON · spends held quote" : "OFF · ETH-quoted only"}</span>
            <HelpHint label="ERC-20 quote live buys">
              <div className="space-y-1.5">
                <div>Lets RH live trading buy curves and graduated pools quoted in <strong>USDG or a tokenized stock</strong> (MSFT, NVDA, TSLA…). Curve buys approve the quote token to the curve; pool buys go through Permit2 → Universal Router, the same v4 hook as ETH pools (verified on-chain: fee 0, tick 200).</div>
                <div>The wallet must already hold the quote asset — see the RH wallet card. Short balance → the entry is skipped as a quote-balance skip, nothing is converted. USDG is 6-decimal; P/L is booked in USD via the live quote prices.</div>
              </div>
            </HelpHint>
          </span>
        </button>
      )}
      {(local.rh_live_trading ?? false) && (
        <div className="grid grid-cols-3 gap-2">
          <Field label="RH slippage %" testid="rh-live-slippage-input" hint="minOut guard on live buys/sells; sells retry with widening slippage, then minOut=0 rather than strand tokens."
                 value={local.rh_live_slippage_pct ?? 8} onChange={(v) => setLocal({ ...local, rh_live_slippage_pct: parseFloat(v) || 0 })} step="1" />
          <Field label="Gas reserve ETH" testid="rh-gas-reserve-input" hint="Never buy below this ETH balance so exits always have gas."
                 value={local.rh_gas_reserve_eth ?? 0.002} onChange={(v) => setLocal({ ...local, rh_gas_reserve_eth: parseFloat(v) || 0 })} step="0.001" />
          <Field label="RH kill switch $" testid="rh-kill-switch-input" hint="Today's realised live RH loss that switches RH live trading off automatically."
                 value={local.rh_daily_kill_switch_usd ?? 20} onChange={(v) => setLocal({ ...local, rh_daily_kill_switch_usd: parseFloat(v) || 0 })} step="5" />
        </div>
      )}

      {/* Speed Mode slider — controls priority fee + slippage as a bundle */}
      <SpeedModeSlider
        value={local.speed_mode || "manual"}
        onChange={(mode) => setLocal({ ...local, speed_mode: mode })}
      />
      <button
        onClick={() => {
          setShowAdvancedFees((v) => !v);
          // If they want manual control, opening Advanced switches mode to manual
          if (!showAdvancedFees && local.speed_mode !== "manual") {
            setLocal({ ...local, speed_mode: "manual" });
          }
        }}
        data-testid="toggle-advanced-fees-btn"
        className="flex items-center gap-1 text-[10px] uppercase tracking-[0.15em] text-neutral-500 hover:text-neutral-300 transition-colors -mt-1"
      >
        {showAdvancedFees ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        Manual fee override
      </button>

      <div className="grid grid-cols-2 gap-2 text-xs">
        <Field label="Min Trade ($)" testid="min-trade-input"
               hint="Floor on USD size per buy. Trades smaller than this are skipped to avoid fee‑drag eating the entire EV on micro positions."
               value={local.min_trade_usd}
               onChange={(v) => setLocal({ ...local, min_trade_usd: parseFloat(v) || 0 })} step="0.1" />
        <Field label="Max Trade ($)" testid="max-trade-input"
               hint="Hard cap on USD size per buy (server ceiling $100). The bot scales position by liquidity/score but never exceeds this."
               value={local.max_trade_usd}
               onChange={(v) => setLocal({ ...local, max_trade_usd: parseFloat(v) || 0 })} step="0.1" />
        {showAdvancedFees && (
          <Field label="Slippage (bps)" testid="slippage-input"
                 hint="Entry slippage tolerance in basis points (100 = 1%). Speed Mode normally sets this; only override if you know why."
                 value={local.slippage_bps}
                 onChange={(v) => setLocal({ ...local, slippage_bps: parseInt(v, 10) || 0 })} step="50" />
        )}
        <Field label="Kill Switch ($)" testid="killswitch-input"
               hint="If LIVE PnL drops by this much in a single day, the bot auto-disables. Manual reset required."
               value={local.daily_kill_switch_usd}
               onChange={(v) => setLocal({ ...local, daily_kill_switch_usd: parseFloat(v) || 0 })} step="1" />
        <label className="flex flex-col gap-1 justify-end" data-testid="resume-on-restart-field">
          <span className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 inline-flex items-center gap-1">
            Resume after restart
            <HelpHint label="help: resume after restart">Deployed app: the backend restarts on deploys and pod reschedules. ON = if the bot was running, it keeps running (positions re-attached) — you don't need the browser open. OFF = old behaviour: auto-disable for safety until you press Start.</HelpHint>
          </span>
          <span className="inline-flex items-center gap-2 text-xs text-neutral-300 h-[30px]">
            <input type="checkbox" data-testid="resume-on-restart-toggle" checked={local.resume_on_restart !== false}
                   onChange={(e) => setLocal({ ...local, resume_on_restart: e.target.checked })} /> keep trading through restarts
          </span>
        </label>
        <Field label="No-Mo Check (s)" testid="no-momentum-after-input"
               hint="No-momentum exit: one-shot check this many seconds after entry (all books). If the position never reached the MFE floor below, exit as 'no-momentum'. Positions that already banked a ladder leg are exempt. This flattens a dead runner — it is not a clock. Set 0 to disable."
               value={local.no_momentum_after_s ?? 30}
               onChange={(v) => { const n = parseInt(v, 10) || 0; setLocal({ ...local, no_momentum_after_s: n, no_momentum_exit_enabled: n > 0 }); }} step="5" />
        <Field label="No-Mo MFE (%)" testid="no-momentum-mfe-input"
               hint="Minimum peak gain (max favourable excursion) the position must have shown by the check time to be allowed to keep running."
               value={local.no_momentum_min_mfe_pct ?? 5}
               onChange={(v) => setLocal({ ...local, no_momentum_min_mfe_pct: parseFloat(v) || 0 })} step="1" />
        <Field label="Recovery Watch (s)" testid="recovery-watch-input"
               hint="When the no-momentum check would kill a RED position whose tape is recovering (above its price 30s ago, and no new low for 20s or ≥2 fresh buyers), the kill becomes a time-boxed watch instead: stop just under the trough, this many seconds to reclaim part of the way back to entry, then it rejoins the normal ladder with a fresh clock. Flat or still-sliding tapes are still killed. 0 = off."
               value={local.recovery_watch_s ?? 90}
               onChange={(v) => { const n = parseInt(v, 10) || 0; setLocal({ ...local, recovery_watch_s: n, recovery_watch_enabled: n > 0 }); }} step="15" />
        <Field label="Recovery Stop (%)" testid="recovery-stop-input"
               hint="During a recovery watch the stop sits this far below the trough the position already survived — the bounded downside of waiting."
               value={local.recovery_stop_below_trough_pct ?? 3}
               onChange={(v) => setLocal({ ...local, recovery_stop_below_trough_pct: parseFloat(v) || 0 })} step="1" />
        <Field label="Recovery Reclaim" testid="recovery-reclaim-input"
               hint="Fraction of the distance from the trough back to entry the price must reclaim to end the watch and rejoin the ladder. 0.5 = halfway back; 1.0 = back to entry."
               value={local.recovery_reclaim_frac ?? 0.5}
               onChange={(v) => setLocal({ ...local, recovery_reclaim_frac: Math.min(1, Math.max(0, parseFloat(v) || 0)) })} step="0.1" />
        <Field label="Mom Gate Flow (%)" testid="exit-momentum-flow-input"
               hint="Net-flow momentum: buys − sells over the gate window as % of the curve's liquidity. The momentum gate defers a stop / target only while net inflow is at least this much (1 = 1% of the pool's SOL/ETH per window). Size-aware replacement for wallet counts (bundlers fake counts, not net money). Buyers count is used only when liquidity is unknown."
               value={local.exit_momentum_min_flow_pct ?? 1} onChange={(v) => setLocal({ ...local, exit_momentum_min_flow_pct: parseFloat(v) || 0 })} step="0.5" />
        <Field label="Entry Flow ≥ (%)" testid="scanner-min-flow-input"
               hint="Entry gate: net inflow over the last 30s must be at least this % of curve liquidity. Velocity says the price moved; flow says money is behind it. 0 = off."
               value={local.scanner_min_flow_ratio_pct ?? 2} onChange={(v) => setLocal({ ...local, scanner_min_flow_ratio_pct: parseFloat(v) || 0 })} step="0.5" />
        <Field label="Flush Floor × Range" testid="flush-range-floor-input"
               hint="Flush hold floor = max(Flush Extra Drop %, this × the last-60s high-low range %). On a token that just swung 40% the floor becomes ~14% instead of 5%, so a whale's flush doesn't stop you out 2% under the trough. Distribution (many sellers) still exits immediately."
               value={local.flush_range_floor_mult ?? 0.35} onChange={(v) => setLocal({ ...local, flush_range_floor_mult: parseFloat(v) || 0 })} step="0.05" />
        <Field label="Mom Gate Buyers" testid="exit-momentum-buyers-input"
               hint="Buy-momentum exit gate: SL and TP are DEFERRED while at least this many distinct wallets bought in the momentum window AND the inflow floor is met — so you don't sell into a dip that buyers are still absorbing. Set 0 to disable."
               value={local.exit_momentum_min_buyers ?? 3}
               onChange={(v) => { const n = parseInt(v, 10) || 0; setLocal({ ...local, exit_momentum_min_buyers: n, exit_momentum_gate_enabled: n > 0 }); }} step="1" />
        <Field label="Mom Gate SOL" testid="exit-momentum-inflow-input"
               hint="Minimum SOL bought in the momentum window (default 10s) for buy pressure to count as 'still strong'."
               value={local.exit_momentum_min_inflow_sol ?? 0.25}
               onChange={(v) => setLocal({ ...local, exit_momentum_min_inflow_sol: parseFloat(v) || 0 })} step="0.05" />
        <Field label="Mom Defer Max (s)" testid="exit-momentum-defer-input"
               hint="Longest an SL/TP can be deferred on momentum before it fires anyway. A deferred SL is also capped at SL + 'SL Defer Max Extra %' — that bound is the only floor."
               value={local.exit_momentum_max_defer_s ?? 20}
               onChange={(v) => setLocal({ ...local, exit_momentum_max_defer_s: parseInt(v, 10) || 0 })} step="5" />
        <Field label="SL Defer Max Extra %" testid="exit-momentum-extra-loss-input"
               hint="While an SL is deferred on momentum, fire anyway once the loss runs this many points past the SL line. SL 20 + 5 ⇒ a deferred stop can never fill below −25% (on both SOL and RH). Also: RH deferral now needs net buy inflow, not just a few buyers."
               value={local.exit_momentum_max_extra_loss_pct ?? 5}
               onChange={(v) => setLocal({ ...local, exit_momentum_max_extra_loss_pct: parseFloat(v) || 0 })} step="1" />
        <label className="flex flex-col gap-1" data-testid="flush-scope-field">
          <span className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 inline-flex items-center gap-1">
            Flush hold
            <HelpHint label="help: flush hold">Robinhood: when a fast dip trips the SL or trailing stop, the bot checks WHO sold since the peak. If one wallet did ≥ the top-share of the selling (≤ max sellers) and buyers are still stepping in, it is a flush of weak hands, not distribution — the exit is held up to Flush hold (s), floored at Flush extra drop % under the flush trough. It also asks the chain whether the flusher emptied their bag. A stop that WAS caused by a flush primes a recovery re-entry watch instead of ending the token. Scope: hot tokens + re-entry legs, or every position.</HelpHint>
          </span>
          <select data-testid="flush-scope-select" value={local.flush_hold_enabled === false ? "off" : (local.flush_hold_scope ?? "hot_reentry")}
                  onChange={(e) => { const v = e.target.value; setLocal({ ...local, flush_hold_enabled: v !== "off", flush_hold_scope: v === "off" ? (local.flush_hold_scope ?? "hot_reentry") : v }); }}
                  className="bg-neutral-950 border border-neutral-800 px-2 py-1 font-mono text-sm focus:border-blue-500 focus:outline-none">
            <option value="hot_reentry">hot + re-entries</option>
            <option value="all">all positions</option>
            <option value="off">off</option>
          </select>
        </label>
        <Field label="Flush hold (s)" testid="flush-hold-s-input"
               hint="Longest an SL/trail is held while the dip still looks like a single-seller flush. The Doctor tunes this from the flush scorecard."
               value={local.flush_hold_s ?? 10}
               onChange={(v) => setLocal({ ...local, flush_hold_s: parseInt(v, 10) || 0 })} step="5" />
        <Field label="Flush top share" testid="flush-top-share-input"
               hint="Share of the dip's sell volume one wallet must account for (0.7 = 70%) to call it a flush."
               value={local.flush_top_share ?? 0.7}
               onChange={(v) => setLocal({ ...local, flush_top_share: parseFloat(v) || 0 })} step="0.05" />
        <Field label="Flush max sellers" testid="flush-max-sellers-input"
               hint="More distinct sellers than this in the dip = distribution, never a flush."
               value={local.flush_max_sellers ?? 2}
               onChange={(v) => setLocal({ ...local, flush_max_sellers: parseInt(v, 10) || 0 })} step="1" />
        <Field label="Flush extra drop %" testid="flush-extra-drop-input"
               hint="Floor while holding a flush: if price falls this far below the flush trough, sell anyway — it was distribution after all."
               value={local.flush_extra_drop_pct ?? 5}
               onChange={(v) => setLocal({ ...local, flush_extra_drop_pct: parseFloat(v) || 0 })} step="1" />
        {showAdvancedFees && (
          <Field label="Priority µLamp" testid="prio-input"
                 hint="Compute-unit price in micro-lamports. Higher = better landing odds, higher fee. Speed Mode handles this; manual override only."
                 value={local.priority_fee_microlamports}
                 onChange={(v) => setLocal({ ...local, priority_fee_microlamports: parseInt(v, 10) || 0 })} step="100000" />
        )}
        {showAdvancedFees && (
          <Field label="Exit Slip (bps)" testid="exit-slip-input"
                 hint="Slippage tolerance for sells. Higher = better landing in fast dumps; lower = preserves more value on the way out."
                 value={local.exit_slippage_bps}
                 onChange={(v) => setLocal({ ...local, exit_slippage_bps: parseInt(v, 10) || 0 })} step="50" />
        )}
      </div>

      <BookExitsEditor local={local} setLocal={setLocal} onRestored={(bx) => {
        // reset form + baseline + the parent's config together so the sync effect can't re-apply a stale prop
        const next = { ...local, book_exits: bx };
        setLocal(next); setBaseline(next); onConfigLoaded?.({ ...config, book_exits: bx });
      }} />

      {/* Portfolio / global entry settings (per-band liquidity & buyer thresholds live in the gates table below) */}
      <div className="border-t border-neutral-800 pt-3 mt-1">
        <div className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 mb-2">Portfolio</div>
        <div className="grid grid-cols-2 gap-2 text-xs">
          <Field label="Max Positions" testid="max-positions-input"
                 hint="Maximum concurrent open positions. The bot won't enter a new buy while at this limit."
                 value={local.max_concurrent_positions}
                 onChange={(v) => setLocal({ ...local, max_concurrent_positions: parseInt(v, 10) || 0 })} step="1" />
        </div>
      </div>

      {/* Momentum scanner config */}
      <div className="border-t border-neutral-800 pt-3 mt-1">
        <div className="flex items-center justify-between mb-2">
          <span className="text-[10px] uppercase tracking-[0.15em] text-neutral-500">
            Momentum Scanner
            <HelpHint label="Momentum Scanner — Per-band protocol split">
              <div className="space-y-1">
                <div><span className="text-amber-300">NEW</span> band = tokens on the Pump.fun bonding curve (pumpfun protocol only).</div>
                <div><span className="text-cyan-300">SEASONED</span> band = tokens that have graduated to the PumpSwap AMM (pumpswap protocol only). Age is measured from the <em>graduation</em> moment.</div>
                <div className="text-neutral-400">Pumpfun tokens that age past the New cap DO NOT roll into Seasoned unless they graduate first.</div>
              </div>
            </HelpHint>
          </span>
          <label className="flex items-center gap-1.5 text-[10px] font-mono uppercase text-neutral-400">
            <input
              type="checkbox"
              data-testid="scanner-enabled-checkbox"
              checked={!!local.scanner_enabled}
              onChange={(e) => flipKey("scanner_enabled", e.target.checked, () => toast.success(e.target.checked ? "SOL scanner ON — auto-entries armed" : "SOL scanner OFF — no auto-entries"))}
            />
            enabled
          </label>
          <label className="flex items-center gap-1.5 text-[10px] font-mono uppercase text-neutral-400" title="Trail ratchet: once a trade is +15% the trail is capped at 6%, at +30% at 4% — overrides your Trail % / Arm %. OFF = your per-book values are used exactly as set.">
            <input type="checkbox" data-testid="trail-ratchet-checkbox" checked={!!local.trail_ratchet_enabled}
              onChange={(e) => flipKey("trail_ratchet_enabled", e.target.checked, () => toast.success(e.target.checked ? "Trail ratchet ON — trails tighten automatically at +15% / +30%" : "Trail ratchet OFF — per-book Trail % / Arm % used as set"))} />
            trail ratchet
          </label>
          <label className="flex items-center gap-1.5 text-[10px] font-mono uppercase text-neutral-400" title="Seasoned-band (graduated PumpSwap pool) entries → hunt book. Off = new-band scalps only.">
            <input
              type="checkbox"
              data-testid="scanner-seasoned-entries-checkbox"
              checked={local.scanner_seasoned_entries_enabled !== false}
              onChange={(e) => flipKey("scanner_seasoned_entries_enabled", e.target.checked, () => toast.success(e.target.checked ? "SEASONED entries ON — graduated pools can enter the hunt book" : "SEASONED entries OFF — new-band only"))}
            />
            seasoned entries
          </label>
        </div>
        <div className="grid grid-cols-2 gap-2 text-xs">
          <Field label="New Min Age (m)" testid="band-new-min-age-input"
                 hint="Lower bound of the NEW band (Pump.fun bonding-curve only). Tokens younger than this are excluded from the scanner. Decimal minutes supported."
                 value={local.band_new_min_age_min ?? 0}
                 onChange={(v) => setLocal({ ...local, band_new_min_age_min: Math.max(0, parseFloat(v) || 0) })} step="0.25" />
          <Field label="New Max Age (m)" testid="band-new-max-age-input"
                 hint="Upper bound of the NEW band (Pump.fun bonding-curve only). Pumpfun tokens older than this fall OFF the scanner entirely — they DO NOT roll over into Seasoned unless graduated."
                 value={local.band_new_max_age_min ?? 15}
                 onChange={(v) => setLocal({ ...local, band_new_max_age_min: Math.max(0, parseFloat(v) || 0) })} step="1" />
          <Field label="Seasoned Min Age (m)" testid="band-seasoned-min-age-input"
                 hint="Lower bound of the SEASONED band (PumpSwap AMM only). Counted from the GRADUATION moment, not launch. Tokens just-graduated under this floor are excluded."
                 value={local.band_seasoned_min_age_min ?? 0}
                 onChange={(v) => setLocal({ ...local, band_seasoned_min_age_min: Math.max(0, parseFloat(v) || 0) })} step="0.25" />
          <Field label="Seasoned Max Age (m)" testid="band-seasoned-max-age-input"
                 hint="Upper bound of the SEASONED band (PumpSwap AMM only). Tokens that graduated further back than this fall off the scanner. The high-EV window is typically 0–30 min post-graduation."
                 value={local.band_seasoned_max_age_min ?? 60}
                 onChange={(v) => setLocal({ ...local, band_seasoned_max_age_min: Math.max(0, parseFloat(v) || 0) })} step="1" />
          <Field label="Scan every (s)" testid="scanner-interval-input"
                 hint="How often the scanner loop re-evaluates all tracked tokens. Backend enforces a 5s minimum — values below 5 are clamped to 5 to avoid frontend OOM from rapid metric broadcasts. SL/TP reactions on open positions are NOT controlled by this — they use LaserStream WSS push events."
                 value={local.scanner_interval_s}
                 onChange={(v) => setLocal({ ...local, scanner_interval_s: parseInt(v, 10) || 0 })} step="5" />
          <Field label="Inflow Win (s)" testid="scanner-inflow-window-input"
                 hint="Rolling window used to sum SOL inflow into the bonding curve. Compared against 'Min Inflow' gate."
                 value={local.scanner_recent_inflow_window_s}
                 onChange={(v) => setLocal({ ...local, scanner_recent_inflow_window_s: parseInt(v, 10) || 0 })} step="30" />
          <Field label="Max Idle (min)" testid="scanner-max-idle-input"
                 hint="Drop a discovered token from the scanner if no trades land within this idle window. Saves cycles on dead tokens."
                 value={local.scanner_discovery_max_idle_minutes}
                 onChange={(v) => setLocal({ ...local, scanner_discovery_max_idle_minutes: parseInt(v, 10) || 0 })} step="1" />
          <Field label="Entry Vel Win (s)" testid="scanner-entry-vel-window-input"
                 hint="Window used to compute the just-before-entry price velocity. Filters out stale momentum that's already faded."
                 value={local.scanner_entry_velocity_window_s}
                 onChange={(v) => setLocal({ ...local, scanner_entry_velocity_window_s: parseInt(v, 10) || 0 })} step="5" />
          <Field label="Min Entry Vel (%)" testid="scanner-entry-vel-min-input"
                 hint="Minimum % price move within the entry-velocity window for a buy to fire. Keeps the bot on the rising edge."
                 value={local.scanner_entry_velocity_min_pct}
                 onChange={(v) => setLocal({ ...local, scanner_entry_velocity_min_pct: parseFloat(v) || 0 })} step="0.5" />
          <Field label="SL Cooldown (min)" testid="sl-cooldown-input"
                 hint="After a stop-loss exit on a token, ignore that mint for this many minutes. Prevents re-buying into a continuing dump."
                 value={local.sl_cooldown_minutes}
                 onChange={(v) => setLocal({ ...local, sl_cooldown_minutes: parseFloat(v) || 0 })} step="0.5" />
        </div>

        {/* Seasoned supply: recently-graduated feed */}
        <div className="mt-3 border-t border-neutral-800 pt-2.5">
          <label className="flex items-center justify-between text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-400 cursor-pointer">
            <span className="flex items-center gap-1.5">
              <input
                type="checkbox"
                data-testid="scanner-graduated-feed-checkbox"
                checked={local.scanner_graduated_feed_enabled !== false}
                onChange={(e) => setLocal({ ...local, scanner_graduated_feed_enabled: e.target.checked })}
              />
              Graduated feed (Seasoned supply)
              <HelpHint label="Graduated feed">
                Every 60s pull Pump.fun's recently-graduated tokens and seed them as PumpSwap candidates so the Seasoned band has supply beyond the ~1% of launches the bot watched graduate.
              </HelpHint>
            </span>
            <span className="text-neutral-600 normal-case tracking-normal">
              poll every 60s · up to 30 mints/cycle
            </span>
          </label>
        </div>

        {/* Distribution-vacuum gate (insider pre-distribution filter) */}
        <div className="mt-3 border-t border-neutral-800 pt-2.5">
          <label className="flex items-center justify-between text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-400 cursor-pointer">
            <span className="flex items-center gap-1.5">
              <input
                type="checkbox"
                data-testid="gate-distribution-vacuum-checkbox"
                checked={local.gate_distribution_vacuum}
                onChange={(e) => setLocal({ ...local, gate_distribution_vacuum: e.target.checked })}
              />
              Distribution vacuum filter
              <HelpHint label="Distribution vacuum filter">
                Blocks entries where every holder appeared inside the same recent window — a classic pattern for a single insider seeding wallets before the dump.
              </HelpHint>
            </span>
            <span className="text-neutral-600 normal-case tracking-normal">
              reject if all holders appeared in last window
            </span>
          </label>
          <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
            <Field label="Min Holders" testid="gate-distribution-min-input"
                   hint="Token must have at least this many unique holders before the vacuum check applies."
                   value={local.gate_distribution_min_holders}
                   onChange={(v) => setLocal({ ...local, gate_distribution_min_holders: parseInt(v, 10) || 0 })} step="1" />
          </div>
        </div>

        {/* Socials gate */}
        <div className="mt-3 border-t border-neutral-800 pt-2.5">
          <label className="flex items-center justify-between text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-400 cursor-pointer">
            <span className="flex items-center gap-1.5">
              <input
                type="checkbox"
                data-testid="gate-socials-required-checkbox"
                checked={local.gate_socials_required}
                onChange={(e) => setLocal({ ...local, gate_socials_required: e.target.checked })}
              />
              Socials required for entry
              <HelpHint label="Socials gate">
                Requires the token to have at least one social link (twitter/telegram/website) AND a Pump.fun reply count ≥ the threshold below. Filters out totally faceless launches.
              </HelpHint>
            </span>
            <span className="text-neutral-600 normal-case tracking-normal">
              twitter / telegram / website + reply_count
            </span>
          </label>
          <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
            <Field label="Min Reply Count" testid="gate-min-replies-input"
                   hint="Pump.fun comment-thread reply count required. Pure spam tokens usually have 0–2 replies."
                   value={local.gate_min_reply_count}
                   onChange={(v) => setLocal({ ...local, gate_min_reply_count: parseInt(v, 10) || 0 })} step="5" />
          </div>
        </div>

        {/* Greylist Sniper */}
        <div className="mt-3 border-t border-neutral-800 pt-2.5">
          <label className="flex items-center justify-between text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-400 cursor-pointer">
            <span className="flex items-center gap-1.5">
              <input
                type="checkbox"
                data-testid="greylist-snipe-enabled-checkbox"
                checked={local.greylist_snipe_enabled}
                onChange={(e) => setLocal({ ...local, greylist_snipe_enabled: e.target.checked })}
              />
              Greylist Sniper
              <HelpHint label="Greylist Sniper">
                Fires on every NEW launch where the creator scored ≥ Min Score on the greylist. Bypasses momentum gates (growth/inflow/buyers/velocity) since greylisted creators rarely pump organically — the entire point is sniping their predictable curve. Still honors kill switch + max-positions + cooldowns.
              </HelpHint>
            </span>
            <span className="text-neutral-600 normal-case tracking-normal">
              snipe greylist creators on every launch
            </span>
          </label>
          <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
            <Field label="Min Score" testid="greylist-snipe-min-score-input"
                   hint="Effective (decayed) greylist score required to fire. 45 = hybrid threshold, 70 = aggressive threshold."
                   value={local.greylist_snipe_min_score}
                   onChange={(v) => setLocal({ ...local, greylist_snipe_min_score: parseFloat(v) || 0 })} step="5" />
            <Field label="Max/hr" testid="greylist-snipe-max-per-hour-input"
                   hint="Rolling 1h fire cap. Safety net so a wave of greylist launches can't blow through the wallet."
                   value={local.greylist_snipe_max_per_hour}
                   onChange={(v) => setLocal({ ...local, greylist_snipe_max_per_hour: parseInt(v, 10) || 0 })} step="1" />
            <Field label="Settle (s)" testid="greylist-snipe-settle-seconds-input"
                   hint="Wait this long after launch detection before buying. Lets the tracking bucket populate first-seen price / liquidity."
                   value={local.greylist_snipe_settle_seconds}
                   onChange={(v) => setLocal({ ...local, greylist_snipe_settle_seconds: parseInt(v, 10) || 0 })} step="1" />
          </div>
          <div className="mt-2 text-[10px] uppercase tracking-[0.15em] text-neutral-500">Pattern-based exits (no SL, no max-hold)</div>
          <div className="mt-1 grid grid-cols-4 gap-2 text-xs">
            <Field label="Peak MC %" testid="greylist-snipe-peak-mc-input"
                   hint="Exit when current MC reaches this % of the creator's typical peak MC. Lower = earlier exit before the predictable rug. 85% is a safe default."
                   value={local.greylist_snipe_peak_mc_proximity_pct}
                   onChange={(v) => setLocal({ ...local, greylist_snipe_peak_mc_proximity_pct: parseFloat(v) || 0 })} step="5" />
            <Field label="Curve Buffer pp" testid="greylist-snipe-curve-buf-input"
                   hint="Exit when curve fill % is within this many points of the creator's typical rug curve %. 5pp = exit at rug_pct - 5. Higher = earlier exit."
                   value={local.greylist_snipe_curve_buffer_pct}
                   onChange={(v) => setLocal({ ...local, greylist_snipe_curve_buffer_pct: parseFloat(v) || 0 })} step="1" />
            <Field label="Ripcord %" testid="greylist-snipe-ripcord-input"
                   hint="Emergency exit when price drops this much FROM OBSERVED PEAK (not from entry). 60% means the rug already happened — bail. NOT an entry-loss SL."
                   value={local.greylist_snipe_ripcord_drawdown_pct}
                   onChange={(v) => setLocal({ ...local, greylist_snipe_ripcord_drawdown_pct: parseFloat(v) || 0 })} step="5" />
            <Field label="Ripcord Grace (s)" testid="greylist-snipe-grace-input"
                   hint="Ripcord requires the drawdown to be sustained this many seconds before firing. Kills wick-driven false exits."
                   value={local.greylist_snipe_ripcord_grace_seconds}
                   onChange={(v) => setLocal({ ...local, greylist_snipe_ripcord_grace_seconds: parseInt(v, 10) || 0 })} step="1" />
          </div>

          {/* Profit ripcord + velocity decay exits */}
          <div className="mt-3 border-t border-dashed border-neutral-800 pt-2.5">
            <div className="text-[10px] uppercase tracking-[0.15em] text-emerald-400/80 mb-1.5 flex items-center gap-1.5">
              Profit Ripcord & Velocity Decay
              <HelpHint label="Snipe-only exits beyond pattern TP">
                These are sniper-only exit gates that run BEFORE the pattern/curve/peak-MC checks:
                <br /><br />
                <b>Profit Ripcord</b> — hard TP at X% above entry. Always wins over pattern TP. Defaults to 100% (lock the 2x before the rug erases it). Set to 0 to disable.
                <br /><br />
                <b>SOL-velocity decay</b> — exit when SOL inflow rate in the last N sec drops by X% vs the prior baseline window. The buying wave is exhausting → rug imminent.
                <br /><br />
                <b>New-holder velocity decay</b> — exit when fresh unique buyers / sec collapses vs baseline. FOMO has dried up → no one left to dump on.
                <br /><br />
                Both decay gates require a minimum number of buys in the baseline window to avoid cold-start false exits.
              </HelpHint>
            </div>
            <div className="grid grid-cols-3 gap-2 text-xs">
                            <Field label="SOL Vel Drop %" testid="greylist-snipe-sol-vel-drop-input"
                     hint="Exit when recent SOL inflow rate is below (100% − this) of the baseline rate. 70 = exit when SOL/s falls to 30% or less of baseline."
                     value={local.greylist_snipe_sol_vel_drop_pct}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_sol_vel_drop_pct: parseFloat(v) || 0 })} step="5" />
              <Field label="Holder Vel Drop %" testid="greylist-snipe-holder-vel-drop-input"
                     hint="Exit when fresh new-buyer rate falls by this % vs baseline. 70 = exit when new-holders/s falls to 30% or less of baseline."
                     value={local.greylist_snipe_holder_vel_drop_pct}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_holder_vel_drop_pct: parseFloat(v) || 0 })} step="5" />
            </div>
            <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
              <Field label="Recent Window (s)" testid="greylist-snipe-vel-window-input"
                     hint="Recent activity window for velocity comparison. Smaller = faster reaction. 15s default."
                     value={local.greylist_snipe_velocity_window_s}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_velocity_window_s: parseInt(v, 10) || 0 })} step="5" />
              <Field label="Baseline (s)" testid="greylist-snipe-vel-baseline-input"
                     hint="Prior baseline window length, ending at the start of the recent window. Larger = more stable comparison."
                     value={local.greylist_snipe_velocity_baseline_s}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_velocity_baseline_s: parseInt(v, 10) || 0 })} step="15" />
              <Field label="Min Baseline Buys" testid="greylist-snipe-vel-min-buys-input"
                     hint="Velocity decay only fires when the baseline window has ≥ this many buys. Cold-start guard. 8 default."
                     value={local.greylist_snipe_velocity_min_buys}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_velocity_min_buys: parseInt(v, 10) || 0 })} step="1" />
            </div>
            <label className="mt-2 flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-[0.15em] text-neutral-400 cursor-pointer">
              <input
                type="checkbox"
                data-testid="greylist-snipe-velocity-exits-checkbox"
                checked={!!local.greylist_snipe_velocity_exits_enabled}
                onChange={(e) => setLocal({ ...local, greylist_snipe_velocity_exits_enabled: e.target.checked })}
              />
              Velocity Exits Enabled
              <span className="text-neutral-600 normal-case tracking-normal ml-1">(both gates on/off — profit ripcord stays independent)</span>
            </label>

            {/* Stale-snipe & pattern-required gates (P0) */}
            <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
              <Field label="Stale Exit (s)" testid="greylist-snipe-stale-seconds-input"
                     hint="Auto-exit a snipe held longer than this many seconds without reaching the Stale Min Profit. Default 90s — paper data showed 10-30min holds drifting to -20-45%. Set 0 to disable."
                     value={local.greylist_snipe_stale_seconds}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_stale_seconds: parseInt(v, 10) || 0 })} step="15" />
              <Field label="Stale Min Profit %" testid="greylist-snipe-stale-min-profit-input"
                     hint="If a snipe is past Stale Exit (s) and is below this % from entry, exit. Default 25%. A snipe that hasn't popped this much in 90s is almost always going to die."
                     value={local.greylist_snipe_stale_min_profit_pct}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_stale_min_profit_pct: parseFloat(v) || 0 })} step="5" />
            </div>
            <label className="mt-2 flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-[0.15em] text-amber-400/80 cursor-pointer">
              <input
                type="checkbox"
                data-testid="greylist-snipe-require-classified-pattern-checkbox"
                checked={!!local.greylist_snipe_require_classified_pattern}
                onChange={(e) => setLocal({ ...local, greylist_snipe_require_classified_pattern: e.target.checked })}
              />
              Require Classified Pattern
              <HelpHint label="Require Classified Pattern">
                When ON, the sniper refuses to fire on creators whose pattern is `unknown` or null. Paper data showed 45/45 unknown-pattern snipes with only 4 winners (9% win rate). The "predictable curve" thesis only holds when the creator HAS a real classified pattern (slow_rug, predictable_dump, fake_hype, bimodal_dump). Research mode bypasses this gate (it deliberately targets the noisy bucket).
              </HelpHint>
            </label>
          </div>

          {/* Research Mode — unpredictable-creator bimodal exploration */}
          <div className="mt-3 border-t border-dashed border-neutral-800 pt-2.5">
            <label className="flex items-center justify-between text-[10px] font-mono uppercase tracking-[0.15em] text-amber-400/80 cursor-pointer">
              <span className="flex items-center gap-1.5">
                <input
                  type="checkbox"
                  data-testid="greylist-snipe-research-mode-checkbox"
                  checked={!!local.greylist_snipe_research_mode}
                  onChange={(e) => setLocal({ ...local, greylist_snipe_research_mode: e.target.checked })}
                />
                Research Mode
                <HelpHint label="Research Mode">
                  EXPERIMENTAL. When ON, the sniper also fires on creators currently classified as `unpredictable_rug` (high curve_fill variance — would normally be blacklisted). Size is automatically halved via Research Size Mult. The bimodal detector still promotes tight 2-cluster creators to `bimodal_dump_tradeable` (full size) — research mode only catches the genuinely chaotic ones so we can collect win-rate data. `untradeable_rug` and `out_of_band` creators stay blocked.
                </HelpHint>
              </span>
              <span className="text-neutral-600 normal-case tracking-normal">
                snipe unpredictable creators at half size
              </span>
            </label>
            <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
              <Field label="Research Min Score" testid="greylist-snipe-research-min-score-input"
                     hint="Effective greylist score required for a RESEARCH snipe (separate floor from the normal Min Score). 35 is the recommended observation threshold — these creators are noisy but tradeable enough to learn from."
                     value={local.greylist_snipe_research_min_score}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_research_min_score: parseFloat(v) || 0 })} step="5" />
              <Field label="Research Size Mult" testid="greylist-snipe-research-size-mult-input"
                     hint="Multiplier applied on top of the risk-bucket size for research snipes. 0.5 = half size. Lower = safer experimental positions. Trades are stamped `is_research_snipe=true` for downstream PnL bucketing."
                     value={local.greylist_snipe_research_size_mult}
                     onChange={(v) => setLocal({ ...local, greylist_snipe_research_size_mult: parseFloat(v) || 0 })} step="0.1" />
            </div>
          </div>
        </div>

        {/* Per-band gates table */}
        <div className="mt-3">
          <div className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 mb-1">Per-band gates</div>
          <div className="border border-neutral-800 text-xs">
            <div className="grid grid-cols-[1.4fr_1fr_1fr] bg-neutral-950 text-[10px] uppercase tracking-[0.15em] text-neutral-500 border-b border-neutral-800">
              <div className="px-2 py-1.5">Gate</div>
              <div className="px-2 py-1.5 text-amber-400">New (&lt; seasoning)</div>
              <div className="px-2 py-1.5 text-cyan-300">Seasoned (≥ seasoning)</div>
            </div>
            <GateRow label="Min Growth (%)"
                     hint="Minimum price growth (from first-seen price) to pass the band. Higher = momentum has to be more obvious before entry."
                     newTestid="scanner-growth-new-input"
                     newValue={local.scanner_min_growth_pct_new}
                     onNewChange={(v) => setLocal({ ...local, scanner_min_growth_pct_new: parseFloat(v) || 0 })}
                     seasonedTestid="scanner-growth-input"
                     seasonedValue={local.scanner_min_growth_pct}
                     onSeasonedChange={(v) => setLocal({ ...local, scanner_min_growth_pct: parseFloat(v) || 0 })}
                     step="5" />
            <GateRow label="Min Liquidity (SOL)"
                     hint="Minimum SOL liquidity on the bonding curve / pool. Filters out micro-cap noise where your own buy moves the price too much."
                     newTestid="min-liq-new-input"
                     newValue={local.min_curve_liquidity_sol_new}
                     onNewChange={(v) => setLocal({ ...local, min_curve_liquidity_sol_new: parseFloat(v) || 0 })}
                     seasonedTestid="min-liq-seasoned-input"
                     seasonedValue={local.min_curve_liquidity_sol}
                     onSeasonedChange={(v) => setLocal({ ...local, min_curve_liquidity_sol: parseFloat(v) || 0 })}
                     step="0.5" />
            {/* New-only: live mempool signals from Helius */}
            <GateRow label="Min Inflow (SOL/win)" newOnly
                     hint="Total SOL flowing into the bonding curve over the 'Inflow Win'. Real cash demand — not just price spikes."
                     newTestid="scanner-inflow-new-input"
                     newValue={local.scanner_min_recent_inflow_sol_new}
                     onNewChange={(v) => setLocal({ ...local, scanner_min_recent_inflow_sol_new: parseFloat(v) || 0 })}
                     step="0.5" />
            <GateRow label="Min new buyers (1m)" newOnly
                     hint="Distinct new wallets that bought in the last 60s. Filters fake pumps driven by one wallet round-tripping."
                     newTestid="scanner-newbuyers-new-input"
                     newValue={local.scanner_min_new_buyers_new}
                     onNewChange={(v) => setLocal({ ...local, scanner_min_new_buyers_new: parseInt(v, 10) || 0 })}
                     step="1" />
            <GateRow label="Min Total Holders" newOnly
                     hint="Minimum unique buyer count on the token. Low holder counts = high rug / one-wallet risk."
                     newTestid="min-buyers-new-input"
                     newValue={local.min_buyers_for_entry_new}
                     onNewChange={(v) => setLocal({ ...local, min_buyers_for_entry_new: parseInt(v, 10) || 0 })}
                     step="1" />
            {/* Seasoned-only: Pump.fun API polled signals */}
            <GateRow label="Min MC ($)" seasonedOnly
                     hint="Minimum USD market cap (from Pump.fun API). Bigger = lower rug risk, lower upside ceiling."
                     seasonedTestid="scanner-mc-seasoned-input"
                     seasonedValue={local.scanner_min_mc_usd_seasoned}
                     onSeasonedChange={(v) => setLocal({ ...local, scanner_min_mc_usd_seasoned: parseFloat(v) || 0 })}
                     step="1000" />
            <GateRow label="Min MC vel (5m %)" seasonedOnly last
                     hint="Minimum % market-cap velocity over the last 5 minutes. Catches Seasoned tokens that are still actively pumping."
                     seasonedTestid="scanner-mcvel-seasoned-input"
                     seasonedValue={local.scanner_min_mc_velocity_5m_pct_seasoned}
                     onSeasonedChange={(v) => setLocal({ ...local, scanner_min_mc_velocity_5m_pct_seasoned: parseFloat(v) || 0 })}
                     step="1" />
          </div>
        </div>
      </div>

      {/* Re-entry config */}
      <div className="border-t border-neutral-800 pt-3 mt-1">
        <div className="flex items-center justify-between mb-2">
          <span className="text-[10px] uppercase tracking-[0.15em] text-neutral-500">Re-entry on winners</span>
          <label className="flex items-center gap-1.5 text-[10px] font-mono uppercase text-neutral-400">
            <input
              type="checkbox"
              data-testid="reentry-enabled-checkbox"
              checked={local.reentry_enabled}
              onChange={(e) => setLocal({ ...local, reentry_enabled: e.target.checked })}
            />
            enabled
          </label>
        </div>
        <div className="grid grid-cols-2 gap-2 text-xs">
          <Field label="Max attempts" testid="reentry-max-input"
                 hint="Maximum number of re-entry buys allowed on a single token after the original exit."
                 value={local.reentry_max_attempts}
                 onChange={(v) => setLocal({ ...local, reentry_max_attempts: parseInt(v, 10) || 0 })} step="1" />
          <Field label="Pullback (%)" testid="reentry-pullback-input"
                 hint="Required pullback (from post-exit local peak) before the bot re-enters. Bigger = wait for deeper dip."
                 value={local.reentry_pullback_pct}
                 onChange={(v) => setLocal({ ...local, reentry_pullback_pct: parseFloat(v) || 0 })} step="1" />
          <Field label="Window (s)" testid="reentry-window-input"
                 hint="Time window after exit during which re-entry is considered. After this, the token falls off the watchlist."
                 value={local.reentry_window_seconds}
                 onChange={(v) => setLocal({ ...local, reentry_window_seconds: parseInt(v, 10) || 0 })} step="30" />
          <Field label="Size ×" testid="reentry-size-input"
                 hint="Position size multiplier for re-entries (e.g., 0.5 = half size). Risk control on a token you already exited once."
                 value={local.reentry_size_multiplier}
                 onChange={(v) => setLocal({ ...local, reentry_size_multiplier: parseFloat(v) || 0 })} step="0.1" />
          <Field label="Hot token ≥ %" testid="hot-token-pnl-input"
                 hint="A winner that exited with at least this % becomes a HOT token: its re-entry watch gets a bigger size, extra attempts and a 2× window so the bot keeps trading it up the chart."
                 value={local.hot_token_pnl_pct ?? 25}
                 onChange={(v) => setLocal({ ...local, hot_token_pnl_pct: parseFloat(v) || 0 })} step="5" />
          <Field label="Hot size ×" testid="hot-reentry-size-input"
                 hint="Extra size multiplier applied to re-entries on HOT tokens (stacked on Size ×). 1.5 = half again as big."
                 value={local.hot_reentry_size_mult ?? 1.5}
                 onChange={(v) => setLocal({ ...local, hot_reentry_size_mult: parseFloat(v) || 0 })} step="0.1" />
          <Field label="Min wait (s)" testid="reentry-min-wait-input"
                 hint="Quiet time after ANY exit on the token before a re-entry may fire on either path. Stops the instant re-buy after a stop-loss."
                 value={local.reentry_min_wait_s ?? 20}
                 onChange={(v) => setLocal({ ...local, reentry_min_wait_s: parseInt(v, 10) || 0 })} step="5" />
          <Field label="Min bounce (%)" testid="reentry-min-bounce-input"
                 hint="The post-exit peak must exceed your exit price by this much before a pullback counts — the token has to keep running after you sold. Otherwise a 'pullback' is just a dump."
                 value={local.reentry_min_bounce_pct ?? 5}
                 onChange={(v) => setLocal({ ...local, reentry_min_bounce_pct: parseFloat(v) || 0 })} step="1" />
          <Field label="Bounce confirm (%)" testid="reentry-bounce-confirm-input"
                 hint="After the pullback, price must lift this much off its trough (with buyers arriving) before the bot buys. No knife-catching."
                 value={local.reentry_bounce_confirm_pct ?? 3}
                 onChange={(v) => setLocal({ ...local, reentry_bounce_confirm_pct: parseFloat(v) || 0 })} step="0.5" />
          <Field label="Min buyers" testid="reentry-min-buyers-input"
                 hint="Distinct buyers in the momentum window (Mom Gate window) required for a pullback re-entry."
                 value={local.reentry_min_buyers ?? 2}
                 onChange={(v) => setLocal({ ...local, reentry_min_buyers: parseInt(v, 10) || 0 })} step="1" />
          <Field label="Breakout (%)" testid="reentry-breakout-input"
                 hint="Breakout path: price above your exit by this much with Mom-Gate-strength buyers + inflow. Skipped if the last leg exited via stop-loss. Set very high to disable."
                 value={local.reentry_breakout_pct ?? 5}
                 onChange={(v) => setLocal({ ...local, reentry_breakout_pct: parseFloat(v) || 0 })} step="1" />
        </div>
        <div className="text-[10px] font-mono text-neutral-600">
          a losing re-entry leg ends a normal watch · a winning leg refreshes it (attempts carry over) · HOT tokens: no cap, no clock — the bot walks away when the chart goes stale (below)
        </div>
        <div className="border border-neutral-800 p-2 space-y-2" data-testid="serial-creator-section">
          <div className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 inline-flex items-center gap-1">
            serial-creator gate (Pump.fun)
            <HelpHint label="help: serial creator gate">Measured on 18,400 launches: creators with several prior launches and NO graduation produce runners 4–8× less often than first launches, while serial creators WITH a graduation launch near first-launch quality. This gate skips tokens whose creator has ≥ N prior launches unless one of them graduated. Both knobs are Doctor-tunable (the ledger measures what the skipped tokens did next).</HelpHint>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 text-xs">
            <label className="flex items-center gap-2 text-neutral-300 sm:col-span-1">
              <input type="checkbox" data-testid="serial-gate-enabled" checked={local.serial_creator_gate_enabled !== false}
                     onChange={(e) => setLocal({ ...local, serial_creator_gate_enabled: e.target.checked })} /> enabled
            </label>
            <Field label="Serial = ≥ N prior launches" testid="serial-min-launches-input"
                   hint="A creator with this many prior launches or more counts as serial. 0 disables the gate."
                   value={local.serial_creator_min_launches ?? 3}
                   onChange={(v) => setLocal({ ...local, serial_creator_min_launches: parseInt(v, 10) || 0 })} step="1" />
            <label className="flex items-center gap-2 text-neutral-300">
              <input type="checkbox" data-testid="serial-requires-grad" checked={local.serial_creator_requires_graduation !== false}
                     onChange={(e) => setLocal({ ...local, serial_creator_requires_graduation: e.target.checked })} /> skip unless a prior launch graduated
            </label>
          </div>
        </div>
        <div className={`border p-2 space-y-2 ${local.creator_audit_enabled ? "border-emerald-800/70" : "border-neutral-800"}`} data-testid="creator-audit-section">
          <div className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 inline-flex items-center gap-1">
            creator wallet audit · master gate {local.creator_audit_enabled ? <span className="text-emerald-400">ON</span> : <span className="text-neutral-600">OFF</span>}
            <HelpHint label="help: creator wallet audit" wiki="creator-audit">Runs LAST, after every other gate, on both chains. A launch is only bought when the creator wallet looks like a person launching one token: funded before the deploy, has used a DEX/launchpad before, main funding landed hours (not seconds) earlier, wallet older than a day, no other deploy in the same batch or hour, no rug / spam / blacklist tag in our greylist, clean metadata. Each check is pass / fail / unavailable; the policy below decides what "unavailable" means. Cached 1h per creator. Solana history comes from Helius (needs credits); Robinhood Chain only exposes balance + nonce + our own feed.</HelpHint>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
            <label className="flex items-center gap-2 text-neutral-300">
              <input type="checkbox" data-testid="creator-audit-enabled" checked={!!local.creator_audit_enabled}
                     onChange={(e) => setLocal({ ...local, creator_audit_enabled: e.target.checked })} /> enabled
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 inline-flex items-center gap-1">unavailable data
                <HelpHint label="help: audit unavailable policy" wiki="creator-audit">PASS = judge the launch on the checks that could run (Helius timeout / RH has no history API → those checks are ignored). SKIP = fail-closed: any check we could not run blocks the entry.</HelpHint></span>
              <select data-testid="creator-audit-unavailable-select" value={local.creator_audit_unavailable ?? "pass"}
                      onChange={(e) => setLocal({ ...local, creator_audit_unavailable: e.target.value })}
                      className="bg-neutral-950 border border-neutral-800 px-2 py-1 text-neutral-100 font-mono text-xs">
                <option value="pass">pass — judge on the rest</option>
                <option value="skip">skip — fail closed</option>
              </select>
            </label>
            <Field label="Funding lead ≥ (h)" testid="creator-audit-lead-input" hint="The largest SOL transfer into the creator wallet before the deploy must have landed at least this many hours earlier. Funded seconds before deploying = throwaway wallet."
                   value={local.creator_audit_min_funding_lead_h ?? 1} onChange={(v) => setLocal({ ...local, creator_audit_min_funding_lead_h: parseFloat(v) || 0 })} step="0.5" />
            <Field label="Wallet age ≥ (h)" testid="creator-audit-age-input" hint="First on-chain activity of the creator wallet must be at least this many hours before the deploy."
                   value={local.creator_audit_min_wallet_age_h ?? 24} onChange={(v) => setLocal({ ...local, creator_audit_min_wallet_age_h: parseFloat(v) || 0 })} step="1" />
            <Field label="Prior DEX txs ≥" testid="creator-audit-dex-input" hint="Transactions with Pump.fun / PumpSwap / Raydium / Jupiter / Orca / Meteora before the deploy. On Robinhood Chain: transactions sent before the deploy (nonce)."
                   value={local.creator_audit_min_prior_dex ?? 1} onChange={(v) => setLocal({ ...local, creator_audit_min_prior_dex: parseInt(v, 10) || 0 })} step="1" />
            <Field label="Max deploys / hour" testid="creator-audit-perhour-input" hint="Launches by this creator in the hour around the deploy, including this one. 1 = this must be the only launch (also covers batch deploys)."
                   value={local.creator_audit_max_deploys_per_hour ?? 1} onChange={(v) => setLocal({ ...local, creator_audit_max_deploys_per_hour: parseInt(v, 10) || 1 })} step="1" />
            <Field label="Rug tags ≥ fail" testid="creator-audit-rugs-input" hint="Failed / rugged launches on the creator's greylist record that count as a rug tag. 1 = any prior failed launch blocks."
                   value={local.creator_audit_max_rug_tags ?? 1} onChange={(v) => setLocal({ ...local, creator_audit_max_rug_tags: parseInt(v, 10) || 1 })} step="1" />
            <label className="flex items-center gap-2 text-neutral-300 sm:col-span-2">
              <input type="checkbox" data-testid="creator-audit-post-activity" checked={!!local.creator_audit_require_post_activity}
                     onChange={(e) => setLocal({ ...local, creator_audit_require_post_activity: e.target.checked })} /> require post-deploy activity (non-sell tx after the launch)
            </label>
          </div>
        </div>
        <div className="border border-amber-900/50 p-2 space-y-2" data-testid="hot-focus-section">
          <div className="text-[10px] uppercase tracking-[0.15em] text-amber-400/90 inline-flex items-center gap-1">
            hot focus · play the runners out
            <HelpHint label="help: hot focus">While any HOT token is in play (a hot re-entry watch, or a position that is riding / up ≥ the hot threshold) fresh token discovery slows down so the bot plays the runner out instead of chasing new launches. SLOW = one fresh entry per cooldown and reserved position slots kept free for hot re-entries. PAUSE = no fresh entries at all while hot. Re-entries and manual entries are never blocked.</HelpHint>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 text-xs">
            <label className="flex flex-col gap-1">
              <span className="text-[10px] uppercase tracking-[0.15em] text-neutral-500">Focus mode</span>
              <select data-testid="hot-focus-mode-select" value={local.hot_focus_mode ?? "slow"}
                      onChange={(e) => setLocal({ ...local, hot_focus_mode: e.target.value })}
                      className="bg-neutral-950 border border-neutral-800 px-2 py-1 font-mono text-sm focus:border-blue-500 focus:outline-none">
                <option value="slow">slow discovery</option>
                <option value="pause">pause discovery</option>
                <option value="off">off</option>
              </select>
            </label>
            <Field label="Fresh cooldown (s)" testid="hot-focus-cooldown-input"
                   hint="SLOW mode: at most one fresh (non-hot) entry per this many seconds while a hot token is in play."
                   value={local.hot_focus_fresh_cooldown_s ?? 90}
                   onChange={(v) => setLocal({ ...local, hot_focus_fresh_cooldown_s: parseInt(v, 10) || 0 })} step="15" />
            <Field label="Reserved slots" testid="hot-focus-reserve-input"
                   hint="SLOW mode: position slots kept free for hot re-entries — fresh entries can't fill them."
                   value={local.hot_focus_reserve_slots ?? 1}
                   onChange={(v) => setLocal({ ...local, hot_focus_reserve_slots: parseInt(v, 10) || 0 })} step="1" />
          </div>
          <div className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 inline-flex items-center gap-1">
            walk away when
            <HelpHint label="help: walk away">Hot tokens have no attempt cap or time window. Instead the watch is dropped when the token stops trending: N consecutive lower swing lows (with a lower high) or N losing legs in a row; sitting near its trough without a meaningful bounce; a flat range and/or no buyers for the stagnation window; or a price this far under the hot peak. The Hot Token Board shows why each token was dropped.</HelpHint>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 text-xs">
            <Field label="Lower lows (n)" testid="hot-lower-lows-input"
                   hint="Consecutive lower swing lows (needs a lower high too) — or losing re-entry legs in a row — before walking away."
                   value={local.hot_walk_lower_lows_n ?? 2}
                   onChange={(v) => setLocal({ ...local, hot_walk_lower_lows_n: parseInt(v, 10) || 0 })} step="1" />
            <Field label="Weak bounce (s)" testid="hot-weak-bounce-s-input"
                   hint="After a real dip, if price sits this long near its trough…"
                   value={local.hot_weak_bounce_s ?? 120}
                   onChange={(v) => setLocal({ ...local, hot_weak_bounce_s: parseInt(v, 10) || 0 })} step="15" />
            <Field label="Weak bounce (%)" testid="hot-weak-bounce-pct-input"
                   hint="…without lifting at least this much off the trough, the bounce is insignificant → walk away."
                   value={local.hot_weak_bounce_pct ?? 3}
                   onChange={(v) => setLocal({ ...local, hot_weak_bounce_pct: parseFloat(v) || 0 })} step="0.5" />
            <Field label="Stagnant (s)" testid="hot-stagnant-s-input"
                   hint="Window for the stagnation checks: no buyers at all, or price range below Stagnant % for this long → walk away."
                   value={local.hot_stagnant_s ?? 180}
                   onChange={(v) => setLocal({ ...local, hot_stagnant_s: parseInt(v, 10) || 0 })} step="30" />
            <Field label="Stagnant range (%)" testid="hot-stagnant-range-input"
                   hint="Price high-low range over the stagnation window below this = flat → walk away."
                   value={local.hot_stagnant_range_pct ?? 4}
                   onChange={(v) => setLocal({ ...local, hot_stagnant_range_pct: parseFloat(v) || 0 })} step="0.5" />
            <Field label="Breakdown (%)" testid="hot-breakdown-input"
                   hint="Price this far below the post-exit peak = the run is over → walk away."
                   value={local.hot_breakdown_pct ?? 40}
                   onChange={(v) => setLocal({ ...local, hot_breakdown_pct: parseFloat(v) || 0 })} step="5" />
          </div>
        </div>
      </div>

      <div className="border border-lime-900/60 p-3 space-y-2" data-testid="rh-paper-gates">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-lime-300">
          <Eye className="w-3 h-3" /> RH Paper Gates (PONS)
          <HelpHint label="RH Paper Gates">Entry filters for the Robinhood Chain paper trader. Independent from the Pump.fun bands — PONS launches graduate at 4.2 ETH and carry a 99% snipe tax in the first 3 seconds, so the defaults skip the launch window and the graduation sweep.</HelpHint>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 text-xs">
          <Field label="RH Stake $" testid="rh-max-trade-input" hint="Stake per Robinhood trade (paper and live). Autopilot derives it from the RH bankroll × risk% and lifts it to the fee floor — round-trip gas is ~$0.19 on Robinhood Chain, so a $0.50 stake needs +40% just to break even; $5 needs ~6%."
                 value={local.rh_max_trade_usd ?? 5} onChange={(v) => setLocal({ ...local, rh_max_trade_usd: parseFloat(v) || 0 })} step="1" />
          <Field label="Max gas drag %" testid="rh-fee-drag-input" hint="Autopilot floor: round-trip gas may eat at most this % of a stake. 5% with $0.19 gas → min stake $3.80 (break-even ≈ +7% incl. the 2% curve fee)."
                 value={local.rh_fee_drag_max_pct ?? 5} onChange={(v) => setLocal({ ...local, rh_fee_drag_max_pct: parseFloat(v) || 0 })} step="1" />
          <Field label="Max Positions" testid="rh-max-positions-input" hint="Concurrent RH positions."
                 value={local.rh_max_positions ?? 3} onChange={(v) => setLocal({ ...local, rh_max_positions: parseInt(v, 10) || 0 })} step="1" />
          <Field label="Min Age (s)" testid="rh-min-age-input" hint="Skip the launch window. Snipe tax is 99% at t=0 and zero after 3s."
                 value={local.rh_min_age_s ?? 5} onChange={(v) => setLocal({ ...local, rh_min_age_s: parseInt(v, 10) || 0 })} step="1" />
          <Field label="Max Age (m)" testid="rh-max-age-input" hint="Oldest launch (minutes since TokenLaunched) still eligible."
                 value={local.rh_max_age_min ?? 15} onChange={(v) => setLocal({ ...local, rh_max_age_min: parseFloat(v) || 0 })} step="1" />
          <Field label="Min Growth %" testid="rh-min-growth-input" hint="Price growth from the first curve trade we observed."
                 value={local.rh_min_growth_pct ?? 30} onChange={(v) => setLocal({ ...local, rh_min_growth_pct: parseFloat(v) || 0 })} step="5" />
          <Field label="Max Growth % (chased)" testid="rh-max-growth-input" hint="Skip as 'chased' once the price has already run this far from the first print we saw — early buyers are sitting on the gain and a new entry is their exit liquidity. Default 400% (5×)."
                 value={local.rh_max_growth_pct ?? 400} onChange={(v) => setLocal({ ...local, rh_max_growth_pct: parseFloat(v) || 0 })} step="50" />
          <Field label="New Buyers (1m)" testid="rh-min-new-buyers-input" hint="Distinct wallets buying on the curve in the last 60s."
                 value={local.rh_min_new_buyers_1m ?? 5} onChange={(v) => setLocal({ ...local, rh_min_new_buyers_1m: parseInt(v, 10) || 0 })} step="1" />
          <Field label="Min Holders" testid="rh-min-holders-input" hint="Total unique curve buyers since launch."
                 value={local.rh_min_unique_buyers ?? 8} onChange={(v) => setLocal({ ...local, rh_min_unique_buyers: parseInt(v, 10) || 0 })} step="1" />
          <Field label="Min Inflow $ (5m)" testid="rh-min-inflow-input" hint="Net quote flowing into the curve over the inflow window, converted to USD (ETH- and USDG-quoted launches only)."
                 value={local.rh_min_inflow_usd ?? 300} onChange={(v) => setLocal({ ...local, rh_min_inflow_usd: parseFloat(v) || 0 })} step="50" />
          <Field label="Min Curve %" testid="rh-min-curve-input" hint="Graduation progress floor."
                 value={local.rh_min_curve_pct ?? 5} onChange={(v) => setLocal({ ...local, rh_min_curve_pct: parseFloat(v) || 0 })} step="5" />
          <Field label="Max Curve %" testid="rh-max-curve-input" hint="Graduation progress ceiling — the curve is swept at 100% and the position is force-closed."
                 value={local.rh_max_curve_pct ?? 70} onChange={(v) => setLocal({ ...local, rh_max_curve_pct: parseFloat(v) || 0 })} step="5" />
          <Field label="Min MC $" testid="rh-min-mc-input" hint="USD market cap floor (last curve price × 1B supply)."
                 value={local.rh_min_mc_usd ?? 5000} onChange={(v) => setLocal({ ...local, rh_min_mc_usd: parseFloat(v) || 0 })} step="500" />
          <Field label="Max MC $" testid="rh-max-mc-input" hint="USD market cap ceiling."
                 value={local.rh_max_mc_usd ?? 60000} onChange={(v) => setLocal({ ...local, rh_max_mc_usd: parseFloat(v) || 0 })} step="5000" />
          <Field label="Max Last Trade (s)" testid="rh-max-last-trade-input" hint="Skip curves with no trade in this many seconds."
                 value={local.rh_max_last_trade_age_s ?? 20} onChange={(v) => setLocal({ ...local, rh_max_last_trade_age_s: parseInt(v, 10) || 0 })} step="5" />
          <Field label="Grad Trail (R)" testid="rh-grad-trail-r-input" hint="Graduated while held → the position rides the v4 pool on an R trail: no fixed TP, no clock. Exit when the giveback from the post-sweep peak reaches this many R (1R = the trade's SL% with slip; floored at the book's trailing stop). Stop moves to breakeven+costs once +1R."
                 value={local.rh_grad_trail_r ?? 1} onChange={(v) => setLocal({ ...local, rh_grad_trail_r: parseFloat(v) || 0 })} step="0.25" />
        </div>
        <label className="flex items-center gap-2 text-neutral-300 text-xs" title="Off = graduation keeps the fixed TP / trailing-stop / clock ladder on pool prices instead of the R-trail ride.">
          <input type="checkbox" data-testid="rh-grad-handoff-checkbox" checked={local.rh_grad_handoff_r_trail !== false}
                 onChange={(e) => setLocal({ ...local, rh_grad_handoff_r_trail: e.target.checked })} /> hand graduated holds to the R-trail ride (no fixed TP)
        </label>
      </div>

      <button
        onClick={save}
        disabled={!dirty}
        data-testid="save-config-btn"
        className="w-full px-3 py-2 border border-blue-700 text-blue-300 bg-blue-950 hover:bg-blue-900 font-mono text-xs uppercase tracking-[0.2em] transition-colors duration-100 disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
      >
        <Zap className="w-3 h-3" />
        {dirty ? "Save Config" : "Saved"}
      </button>

      {/* Save as Default + Restore My Defaults */}
      <div className="grid grid-cols-2 gap-2">
        <button
          onClick={async () => {
            if (dirty) {
              if (!window.confirm("You have unsaved edits. Save them first, then snapshot as your new defaults?")) return;
              try { const patch = diff(local, baseline); if (Object.keys(patch).length) await onUpdate(patch); setBaseline(local); }
              catch { toast.error("Save failed"); return; }
            }
            try {
              const res = await import("@/lib/api").then(m => m.api.saveConfigAsDefault());
              setSavedDefaults({ exists: true, saved_at: new Date().toISOString() });
              toast.success("Current settings saved as your defaults");
              // Keep `res` referenced to satisfy lint
              void res;
            } catch {
              toast.error("Save-as-default failed");
            }
          }}
          data-testid="save-as-default-btn"
          className="px-3 py-1.5 border border-emerald-800 text-emerald-300 bg-emerald-950/40 hover:bg-emerald-900/50 font-mono text-[10px] uppercase tracking-[0.2em] transition-colors duration-100"
        >
          Save as My Default
        </button>
        <button
          onClick={async () => {
            const which = savedDefaults.exists ? "your saved defaults" : "the SHIPPED defaults";
            if (!window.confirm(`Restore settings to ${which}? (kill switch + live trading flag preserved)`)) return;
            try {
              const fresh = savedDefaults.exists
                ? await import("@/lib/api").then(m => m.api.restoreUserDefaults())
                : await import("@/lib/api").then(m => m.api.resetConfig());
              setLocal(fresh);
              setBaseline(fresh);
              toast.success(`Restored to ${which}`);
            } catch {
              toast.error("Restore failed");
            }
          }}
          data-testid="restore-defaults-btn"
          className="px-3 py-1.5 border border-neutral-700 text-neutral-400 hover:bg-neutral-900 hover:text-neutral-200 font-mono text-[10px] uppercase tracking-[0.2em] transition-colors duration-100"
        >
          {savedDefaults.exists ? "Restore My Defaults" : "Reset to Shipped Defaults"}
        </button>
      </div>
      {savedDefaults.exists && savedDefaults.saved_at && (
        <div className="text-[10px] text-neutral-600 font-mono tracking-wide text-center -mt-1">
          your defaults saved {new Date(savedDefaults.saved_at).toLocaleString()}
        </div>
      )}

      <ConfigSyncPanel onApplied={(cfg) => setLocal(cfg)} />
      <BrainSyncPanel onApplied={(cfg) => { setLocal(cfg); setBaseline(cfg); }} />
    </div>
  );
}

function Field({ label, value, onChange, step, testid, hint }) {
  const labelRef = useRef(null);
  return (
    <label className="flex flex-col gap-1">
      <span ref={labelRef} className="text-[10px] uppercase tracking-[0.15em] text-neutral-500 inline-flex items-center gap-1 select-none" title={hint ? "long-press for help on phones" : undefined}>
        {label}
        {hint && <HelpHint label={`help: ${label}`} wiki={wikiSectionFor(testid, label)} longPressRef={labelRef}>{hint}</HelpHint>}
      </span>
      <input
        data-testid={testid}
        type="number"
        step={step}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-neutral-950 border border-neutral-800 px-2 py-1 font-mono text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
      />
    </label>
  );
}

function GateRow({ label, hint, newTestid, newValue, onNewChange, seasonedTestid, seasonedValue, onSeasonedChange, step, last, newOnly, seasonedOnly }) {
  const cell = "px-2 py-1 border-l border-neutral-800";
  const dim = "px-2 py-1 border-l border-neutral-800 text-[10px] font-mono text-neutral-700 italic text-center self-center";
  return (
    <div className={`grid grid-cols-[1.4fr_1fr_1fr] ${last ? "" : "border-b border-neutral-800"}`}>
      <div className="px-2 py-1 text-[10px] uppercase tracking-[0.1em] text-neutral-400 font-mono self-center inline-flex items-center gap-1">
        {label}
        {hint && <HelpHint label={`help: ${label}`}>{hint}</HelpHint>}
      </div>
      {seasonedOnly ? (
        <div className={dim}>n/a</div>
      ) : (
        <div className={cell}>
          <input
            data-testid={newTestid}
            type="number"
            step={step}
            value={newValue}
            onChange={(e) => onNewChange(e.target.value)}
            className="w-full bg-neutral-950 border border-amber-900/50 px-2 py-0.5 font-mono text-xs text-amber-200 focus:border-amber-500 focus:outline-none"
          />
        </div>
      )}
      {newOnly ? (
        <div className={dim}>n/a</div>
      ) : (
        <div className={cell}>
          <input
            data-testid={seasonedTestid}
            type="number"
            step={step}
            value={seasonedValue}
            onChange={(e) => onSeasonedChange(e.target.value)}
            className="w-full bg-neutral-950 border border-cyan-900/50 px-2 py-0.5 font-mono text-xs text-cyan-200 focus:border-cyan-500 focus:outline-none"
          />
        </div>
      )}
    </div>
  );
}

export default memo(BotControlCard);
