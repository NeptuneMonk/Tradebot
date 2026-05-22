import { useEffect, useState, useCallback } from "react";
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
import { Activity } from "lucide-react";

export default function Dashboard() {
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
      case "launch":
        setLaunches((prev) => [data, ...prev.filter((l) => l.id !== data.id)].slice(0, 50));
        break;
      case "launch_update":
        setLaunches((prev) => prev.map((l) => (l.id === data.id ? { ...l, ...data } : l)));
        break;
      case "trade_enter":
        setActiveTrades((prev) => [data, ...prev.filter((t) => t.id !== data.id)]);
        break;
      case "trade_update":
        setActiveTrades((prev) => prev.map((t) => (t.id === data.id ? { ...t, ...data } : t)));
        break;
      case "trade_exit":
        setActiveTrades((prev) => prev.filter((t) => t.id !== data.id));
        setHistory((prev) => [data, ...prev]);
        api.plSummary(7).then(setPl).catch(() => {});
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
      default:
        break;
    }
  }, []));

  return (
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
        </div>
      </header>

      <StatusBanner status={status} onResetKill={async () => { await api.resetKillSwitch(); refreshAll(); }} />

      <main className="max-w-[1600px] mx-auto p-4 md:p-6 space-y-4 md:space-y-6">
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 md:gap-6">
          <WalletCard wallet={wallet} />
          <BotControlCard
            status={status}
            config={config}
            onUpdate={async (cfg) => { setConfig(await api.updateConfig(cfg)); refreshAll(); }}
            onStart={async () => { await api.start(); refreshAll(); }}
            onStop={async () => { await api.stop(); refreshAll(); }}
          />
          <PLSummaryCard pl={pl} />
          <DailyLossMeter status={status} />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 md:gap-6">
          <ActiveTradesTable trades={activeTrades} onExit={async (id) => { await api.exitTrade(id); refreshAll(); }} />
          <RecentLaunchesFeed launches={launches} />
        </div>

        <ReentryWatchCard watchlist={reentry} onRefresh={() => api.reentryWatchlist().then(setReentry).catch(() => {})} />

        <ScannerCandidatesCard candidates={scanner} />

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 md:gap-6">
          <TradeHistoryTable history={history} />
          <ClassifierRulesEditor rules={rules} onSave={async (r) => { setRules(await api.updateRules(r)); }} />
        </div>

        <footer className="text-[10px] text-neutral-600 font-mono text-center pt-4 pb-8 tracking-wider uppercase">
          // Preview-only. Real funds at risk. Never deploy this outside Emergent preview.
        </footer>
      </main>
    </div>
  );
}
