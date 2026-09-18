import { Square, Play } from "lucide-react";

export const VIEWS = [
  { id: "live", label: "LIVE", hint: "cockpit — positions, equity, candidates, ladder" },
  { id: "scan", label: "SCAN", hint: "launch feed, tracked tokens, greylist" },
  { id: "ladder", label: "LADDER", hint: "graduate ladder — full watch list" },
  { id: "doctor", label: "DOCTOR", hint: "strategy doctor, autopilot, costs" },
  { id: "books", label: "BOOKS", hint: "P/L by source, scorecard, classifier, book exits" },
  { id: "control", label: "CONTROL", hint: "bot control, wallets, gates" },
  { id: "wiki", label: "WIKI", hint: "manual — terminology, screens, how to read and adjust gates and exits" },
];

export default function NavBar({ value, onChange, badges = {}, status, onStart, onStop }) {
  const running = !!status?.enabled;
  const stopping = !!status?.stopping_gracefully;
  return (
    <nav data-testid="view-tabs" className="border-b border-neutral-800 bg-neutral-950 sticky top-[49px] z-20">
      <div className="max-w-[1600px] mx-auto px-4 md:px-6 flex items-stretch">
        {VIEWS.map(({ id, label, hint }) => {
          const active = value === id;
          return (
            <button key={id} type="button" data-testid={`view-tab-${id}`} onClick={() => onChange(id)} title={hint}
              className={`relative flex-1 md:flex-none md:px-10 py-3 font-mono text-[12px] tracking-[0.25em] transition-colors duration-150 ${active ? "text-emerald-300" : "text-neutral-400 hover:text-neutral-100"}`}>
              {label}
              {badges[id] != null && <span className="ml-2 text-[9px] px-1 border border-neutral-800 text-neutral-500 tracking-normal">{badges[id]}</span>}
              <span className={`absolute left-0 right-0 bottom-0 h-[2px] bg-emerald-400 transition-transform duration-200 origin-left ${active ? "scale-x-100" : "scale-x-0"}`} />
            </button>
          );
        })}
        <div className="ml-auto flex items-center py-1.5">
          {status && (
            <button type="button" data-testid="nav-run-toggle" onClick={running ? onStop : onStart} disabled={stopping}
              className={`inline-flex items-center gap-2 px-4 py-1.5 border font-mono text-[11px] tracking-[0.2em] transition-colors duration-100 disabled:opacity-60 ${
                running ? "border-red-800 text-red-300 hover:bg-red-950/50" : "border-emerald-800 text-emerald-300 hover:bg-emerald-950/50"}`}
              title={stopping ? "graceful stop in progress — waiting for open positions" : running ? "graceful stop: no new entries, open positions ride to their exits" : "start the bot"}>
              {running ? <Square className="w-3 h-3" /> : <Play className="w-3 h-3" />}
              {stopping ? "STOPPING" : running ? "STOP" : "START"}
            </button>
          )}
        </div>
      </div>
    </nav>
  );
}
