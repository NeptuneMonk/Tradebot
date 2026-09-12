import { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { toast } from "sonner";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useWebSocket } from "@/lib/useWebSocket";
import StatusBanner from "@/components/StatusBanner";
import HaltBanner from "@/components/HaltBanner";
import ReadinessBanner from "@/components/ReadinessBanner";
import ScorecardPanel from "@/components/ScorecardPanel";
import WalletCard from "@/components/WalletCard";
import BotControlCard from "@/components/BotControlCard";
import PLSummaryCard from "@/components/PLSummaryCard";
import DailyLossMeter from "@/components/DailyLossMeter";
import ActiveTradesTable from "@/components/ActiveTradesTable";
import RecentLaunchesFeed from "@/components/RecentLaunchesFeed";
import TradeHistoryTable from "@/components/TradeHistoryTable";
import ClassifierRulesEditor from "@/components/ClassifierRulesEditor";
import ReentryWatchCard from "@/components/ReentryWatchCard";
import ScannerCandidatesCard from "@/components/ScannerCandidatesCard";
import StrategyDoctorPanel from "@/components/StrategyDoctorPanel";
import CreatorGreylistPanel from "@/components/CreatorGreylistPanel";
import PLBySourceCard from "@/components/PLBySourceCard";
import CostTrackerCard from "@/components/CostTrackerCard";
import CollapsibleSection from "@/components/CollapsibleSection";
import AutopilotCard, { AutopilotSwitch } from "@/components/AutopilotCard";
import RhWalletCard from "@/components/RhWalletCard";
import MinimizableCard, { setAllMinimized } from "@/components/MinimizableCard";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Activity, LogOut } from "lucide-react";

const fmtUsd = (v) => (v == null ? "—" : `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(2)}`);

export default function Dashboard() {
  const navigate = useNavigate();
  const [me, setMe] = useState(null);
  const [wallet, setWallet] = useState(null);
  const [status, setStatus] = useState(null);
  const [config, setConfig] = useState(null);
  const [rules, setRules] = useState(null);
  const [launches, setLaunches] = useState([]);
  const [activeTrades, setActiveTrades] = useState([]);
  const [history, setHistory] = useState([]);
  const [pl, setPl] = useState({ series: [], daily_pnl_usd: 0, cumulative_usd: 0 });
  const [reentry, setReentry] = useState([]);
  const [scanner, setScanner] = useState([]);
  const [plSourceRefresh, setPlSourceRefresh] = useState(0);

  // Initial full pull + slow polling fallback (every 20s)
  const refreshAll = useCallback(async () => {
    try {
      const [w, s, c, r, l, a, h, p, re, sc] = await Promise.all([
        api.wallet().catch(() => null),
        api.status().catch(() => null),
        api.config().catch(() => null),
        api.rules().catch(() => null),
        api.launches(30).catch(() => []),
        api.activeTrades().catch(() => []),
        api.tradeHistory(50).catch(() => []),
        api.plSummary(7).catch(() => ({ series: [], daily_pnl_usd: 0, cumulative_usd: 0 })),
        api.reentryWatchlist().catch(() => []),
        api.scannerCandidates().catch(() => []),
      ]);
      if (w) setWallet(w);
      if (s) setStatus(s);
      if (c) setConfig(c);
      if (r) setRules(r);
      setLaunches(l || []);
      setActiveTrades(a || []);
      setHistory(h || []);
      setPl(p);
      setReentry(re || []);
      setScanner(sc || []);
    } catch (e) { /* swallow */ }
  }, []);

  // ----- Polling fallback (only when WebSocket is NOT connected) ---------
  // The WebSocket hub broadcasts every state change in real time. Polling
  // every 20s on top of that doubles the work for no benefit — we drop it
  // to a slower 60s safety net that only fires while disconnected.
  useEffect(() => {
    refreshAll();  // initial pull
  }, [refreshAll]);

  // ----- Coalesced launches updates --------------------------------------
  // High-volume markets emit 5-20 launch_update events per second. Calling
  // setLaunches on every one trashes the React render loop and tanks the
  // UI on lower-power devices. We coalesce all updates within a 400ms
  // window into a single state mutation by buffering pending patches in a
  // ref + scheduling one render.
  const launchUpdateBufRef = useRef(new Map());     // mint -> latest patch
  const launchUpdateNewBufRef = useRef([]);          // brand-new launches (FIFO)
  const launchFlushHandleRef = useRef(null);
  const scheduleLaunchFlush = useCallback(() => {
    if (launchFlushHandleRef.current) return;
    launchFlushHandleRef.current = setTimeout(() => {
      launchFlushHandleRef.current = null;
      const updates = launchUpdateBufRef.current;
      const newOnes = launchUpdateNewBufRef.current;
      launchUpdateBufRef.current = new Map();
      launchUpdateNewBufRef.current = [];
      if (updates.size === 0 && newOnes.length === 0) return;
      setLaunches((prev) => {
        // 1. Merge updates against current state; a candidate that degraded to a skip arrives with `dropped` → remove it
        let next = prev.map((l) => (updates.has(l.id) ? { ...l, ...updates.get(l.id) } : l)).filter((l) => !l.dropped);
        // Drop any updates that hit mints we never displayed — keeps state tight
        // 2. Prepend brand-new launches, dropping dupes (BOTH against `next`
        //    AND within `newOnes` itself — the backend can re-broadcast a
        //    `launch` event for the same id when a token gets evicted from
        //    in-memory tracking and re-seeded by discovery a few minutes
        //    later. Without internal dedup, React throws duplicate-key
        //    warnings and the rendered DOM silently desyncs from state —
        //    top of the feed appears to "freeze" at the first dupe).
        //    We also dedup by mint as a belt-and-braces defense (different
        //    code paths can mint different ids for the same on-chain mint:
        //    organic `on_launch` UUID vs discovery's `disc-{mint8}` synth id).
        if (newOnes.length) {
          const dedupedNew = [];
          const seenIds = new Set();
          const seenMints = new Set();
          for (const d of newOnes) {
            if (!d?.id) continue;
            if (seenIds.has(d.id)) continue;
            if (d.mint && seenMints.has(d.mint)) continue;
            seenIds.add(d.id);
            if (d.mint) seenMints.add(d.mint);
            dedupedNew.push(d);
          }
          if (dedupedNew.length) {
            next = [
              ...dedupedNew,
              ...next.filter(
                (l) =>
                  !seenIds.has(l.id) && !(l.mint && seenMints.has(l.mint))
              ),
            ];
          }
        }
        // Per-chain caps: the Robinhood Chain feed (~12 launches/min) must
        // never evict Solana launches from the window, and vice versa.
        const keep = new Set();
        let nSol = 0, nRh = 0;
        for (const l of next) {
          if (l.chain === "rh") { if (nRh < 30) { keep.add(l.id); nRh++; } }
          else if (nSol < 30) { keep.add(l.id); nSol++; }
        }
        return next.filter((l) => keep.has(l.id));
      });
    }, 400);
  }, []);
  // Cleanup pending flush on unmount
  useEffect(() => {
    return () => {
      if (launchFlushHandleRef.current) {
        clearTimeout(launchFlushHandleRef.current);
        launchFlushHandleRef.current = null;
      }
    };
  }, []);

  // ----- Coalesced exit toasts -------------------------------------------
  // Bursts of trade_exit events (e.g. when the user hits "Stop bot" with
  // 8 active positions) used to fire 8 stacked toasts. We aggregate exits
  // within a 1.5s window into a single summary toast.
  const exitToastCountRef = useRef(0);
  const exitToastHandleRef = useRef(null);
  const scheduleExitToast = useCallback(() => {
    exitToastCountRef.current += 1;
    if (exitToastHandleRef.current) return;
    exitToastHandleRef.current = setTimeout(() => {
      const n = exitToastCountRef.current;
      exitToastHandleRef.current = null;
      exitToastCountRef.current = 0;
      if (n === 1) return;  // single exits don't deserve a toast
      toast.info(`${n} trades exited`);
    }, 1500);
  }, []);
  useEffect(() => () => {
    if (exitToastHandleRef.current) {
      clearTimeout(exitToastHandleRef.current);
      exitToastHandleRef.current = null;
    }
  }, []);

  // ----- Coalesced refetches on trade_exit -------------------------------
  // Each trade_exit triggers a re-fetch of launches+pnl+pl_source+reentry.
  // A burst of N exits = 4×N HTTP calls. Debounce to one set every 1s.
  const tradeExitRefetchHandleRef = useRef(null);
  const scheduleTradeExitRefetch = useCallback(() => {
    if (tradeExitRefetchHandleRef.current) return;
    tradeExitRefetchHandleRef.current = setTimeout(() => {
      tradeExitRefetchHandleRef.current = null;
      api.launches().then(setLaunches).catch(() => {});
      api.plSummary(7).then(setPl).catch(() => {});
      setPlSourceRefresh((n) => n + 1);
      api.reentryWatchlist().then(setReentry).catch(() => {});
    }, 1000);
  }, []);
  useEffect(() => () => {
    if (tradeExitRefetchHandleRef.current) {
      clearTimeout(tradeExitRefetchHandleRef.current);
      tradeExitRefetchHandleRef.current = null;
    }
  }, []);

  // Pull current user once for header display
  useEffect(() => {
    api.authMe().then(setMe).catch(() => {});
  }, []);

  const handleLogout = useCallback(async () => {
    try { await api.authLogout(); } catch { /* ignore */ }
    navigate("/login", { replace: true });
  }, [navigate]);

  // Real-time WebSocket event handler
  const { connected: wsConnected } = useWebSocket(useCallback((evt) => {
    const { type, data } = evt || {};
    if (!type) return;
    switch (type) {
      case "status":
        setStatus(data);
        break;
      case "wallet":
        setWallet(data);
        break;
      case "candidate":
        // The hub only forwards CANDIDATES (tens/min) — raw launches never reach the wire.
        // Buffered for the 400 ms coalesced flush (see scheduleLaunchFlush).
        launchUpdateNewBufRef.current.push(data);
        scheduleLaunchFlush();
        break;
      case "candidate_update":
        launchUpdateBufRef.current.set(data.id, data);
        scheduleLaunchFlush();
        break;
      case "trade_enter":
        setActiveTrades((prev) => [data, ...prev.filter((t) => t.id !== data.id)]);
        api.launches().then(setLaunches).catch(() => {});   // refresh ENT badges / live P/L stamps
        break;
      case "trade_update":
        setActiveTrades((prev) => prev.map((t) => (t.id === data.id ? { ...t, ...data } : t)));
        break;
      case "trade_exit":
        setActiveTrades((prev) => prev.filter((t) => t.id !== data.id));
        setHistory((prev) => [data, ...prev.filter((t) => t.id !== data.id)]);
        // Coalesce refetches + toast spam during exit bursts (perf).
        scheduleTradeExitRefetch();
        scheduleExitToast();
        break;
      case "reentry_watch_add":
        setReentry((prev) => [...prev.filter((w) => w.mint !== data.mint), data]);
        break;
      case "reentry_watch_remove":
        setReentry((prev) => prev.filter((w) => w.mint !== data.mint));
        break;
      case "reentry_attempted":
        api.reentryWatchlist().then(setReentry).catch(() => {});
        break;
      case "doctor_auto_applied":
        toast.info(`Doctor auto-applied: ${data?.title ?? "config change"}`, { duration: 8000 });
        break;
      case "doctor_auto_reverted":
        toast.warning(
          `Doctor auto-reverted "${data?.title ?? "change"}" — win rate ${data?.wr_since ?? "?"}% vs ${data?.baseline_wr != null ? Math.round(data.baseline_wr) : "?"}% baseline`,
          { duration: 12000 }
        );
        break;
      case "bot_resumed_after_restart":
        toast.success(`Backend restarted — bot resumed automatically (${data?.active_positions ?? 0} positions re-attached).`, { duration: 8000 });
        break;
      case "bot_auto_disabled_on_restart":
        toast.warning(
          `Bot was auto-disabled after backend restart (${data?.active_positions ?? 0} positions retained). Press Start to resume.`,
          { duration: 12000 }
        );
        break;
      default:
        break;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []));

  // WS-aware safety-net polling. While the WebSocket is healthy we don't
  // need to poll — every state change is pushed in real time. While
  // disconnected, fall back to a 30s pull so the UI keeps drifting forward.
  useEffect(() => {
    if (wsConnected) return;
    const id = setInterval(refreshAll, 30000);
    return () => clearInterval(id);
  }, [wsConnected, refreshAll]);

  // Bot Control only cares about 4 status fields — hand it a slice so the 3 s status tick doesn't re-render
  // the 1100-line card (and its inputs) every time. Callbacks are stable for the same reason.
  const controlStatus = useMemo(() => status && ({
    enabled: status.enabled, stopping_gracefully: status.stopping_gracefully,
    kill_switch_tripped: status.kill_switch_tripped, active_trade_count: status.active_trade_count,
    listener_connected: status.listener_connected, rh_feed_alive: status.rh_feed_alive,
    helius_paused: status.helius_paused, listener_last_error: status.listener_last_error, rh_feed_paused_reason: status.rh_feed_paused_reason,
    listener_last_ok_ts: status.listener_last_ok_ts, listener_last_attempt_ts: status.listener_last_attempt_ts,
  }), [status?.enabled, status?.stopping_gracefully, status?.kill_switch_tripped, status?.active_trade_count, status?.listener_connected, status?.rh_feed_alive,
       status?.helius_paused?.paused, status?.helius_paused?.auto, status?.listener_last_error, status?.listener_last_ok_ts, status?.listener_last_attempt_ts, status?.rh_feed_paused_reason]);
  const onConfigPatch = useCallback(async (patch) => {
    const saved = await api.updateConfig(patch);
    setConfig(saved);
    api.status().then((st) => st && setStatus(st)).catch(() => {});
  }, []);
  const onStart = useCallback(async () => { await api.start(); refreshAll(); }, [refreshAll]);
  const onExitTrade = useCallback(async (id) => {
    try {
      await api.exitTrade(id);
      toast.success("Manual exit submitted");
    } catch (e) {
      const detail = e?.response?.data?.detail || e?.message || "exit failed";
      if (e?.response?.status === 400 && /not active/i.test(detail)) toast.info("Trade already closed");
      else toast.error(`Exit failed: ${detail}`);
    } finally {
      refreshAll();
    }
  }, [refreshAll]);
  const onRulesSave = useCallback(async (r) => { setRules(await api.updateRules(r)); }, []);
  const onDoctorApplied = useCallback(() => api.config().then(setConfig).catch(() => {}), []);
  const onReentryRefresh = useCallback(() => api.reentryWatchlist().then(setReentry).catch(() => {}), []);
  const onStop = useCallback(async () => { await api.stop(); refreshAll(); }, [refreshAll]);

  return (
    <TooltipProvider delayDuration={150} skipDelayDuration={50}>
    <div className="min-h-screen bg-neutral-950 text-neutral-50" data-testid="dashboard">
      <header className="border-b border-neutral-800 px-6 py-3 flex items-center justify-between bg-neutral-950 sticky top-0 z-20">
        <div className="flex items-center gap-3">
          <Activity className="w-5 h-5 text-blue-500" />
          <div>
            <h1 className="text-base font-mono font-bold tracking-tight" data-testid="app-title">PUMP.BOT // micro-stake</h1>
            <p className="text-[10px] uppercase tracking-[0.2em] text-neutral-500">preview-only · solana mainnet</p>
          </div>
        </div>
        <div className="flex items-center gap-4 text-xs font-mono">
          {config && (
            <AutopilotSwitch
              enabled={!!config.autopilot_enabled}
              onChange={async () => { try { setConfig(await api.config()); } catch { /* noop */ } refreshAll(); }}
            />
          )}
          <span className="flex items-center gap-2" data-testid="ws-status">
            <span className={`w-2 h-2 rounded-full ${wsConnected ? "bg-blue-500 animate-pulse" : "bg-neutral-600"}`}></span>
            <span className="text-neutral-400">{wsConnected ? "WS LIVE" : "WS OFFLINE"}</span>
          </span>
          <span className="flex items-center gap-2">
            {(() => {
              const desired = status?.helius_tracker_enabled ?? config?.helius_tracker_enabled ?? true;
              const live = !!status?.listener_connected;
              const paused = status?.helius_paused?.auto;
              const connecting = status?.listener_last_attempt_ts && Date.now() / 1000 - status.listener_last_attempt_ts < 15;
              const dot = !desired || (!live && paused) ? "bg-amber-500" : live ? "bg-emerald-500" : "bg-red-500";
              const text = !desired ? "PUMP.FUN FEED OFF" : live ? "PUMP.FUN FEED ON · LIVE" : paused ? "PUMP.FUN FEED ON · PAUSED · DOCTOR" : connecting ? "PUMP.FUN FEED ON · CONNECTING" : "PUMP.FUN FEED ON · OFFLINE";
              const why = !desired ? "operator switch OFF" : live ? "" : (status?.listener_last_error || (connecting ? "connecting…" : ""));
              return (<><span className={`w-2 h-2 rounded-full ${dot}`}></span><span className="text-neutral-400" data-testid="listener-status" title={why}>{text}</span></>);
            })()}
          </span>
          {me && (
            <span className="hidden md:flex items-center gap-2 text-neutral-500" data-testid="auth-user">
              {me.picture ? (
                <img src={me.picture} alt="" className="w-5 h-5 rounded-full" />
              ) : null}
              <span className="text-neutral-400">{me.email}</span>
            </span>
          )}
          {status && (
            <button
              type="button"
              data-testid="header-bot-toggle"
              onClick={async () => {
                try {
                  if (status.enabled) { await api.stop(); }
                  else { await api.start(); }
                  refreshAll();
                } catch (e) {
                  toast.error(`Toggle failed: ${e?.response?.data?.detail || e.message}`);
                }
              }}
              className={`px-2.5 py-1 border text-[11px] font-mono uppercase tracking-wider transition-colors duration-100 ${
                status.enabled
                  ? "border-emerald-700/60 text-emerald-300 hover:bg-emerald-950/40"
                  : "border-rose-800/60 text-rose-300 hover:bg-rose-950/40"
              }`}
              title={status.enabled ? "Bot is RUNNING — click to stop" : "Bot is STOPPED — click to start"}
            >
              <span className={`inline-block w-1.5 h-1.5 rounded-full mr-1.5 ${status.enabled ? "bg-emerald-500 animate-pulse" : "bg-rose-500"}`} />
              {status.enabled ? "RUNNING" : "STOPPED"}
            </button>
          )}
          <span className="hidden lg:inline-flex items-center gap-1 text-[10px] uppercase tracking-wider text-neutral-500" data-testid="minimize-all-group">
            <button type="button" onClick={() => setAllMinimized(true)} data-testid="minimize-all-btn" className="hover:text-neutral-200 transition-colors duration-100" title="Minimize every window">minimize all</button>
            <span className="text-neutral-700">/</span>
            <button type="button" onClick={() => setAllMinimized(false)} data-testid="expand-all-btn" className="hover:text-neutral-200 transition-colors duration-100" title="Expand every window">expand all</button>
          </span>
          <button
            type="button"
            onClick={handleLogout}
            data-testid="logout-btn"
            className="flex items-center gap-1.5 text-neutral-400 hover:text-red-400 transition uppercase tracking-wider"
          >
            <LogOut className="w-3.5 h-3.5" />
            <span>logout</span>
          </button>
        </div>
      </header>

      <StatusBanner status={status} onResetKill={async () => { await api.resetKillSwitch(); refreshAll(); }} />
      <HaltBanner />
      <ReadinessBanner onChanged={refreshAll} />

      <main className="max-w-[1600px] mx-auto p-4 md:p-6 space-y-4 md:space-y-6">
        {/* TOP KPI STRIP — always visible. Wallet + PnL + DailyLoss.
            Bot Control moved to a collapsible below; the StatusBanner at
            the top of the page already shows running/stopped state. */}
        {config?.autopilot_enabled && (
          <div className="border border-lime-800/60 bg-lime-950/20 px-4 py-2 text-[11px] font-mono text-lime-200 flex items-center gap-2" data-testid="autopilot-banner">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-lime-400 animate-pulse" />
            AUTOPILOT — the Doctor is driving: sizing from bankroll, tuning on expectancy in R, one canary at a time. You keep Start/Stop and live/paper.
          </div>
        )}
        <MinimizableCard id="autopilot" title="Autopilot" stat={config?.autopilot_enabled ? "doctor is driving" : "manual"}>
          <AutopilotCard config={config} onConfigUpdate={setConfig} />
        </MinimizableCard>
        <MinimizableCard id="rh-wallet" title="Robinhood wallet" stat={config?.rh_live_trading ? "LIVE" : config?.rh_paper_enabled ? "paper" : "off"}>
          <RhWalletCard config={config} />
        </MinimizableCard>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 md:gap-6 items-start">
          <MinimizableCard id="wallet" title="Wallet" stat={wallet ? `${wallet.sol_balance.toFixed(4)} SOL` : "—"}>
            <WalletCard wallet={wallet} />
          </MinimizableCard>
          <MinimizableCard id="pl" title="P/L today" stat={fmtUsd(pl?.daily_pnl_usd)}>
            <PLSummaryCard pl={pl} status={status} onReset={refreshAll} />
          </MinimizableCard>
          <MinimizableCard id="daily-loss" title="Daily loss" stat={`$${Number(status?.daily_loss_usd ?? 0).toFixed(2)} / $${Number(status?.daily_kill_switch_usd ?? 0).toFixed(0)}`}>
            <DailyLossMeter status={status} onReset={refreshAll} />
          </MinimizableCard>
        </div>

        {/* PRIMARY — Active Trades + Recent Launches always visible. */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 md:gap-6 items-start">
          <MinimizableCard id="active-trades" title="Active trades" stat={`${activeTrades.length} open`}>
          <ActiveTradesTable trades={activeTrades} onExit={onExitTrade} />
          </MinimizableCard>
          <MinimizableCard id="launch-feed" title="Live launch feed" stat={`${launches.length} tracked`}>
          <RecentLaunchesFeed launches={launches} feedLive={{ sol: !!status?.listener_connected, rh: !!status?.rh_feed_alive }} />
          </MinimizableCard>
        </div>

        {/* Trade History — always visible (collapsed cards above feed flow).
            Co-mounted with Classifier Rules so the second column on wide
            screens stays useful. */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 md:gap-6 items-start">
          <MinimizableCard id="trade-history" title="Trade history" stat={`${history.length} trades`}>
            <TradeHistoryTable history={history} />
          </MinimizableCard>
          <CollapsibleSection
            title="Scorecard"
            description="situation cells — book × pattern × band × hour × cost · disabled cells skip"
            storageKey="ui.section.scorecard"
            testId="section-scorecard"
          >
            <ScorecardPanel />
          </CollapsibleSection>
          <CollapsibleSection
            title="Classifier Rules"
            description="book router — scalp / hunt / skip"
            storageKey="ui.section.classifier"
            testId="section-classifier"
          >
            <ClassifierRulesEditor rules={rules} onSave={onRulesSave} />
          </CollapsibleSection>
        </div>

        {/* COLLAPSIBLE — everything below is lazy-mounted on first expand
            and persists per-user via localStorage. Closed by default
            because the bot runs fine without them being on-screen. */}

        <CollapsibleSection
          title="Bot Control"
          description="Start/Stop · bands · gates · greylist sniper config"
          storageKey="ui.section.bot-control"
          testId="section-bot-control"
          badge={status?.enabled ? "RUNNING" : "STOPPED"}
        >
          <BotControlCard
            status={controlStatus}
            config={config}
            onUpdate={onConfigPatch}
            onConfigLoaded={setConfig}
            onStart={onStart}
            onStop={onStop}
          />
        </CollapsibleSection>

        <CollapsibleSection
          title="Strategy Doctor"
          description="advisory toggle · pending suggestions · live panels"
          storageKey="ui.section.doctor"
          testId="section-doctor"
          badge={config?.autopilot_enabled ? "autopilot" : null}
        >
          <StrategyDoctorPanel
            config={config}
            onConfigUpdate={setConfig}
            onApplied={onDoctorApplied}
          />
        </CollapsibleSection>

        <CollapsibleSection
          title="Creator Greylist"
          description="creator scoring · pattern analytics · sniper targets"
          storageKey="ui.section.greylist"
          testId="section-greylist"
        >
          <CreatorGreylistPanel
            config={config}
            onConfigUpdate={setConfig}
          />
        </CollapsibleSection>

        <CollapsibleSection
          title="Re-entry Watchlist"
          description={`${reentry?.length || 0} winners eligible to re-buy`}
          storageKey="ui.section.reentry"
          testId="section-reentry"
          badge={reentry?.length ? String(reentry.length) : null}
        >
          <ReentryWatchCard watchlist={reentry} onRefresh={onReentryRefresh} />
        </CollapsibleSection>

        <CollapsibleSection
          title="P/L by Source"
          description="momentum_new · momentum_seasoned · greylist_snipe · reentry"
          storageKey="ui.section.pl-by-source"
          testId="section-pl-by-source"
        >
          <PLBySourceCard refreshSignal={plSourceRefresh} />
        </CollapsibleSection>

        <CollapsibleSection
          title="Cost Tracker"
          description="Helius credit burn · monthly cap"
          storageKey="ui.section.cost"
          testId="section-cost"
        >
          <CostTrackerCard apiBase={process.env.REACT_APP_BACKEND_URL || ""} />
        </CollapsibleSection>

        <CollapsibleSection
          title="Scanner Candidates"
          description="live tokens being tracked towards entry"
          storageKey="ui.section.scanner"
          testId="section-scanner"
          badge={scanner?.length ? String(scanner.length) : null}
        >
          <ScannerCandidatesCard candidates={scanner} config={config} />
        </CollapsibleSection>

        <footer className="text-[10px] text-neutral-600 font-mono text-center pt-4 pb-8 tracking-wider uppercase">
          // Preview-only. Real funds at risk. Never deploy this outside Emergent preview.
        </footer>
      </main>
    </div>
    </TooltipProvider>
  );
}
