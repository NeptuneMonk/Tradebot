import { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { toast } from "sonner";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useWebSocket } from "@/lib/useWebSocket";
import StatusBanner from "@/components/StatusBanner";
import HaltBanner from "@/components/HaltBanner";
import ReadinessBanner from "@/components/ReadinessBanner";
import PodPill from "@/components/PodPill";
import ScorecardPanel from "@/components/ScorecardPanel";
import WalletCard from "@/components/WalletCard";
import BotControlCard from "@/components/BotControlCard";
import Wiki from "@/components/wiki/Wiki";
import PLSummaryCard from "@/components/PLSummaryCard";
import DailyLossMeter from "@/components/DailyLossMeter";
import RecentLaunchesFeed from "@/components/RecentLaunchesFeed";
import TradeHistoryTable from "@/components/TradeHistoryTable";
import ClassifierRulesEditor from "@/components/ClassifierRulesEditor";
import ReentryWatchCard from "@/components/ReentryWatchCard";
import GraduateLadderCard from "@/components/GraduateLadderCard";
import ScannerCandidatesCard from "@/components/ScannerCandidatesCard";
import CreatorGreylistPanel from "@/components/CreatorGreylistPanel";
import PLBySourceCard from "@/components/PLBySourceCard";
import CollapsibleSection from "@/components/CollapsibleSection";
import { AutopilotSwitch } from "@/components/AutopilotCard";
import RhWalletCard from "@/components/RhWalletCard";
import MinimizableCard, { setAllMinimized } from "@/components/MinimizableCard";
import DoctorStrip from "@/components/cockpit/DoctorStrip";
import KpiStrip from "@/components/cockpit/KpiStrip";
import ActiveTradesCockpit from "@/components/cockpit/ActiveTradesCockpit";
import EquityPanel from "@/components/cockpit/EquityPanel";
import CompactCandidates from "@/components/cockpit/CompactCandidates";
import CompactLadder from "@/components/cockpit/CompactLadder";
import SkipTicker from "@/components/cockpit/SkipTicker";
import NavBar from "@/components/cockpit/NavBar";
import DoctorWorkspace from "@/components/cockpit/DoctorWorkspace";
import { useSkipFeed } from "@/lib/useSkipFeed";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Activity, LogOut } from "lucide-react";

// On-screen list caps (P0.5): 30 Solana + 30 RH launches, newest first by detected_at; history never grows past 50 from WS.
const LAUNCH_CAP_PER_CHAIN = 30;
const HISTORY_CAP = 50;
const isActiveRow = (t) => !t.status || t.status === "active";
const tsOf = (l) => (l?.detected_at ? Date.parse(l.detected_at) || 0 : 0);
function capLaunches(list) {
  // Per-chain caps: the Robinhood Chain feed (~12 launches/min) must never evict Solana launches, and vice versa.
  // Evict the OLDEST by detected_at (not by array position) so a late re-broadcast can't push live rows off.
  const sol = [], rh = [];
  for (const l of list) (l.chain === "rh" ? rh : sol).push(l);
  const trim = (arr) => {
    if (arr.length <= LAUNCH_CAP_PER_CHAIN) return new Set(arr.map((l) => l.id));
    return new Set([...arr].sort((a, b) => tsOf(b) - tsOf(a)).slice(0, LAUNCH_CAP_PER_CHAIN).map((l) => l.id));
  };
  const keep = new Set([...trim(sol), ...trim(rh)]);
  return list.filter((l) => keep.has(l.id));
}

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
  const [ladder, setLadder] = useState(null);
  const [plSourceRefresh, setPlSourceRefresh] = useState(0);
  const [view, setView] = useState(() => (window.location.hash.startsWith("#wiki") ? "wiki" : localStorage.getItem("ui.view") || "live"));
  const [auto, setAuto] = useState(null);           // /api/autopilot/status — bankroll, search ledger, canary
  const [pendingDoc, setPendingDoc] = useState(0);
  useEffect(() => {
    const tick = () => {
      api.autopilotStatus().then(setAuto).catch(() => {});
      api.doctorSuggestions().then((d) => setPendingDoc((d?.items || []).filter((x) => x.status === "pending").length)).catch(() => {});
    };
    tick();
    const id = setInterval(tick, 30000);
    return () => clearInterval(id);
  }, [plSourceRefresh]);
  const pickView = useCallback((v) => { setView(v); localStorage.setItem("ui.view", v); }, []);
  useEffect(() => {
    const onWiki = () => setView("wiki");
    const onHash = () => { if (window.location.hash.startsWith("#wiki")) setView("wiki"); };
    window.addEventListener("open-wiki", onWiki);
    window.addEventListener("hashchange", onHash);
    return () => { window.removeEventListener("open-wiki", onWiki); window.removeEventListener("hashchange", onHash); };
  }, []);

  // Footer skip ticker — resolves launch ids / mints to symbol + chain from whatever the UI already holds.
  const skipFeed = useSkipFeed();
  const skipFeedRef = useRef(skipFeed.onEvent);
  skipFeedRef.current = skipFeed.onEvent;
  const lookupRef = useRef({ launches: [], scanner: [] });
  useEffect(() => { lookupRef.current = { launches, scanner }; skipFeed.seed(launches); }, [launches, scanner, skipFeed.seed]);
  const resolveToken = useCallback((idOrMint) => {
    if (!idOrMint) return null;
    const { launches: ls, scanner: sc } = lookupRef.current;
    const l = ls.find((x) => x.id === idOrMint || x.mint === idOrMint);
    if (l) return { mint: l.mint, symbol: l.symbol, chain: l.chain || "sol" };
    const c = sc.find((x) => x.mint === idOrMint);
    return c ? { mint: c.mint, symbol: c.symbol, chain: c.chain || "sol" } : null;
  }, []);

  // Initial full pull + slow polling fallback (every 20s)
  const refreshAll = useCallback(async () => {
    try {
      const [w, s, c, r, l, a, h, p, re, sc, ld] = await Promise.all([
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
        api.ladder().catch(() => null),
      ]);
      if (w) setWallet(w);
      if (s) setStatus(s);
      if (c) setConfig(c);
      if (r) setRules(r);
      setLaunches(capLaunches(l || []));
      setActiveTrades((a || []).filter(isActiveRow));
      setHistory((h || []).slice(0, HISTORY_CAP));
      setPl(p);
      setReentry(re || []);
      setScanner(sc || []);
      if (ld) setLadder(ld);
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
  // High-volume markets emit 5-20 candidate_update events per second. Calling
  // setLaunches on every one trashes the React render loop. All patches that
  // arrive within one animation frame are buffered in refs and applied in ONE
  // state write on the next frame (~16ms), so the tape ticks immediately.
  // Wire shape: `candidate` = slim row (+seq); `candidate_update` = { id, seq, p: {changed} } —
  // a patch is only applied when its seq is newer than what the row already carries.
  const launchUpdateBufRef = useRef(new Map());     // id -> { seq, p } (later patches merged in order)
  const launchUpdateNewBufRef = useRef([]);          // brand-new launches (FIFO)
  const launchFlushHandleRef = useRef(null);
  const scheduleLaunchFlush = useCallback(() => {
    if (launchFlushHandleRef.current) return;
    launchFlushHandleRef.current = requestAnimationFrame(() => {
      launchFlushHandleRef.current = null;
      const updates = launchUpdateBufRef.current;
      const newOnes = launchUpdateNewBufRef.current;
      launchUpdateBufRef.current = new Map();
      launchUpdateNewBufRef.current = [];
      if (updates.size === 0 && newOnes.length === 0) return;
      setLaunches((prev) => {
        // 1. Apply patches (seq-guarded) against current state; a candidate that degraded to a skip arrives with `dropped` → remove it
        let next = prev.map((l) => {
          const u = updates.get(l.id);
          if (!u || (u.seq != null && l.seq != null && u.seq <= l.seq)) return l;
          return { ...l, ...u.p, seq: u.seq ?? l.seq };
        }).filter((l) => !l.dropped);
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
        return capLaunches(next);
      });
    });
  }, []);
  // Cleanup pending flush on unmount
  useEffect(() => {
    return () => {
      if (launchFlushHandleRef.current) {
        cancelAnimationFrame(launchFlushHandleRef.current);
        launchFlushHandleRef.current = null;
      }
    };
  }, []);

  // ----- Coalesced trade ticks -------------------------------------------
  // trade_update fires per open position per price tick; buffer per trade id and apply once per frame.
  const tradeUpdateBufRef = useRef(new Map());
  const tradeFlushHandleRef = useRef(null);
  const scheduleTradeFlush = useCallback(() => {
    if (tradeFlushHandleRef.current) return;
    tradeFlushHandleRef.current = requestAnimationFrame(() => {
      tradeFlushHandleRef.current = null;
      const updates = tradeUpdateBufRef.current;
      tradeUpdateBufRef.current = new Map();
      if (updates.size === 0) return;
      setActiveTrades((prev) => prev.map((t) => (updates.has(t.id) ? { ...t, ...updates.get(t.id) } : t)).filter(isActiveRow));
    });
  }, []);
  useEffect(() => () => {
    if (tradeFlushHandleRef.current) {
      cancelAnimationFrame(tradeFlushHandleRef.current);
      tradeFlushHandleRef.current = null;
    }
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
      api.launches().then((l) => setLaunches(capLaunches(l || []))).catch(() => {});
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
        // Buffered for the per-frame coalesced flush (see scheduleLaunchFlush).
        launchUpdateNewBufRef.current.push(data);
        scheduleLaunchFlush();
        skipFeedRef.current(type, data, resolveToken);
        break;
      case "candidate_update": {
        // { id, seq, p } patch — later patches for the same row merge on top (seq = newest)
        const buf = launchUpdateBufRef.current;
        const cur = buf.get(data.id);
        const p = data.p || {};
        buf.set(data.id, cur ? { seq: data.seq ?? cur.seq, p: { ...cur.p, ...p } } : { seq: data.seq, p });
        scheduleLaunchFlush();
        skipFeedRef.current(type, { id: data.id, ...p }, resolveToken);
        break;
      }
      case "scanner_skip":
        skipFeedRef.current(type, data, resolveToken);
        break;
      case "ladder":
        setLadder(data);
        break;
      case "trade_enter":
        setActiveTrades((prev) => [data, ...prev.filter((t) => t.id !== data.id)].filter(isActiveRow));
        api.launches().then((l) => setLaunches(capLaunches(l || []))).catch(() => {});   // refresh ENT badges / live P/L stamps
        break;
      case "trade_update":
      case "trade_partial": {
        const buf = tradeUpdateBufRef.current;
        buf.set(data.id, { ...(buf.get(data.id) || {}), ...data });
        scheduleTradeFlush();
        break;
      }
      case "scanner_snapshot":
        if (Array.isArray(data?.items)) setScanner(data.items);
        break;
      case "trade_exit":
        tradeUpdateBufRef.current.delete(data.id);
        setActiveTrades((prev) => prev.filter((t) => t.id !== data.id));
        setHistory((prev) => [data, ...prev.filter((t) => t.id !== data.id)].slice(0, HISTORY_CAP));
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

  // Scanner candidates are pushed over WS (`scanner_snapshot`, every scanner pass). REST polling is a fallback that
  // only runs while the socket is down — same pattern as the 30s status fallback above.
  useEffect(() => {
    if (wsConnected) return undefined;
    const tick = () => {
      api.scannerCandidates().then((sc) => setScanner(sc || [])).catch(() => {});
      api.ladder().then((ld) => ld && setLadder(ld)).catch(() => {});
    };
    const id = setInterval(tick, 5000);
    return () => clearInterval(id);
  }, [wsConnected]);

  // Bot Control only cares about 4 status fields — hand it a slice so the 3 s status tick doesn't re-render
  // the 1100-line card (and its inputs) every time. Callbacks are stable for the same reason.
  const controlStatus = useMemo(() => status && ({
    enabled: status.enabled, stopping_gracefully: status.stopping_gracefully,
    kill_switch_tripped: status.kill_switch_tripped, active_trade_count: status.active_trade_count,
    listener_connected: status.listener_connected, rh_feed_alive: status.rh_feed_alive,
    helius_paused: status.helius_paused, listener_last_error: status.listener_last_error, rh_feed_paused_reason: status.rh_feed_paused_reason,
    listener_last_ok_ts: status.listener_last_ok_ts, listener_last_attempt_ts: status.listener_last_attempt_ts, listener_via: status.listener_via,
  }), [status?.enabled, status?.stopping_gracefully, status?.kill_switch_tripped, status?.active_trade_count, status?.listener_connected, status?.rh_feed_alive,
       status?.helius_paused?.paused, status?.helius_paused?.auto, status?.listener_last_error, status?.listener_last_ok_ts, status?.listener_last_attempt_ts, status?.rh_feed_paused_reason, status?.listener_via]);
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
  const onLadderRefresh = useCallback(() => api.ladder().then(setLadder).catch(() => {}), []);
  const onStop = useCallback(async () => { await api.stop(); refreshAll(); }, [refreshAll]);
  // Stable props for the memo'd cockpit cards — an inline arrow / object literal here would defeat React.memo
  // and repaint every card on each 3s status tick.
  const onEnableScanner = useCallback(() => onConfigPatch({ scanner_enabled: true }), [onConfigPatch]);
  const onOpenDoctor = useCallback(() => pickView("doctor"), [pickView]);
  const onReloadAuto = useCallback(() => api.autopilotStatus().then(setAuto).catch(() => {}), []);
  const onAutopilotChange = useCallback(async () => { try { setConfig(await api.config()); } catch { /* noop */ } refreshAll(); }, [refreshAll]);
  const onResetKill = useCallback(async () => { await api.resetKillSwitch(); refreshAll(); }, [refreshAll]);
  const ladderHolding = useMemo(() => (ladder?.tokens || []).filter((t) => t.state === "holding").length, [ladder]);
  const navBadges = useMemo(() => ({
    live: activeTrades.length || null, scan: launches.length || null, ladder: ladderHolding || null, doctor: pendingDoc || null,
  }), [activeTrades.length, launches.length, ladderHolding, pendingDoc]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const feedLive = useMemo(() => ({ sol: !!status?.listener_connected, rh: !!status?.rh_feed_alive }), [status?.listener_connected, status?.rh_feed_alive]);
  const scannerEnabled = config?.scanner_enabled ?? true;

  return (
    <TooltipProvider delayDuration={150} skipDelayDuration={50}>
    <div className="min-h-screen bg-neutral-950 text-neutral-50" data-testid="dashboard">
      <header className="border-b border-neutral-800 px-6 py-3 flex items-center justify-between bg-neutral-950 sticky top-0 z-20">
        <div className="flex items-center gap-3">
          <Activity className="w-5 h-5 text-emerald-400" />
          <div>
            <h1 className="text-base font-mono font-bold tracking-tight" data-testid="app-title">PUMP.BOT // micro-stake</h1>
            <p className="text-[10px] uppercase tracking-[0.2em] text-neutral-500">preview-only · solana mainnet</p>
          </div>
        </div>
        <div className="flex items-center gap-4 text-xs font-mono">
          {config && (
            <AutopilotSwitch
              enabled={!!config.autopilot_enabled}
              onChange={onAutopilotChange}
            />
          )}
          <PodPill />
          <span className="flex items-center gap-2" data-testid="ws-status">
            <span className={`w-2 h-2 rounded-full ${wsConnected ? "bg-emerald-400 animate-pulse" : "bg-neutral-600"}`}></span>
            <span className="text-neutral-400">{wsConnected ? "WS LIVE" : "WS OFFLINE"}</span>
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
                status.enabled && Object.keys(status.books_paused || {}).length > 0
                  ? "border-amber-600/70 text-amber-200 bg-amber-950/40 hover:bg-amber-900/40"
                  : status.enabled
                  ? "border-emerald-700/60 text-emerald-300 hover:bg-emerald-950/40"
                  : "border-rose-800/60 text-rose-300 hover:bg-rose-950/40"
              }`}
              title={status.enabled
                ? (Object.keys(status.books_paused || {}).length > 0
                  ? `Bot is RUNNING but the live-doctor breaker has benched: ${Object.keys(status.books_paused).join(", ")} — no new entries for those books (see the amber strip below to LIFT). Click to stop.`
                  : "Bot is RUNNING — click to stop")
                : "Bot is STOPPED — click to start"}
            >
              <span className={`inline-block w-1.5 h-1.5 rounded-full mr-1.5 ${status.enabled ? (Object.keys(status.books_paused || {}).length > 0 ? "bg-amber-400 animate-pulse" : "bg-emerald-500 animate-pulse") : "bg-rose-500"}`} />
              {status.enabled
                ? (Object.keys(status.books_paused || {}).length > 0
                  ? `RUNNING · ${Object.keys(status.books_paused).map((b) => b.replace("_", " ")).join(" + ")} BENCHED`
                  : "RUNNING")
                : "STOPPED"}
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

      <StatusBanner status={status} onResetKill={onResetKill} />
      <HaltBanner />
      <ReadinessBanner onChanged={refreshAll} />
      <NavBar value={view} onChange={pickView} status={status} onStart={onStart} onStop={onStop} badges={navBadges} />

      <main className="max-w-[1600px] mx-auto p-4 space-y-4 pb-16">
        {config?.autopilot_enabled && (
          <div className="border border-lime-800/60 bg-lime-950/20 px-4 py-2 text-[11px] font-mono text-lime-200 flex items-center gap-2" data-testid="autopilot-banner">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-lime-400 animate-pulse" />
            AUTOPILOT — the Doctor is driving: sizing from bankroll, tuning on expectancy in R, one canary at a time. You keep Start/Stop and live/paper.
          </div>
        )}

        {view === "live" && (
          <div key="live" className="space-y-4" data-testid="view-live">
            <div className="tile-in"><KpiStrip wallet={wallet} status={status} config={config} auto={auto} pl={pl} /></div>
            <div className="grid grid-cols-1 lg:grid-cols-[1.15fr_1fr] gap-4 items-stretch" data-testid="cockpit-grid">
              <div className="tile-in" style={{ animationDelay: "60ms" }}><ActiveTradesCockpit trades={activeTrades} onExit={onExitTrade} /></div>
              <div className="tile-in" style={{ animationDelay: "120ms" }}><EquityPanel refreshKey={pl?.cumulative_usd} /></div>
              <div className="tile-in" style={{ animationDelay: "180ms" }}><CompactCandidates candidates={scanner} scannerEnabled={scannerEnabled} onEnableScanner={onEnableScanner} /></div>
              <div className="tile-in" style={{ animationDelay: "240ms" }}><CompactLadder ladder={ladder} /></div>
            </div>
            <div className="tile-in" style={{ animationDelay: "300ms" }}><DoctorStrip status={status} auto={auto} pending={pendingDoc} onOpenDoctor={onOpenDoctor} /></div>

            <MinimizableCard id="trade-history" title="Trade history" stat={`${history.length} trades`}>
              <TradeHistoryTable history={history} />
            </MinimizableCard>
            <CollapsibleSection
              title="Re-entry Watchlist"
              description={`${reentry?.length || 0} winners eligible to re-buy`}
              storageKey="ui.section.reentry"
              testId="section-reentry"
              badge={reentry?.length ? String(reentry.length) : null}
            >
              <ReentryWatchCard watchlist={reentry} onRefresh={onReentryRefresh} />
            </CollapsibleSection>
          </div>
        )}

        {view === "ladder" && (
          <div key="ladder" className="space-y-4 tile-in" data-testid="view-ladder">
            <div className="control-card">
              <GraduateLadderCard ladder={ladder} config={config} onConfigPatch={onConfigPatch} onRefresh={onLadderRefresh} />
            </div>
          </div>
        )}

        {view === "books" && (
          <div key="books" className="space-y-4" data-testid="view-books">
            <div className="tile-in">
              <CollapsibleSection
                title="P/L by Source"
                description="harvest vs search — momentum_new · momentum_seasoned · greylist_snipe · reentry"
                storageKey="ui.section.pl-by-source"
                testId="section-pl-by-source"
                defaultOpen
              >
                <PLBySourceCard refreshSignal={plSourceRefresh} />
              </CollapsibleSection>
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start tile-in" style={{ animationDelay: "80ms" }}>
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
          </div>
        )}

        {view === "scan" && (
          <div key="scan" className="space-y-4" data-testid="view-scan">
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
              <div className="tile-in">
                <MinimizableCard id="launch-feed" title="Live launch feed" stat={`${launches.length} tracked`}>
                  <RecentLaunchesFeed launches={launches} feedLive={feedLive} />
                </MinimizableCard>
              </div>
              <div className="tile-in" style={{ animationDelay: "80ms" }}>
                <CollapsibleSection
                  title="Scanner Candidates"
                  description="live tokens being tracked towards entry"
                  storageKey="ui.section.scanner"
                  testId="section-scanner"
                  defaultOpen
                  badge={scanner?.length ? String(scanner.length) : null}
                >
                  <ScannerCandidatesCard candidates={scanner} config={config} />
                </CollapsibleSection>
              </div>
            </div>
            <CollapsibleSection
              title="Creator Greylist"
              description="creator scoring · pattern analytics · sniper targets"
              storageKey="ui.section.greylist"
              testId="section-greylist"
            >
              <CreatorGreylistPanel config={config} onConfigUpdate={setConfig} />
            </CollapsibleSection>
          </div>
        )}

        {view === "doctor" && (
          <div key="doctor" className="space-y-4" data-testid="view-doctor">
            <DoctorWorkspace config={config} onConfigUpdate={setConfig} onApplied={onDoctorApplied} status={status} auto={auto}
              onReloadAuto={onReloadAuto} />
          </div>
        )}

        {view === "wiki" && <div key="wiki" className="tile-in"><Wiki /></div>}

        {view === "control" && (
          <div key="control" className="space-y-4" data-testid="view-control">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 items-start">
              <div className="tile-in">
                <MinimizableCard id="wallet" title="Wallet" stat={wallet ? `${wallet.sol_balance.toFixed(4)} SOL` : "—"}>
                  <WalletCard wallet={wallet} />
                </MinimizableCard>
              </div>
              <div className="tile-in" style={{ animationDelay: "80ms" }}>
                <MinimizableCard id="rh-wallet" title="Robinhood wallet" stat={config?.rh_live_trading ? "LIVE" : config?.rh_paper_enabled ? "paper" : "off"}>
                  <RhWalletCard config={config} />
                </MinimizableCard>
              </div>
              <div className="tile-in" style={{ animationDelay: "160ms" }}>
                <MinimizableCard id="daily-loss" title="Daily loss" stat={`$${Number(status?.daily_loss_usd ?? 0).toFixed(2)} / $${Number(status?.daily_kill_switch_usd ?? 0).toFixed(0)}`}>
                  <DailyLossMeter status={status} onReset={refreshAll} />
                </MinimizableCard>
              </div>
            </div>
            <MinimizableCard id="pl" title="P/L today · paper reset" stat={`${(pl?.daily_pnl_usd ?? 0) >= 0 ? "+" : "-"}$${Math.abs(pl?.daily_pnl_usd ?? 0).toFixed(2)}`}>
              <PLSummaryCard pl={pl} status={status} onReset={refreshAll} />
            </MinimizableCard>
            <CollapsibleSection
              title="Bot Control"
              description="Start/Stop · bands · gates · greylist sniper config"
              storageKey="ui.section.bot-control"
              testId="section-bot-control"
              defaultOpen
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
          </div>
        )}

        <footer className="text-[10px] text-neutral-600 font-mono text-center pt-4 pb-8 tracking-wider uppercase">
          // Preview-only. Real funds at risk. Never deploy this outside Emergent preview.
        </footer>
      </main>
      <SkipTicker items={skipFeed.items} count10m={skipFeed.count10m} live={wsConnected && !!status?.enabled} />
    </div>
    </TooltipProvider>
  );
}
