import { useEffect, useState, useCallback } from "react";
import { toast } from "sonner";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useWebSocket } from "@/lib/useWebSocket";
import StatusBanner from "@/components/StatusBanner";
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
import { TooltipProvider } from "@/components/ui/tooltip";
import { Activity, LogOut } from "lucide-react";

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

  useEffect(() => {
    refreshAll();
    const id = setInterval(refreshAll, 20000);
    return () => clearInterval(id);
  }, [refreshAll]);

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
      case "launch": {
        // Pinned launches survive the 50-item cap (Phase 2.9). The backend
        // already returns pinned-first on the /launches/recent endpoint;
        // here we just ensure WS-driven inserts don't bump them off.
        setLaunches((prev) => {
          const merged = [data, ...prev.filter((l) => l.id !== data.id)];
          const pinned = merged.filter((l) => l.pinned);
          const unpinned = merged.filter((l) => !l.pinned);
          return [...pinned.slice(0, 200), ...unpinned.slice(0, 50)];
        });
        break;
      }
      case "launch_update":
        // Only update if the mint is already in our 50-item window —
        // events for mints we never displayed shouldn't bloat React state.
        setLaunches((prev) => {
          const idx = prev.findIndex((l) => l.id === data.id);
          if (idx === -1) return prev;
          const next = prev.slice();
          next[idx] = { ...next[idx], ...data };
          return next;
        });
        break;
      case "trade_enter":
        setActiveTrades((prev) => [data, ...prev.filter((t) => t.id !== data.id)]);
        // Re-fetch launches so the new pinned card appears at the top
        // immediately, with pin_strategy / pin_creator_pattern populated.
        api.launches().then(setLaunches).catch(() => {});
        break;
      case "trade_update":
        setActiveTrades((prev) => prev.map((t) => (t.id === data.id ? { ...t, ...data } : t)));
        break;
      case "trade_exit":
        setActiveTrades((prev) => prev.filter((t) => t.id !== data.id));
        setHistory((prev) => [data, ...prev]);
        // Refresh launches so the exited card flips to grey/dimmed state
        api.launches().then(setLaunches).catch(() => {});
        api.plSummary(7).then(setPl).catch(() => {});
        setPlSourceRefresh((n) => n + 1);
        // Refresh reentry watchlist (may have a new entry)
        api.reentryWatchlist().then(setReentry).catch(() => {});
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
      case "bot_auto_disabled_on_restart":
        toast.warning(
          `Bot was auto-disabled after backend restart (${data?.active_positions ?? 0} positions retained). Press Start to resume.`,
          { duration: 12000 }
        );
        break;
      default:
        break;
    }
  }, []));

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
          <span className="flex items-center gap-2" data-testid="ws-status">
            <span className={`w-2 h-2 rounded-full ${wsConnected ? "bg-blue-500 animate-pulse" : "bg-neutral-600"}`}></span>
            <span className="text-neutral-400">{wsConnected ? "WS LIVE" : "WS OFFLINE"}</span>
          </span>
          <span className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${status?.listener_connected ? "bg-emerald-500" : "bg-red-500"}`}></span>
            <span className="text-neutral-400" data-testid="listener-status">
              {status?.listener_connected ? "LISTENER LIVE" : "LISTENER OFFLINE"}
            </span>
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

      <main className="max-w-[1600px] mx-auto p-4 md:p-6 space-y-4 md:space-y-6">
        {/* TOP KPI STRIP — always visible. Wallet + PnL + DailyLoss.
            Bot Control moved to a collapsible below; the StatusBanner at
            the top of the page already shows running/stopped state. */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 md:gap-6">
          <WalletCard wallet={wallet} />
          <PLSummaryCard pl={pl} status={status} onReset={refreshAll} />
          <DailyLossMeter status={status} onReset={refreshAll} />
        </div>

        {/* PRIMARY — Active Trades + Recent Launches always visible. */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 md:gap-6">
          <ActiveTradesTable trades={activeTrades} onExit={async (id) => {
            try {
              await api.exitTrade(id);
              toast.success("Manual exit submitted");
            } catch (e) {
              const detail = e?.response?.data?.detail || e?.message || "exit failed";
              if (e?.response?.status === 400 && /not active/i.test(detail)) {
                toast.info("Trade already closed");
              } else {
                toast.error(`Exit failed: ${detail}`);
              }
            } finally {
              refreshAll();
            }
          }} />
          <RecentLaunchesFeed
            launches={launches}
            onUnpin={(launchId) =>
              setLaunches((prev) =>
                prev.map((l) =>
                  l.id === launchId
                    ? { ...l, pinned: false, pin_exited: undefined }
                    : l
                )
              )
            }
          />
        </div>

        {/* Trade History — always visible (collapsed cards above feed flow).
            Co-mounted with Classifier Rules so the second column on wide
            screens stays useful. */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 md:gap-6">
          <TradeHistoryTable history={history} />
          <CollapsibleSection
            title="Classifier Rules"
            description="entry/exit gates — abort & exit-early rules"
            storageKey="ui.section.classifier"
            testId="section-classifier"
          >
            <ClassifierRulesEditor rules={rules} onSave={async (r) => { setRules(await api.updateRules(r)); }} />
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
            status={status}
            config={config}
            onUpdate={async (cfg) => { setConfig(await api.updateConfig(cfg)); refreshAll(); }}
            onStart={async () => { await api.start(); refreshAll(); }}
            onStop={async () => { await api.stop(); refreshAll(); }}
          />
        </CollapsibleSection>

        <CollapsibleSection
          title="Strategy Doctor"
          description="advisory toggle · pending suggestions · live panels"
          storageKey="ui.section.doctor"
          testId="section-doctor"
          badge={config?.doctor_advisory_only ? "advisory" : null}
        >
          <StrategyDoctorPanel
            config={config}
            onConfigUpdate={setConfig}
            onApplied={() => api.config().then(setConfig).catch(() => {})}
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
            onConfigUpdate={(cfg) => setConfig(cfg)}
          />
        </CollapsibleSection>

        <CollapsibleSection
          title="Re-entry Watchlist"
          description={`${reentry?.length || 0} winners eligible to re-buy`}
          storageKey="ui.section.reentry"
          testId="section-reentry"
          badge={reentry?.length ? String(reentry.length) : null}
        >
          <ReentryWatchCard watchlist={reentry} onRefresh={() => api.reentryWatchlist().then(setReentry).catch(() => {})} />
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
