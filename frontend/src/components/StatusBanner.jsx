import { AlertTriangle, ShieldOff, CheckCircle2 } from "lucide-react";

export default function StatusBanner({ status, onResetKill }) {
  if (!status) return null;

  let bg, txt, icon, label;
  if (status.kill_switch_tripped) {
    bg = "bg-red-950 border-red-800";
    txt = "text-red-400";
    icon = <ShieldOff className="w-4 h-4" />;
    label = "KILL SWITCH TRIPPED · daily loss exceeded";
  } else if (status.enabled) {
    bg = "bg-emerald-950 border-emerald-800";
    txt = "text-emerald-400";
    icon = <CheckCircle2 className="w-4 h-4" />;
    label = status.live_trading ? "BOT RUNNING · LIVE TRADING" : "BOT RUNNING · PAPER MODE";
  } else {
    bg = "bg-amber-950 border-amber-800";
    txt = "text-amber-400";
    icon = <AlertTriangle className="w-4 h-4" />;
    label = "BOT PAUSED · awaiting start";
  }

  return (
    <div className={`border-b ${bg} px-6 py-2 flex items-center justify-between`} data-testid="status-banner">
      <div className={`flex items-center gap-2 text-xs font-mono tracking-wider uppercase ${txt}`}>
        {icon}
        <span data-testid="status-banner-label">{label}</span>
      </div>
      {status.kill_switch_tripped && (
        <button
          data-testid="reset-killswitch-btn"
          onClick={onResetKill}
          className="text-xs font-mono uppercase tracking-wider px-3 py-1 border border-red-700 text-red-300 hover:bg-red-900/40 transition-colors duration-100"
        >
          Reset kill switch
        </button>
      )}
    </div>
  );
}
