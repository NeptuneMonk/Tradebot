import { Activity, Radar, Stethoscope, SlidersHorizontal } from "lucide-react";

export const VIEWS = [
  { id: "live", label: "LIVE", Icon: Activity, hint: "cockpit — positions, equity, candidates, ladder" },
  { id: "scan", label: "SCAN", Icon: Radar, hint: "launch feed, tracked tokens, scorecard, classifier, greylist" },
  { id: "doctor", label: "DOCTOR", Icon: Stethoscope, hint: "strategy doctor, autopilot, P/L by source, costs" },
  { id: "control", label: "CONTROL", Icon: SlidersHorizontal, hint: "bot control, wallets, gates" },
];

export default function ViewTabs({ value, onChange, badges = {} }) {
  return (
    <nav data-testid="view-tabs" className="flex items-end gap-1 border-b border-neutral-800">
      {VIEWS.map(({ id, label, Icon, hint }) => {
        const active = value === id;
        return (
          <button key={id} type="button" data-testid={`view-tab-${id}`} onClick={() => onChange(id)} title={hint}
            className={`relative flex items-center gap-2 px-4 py-2 font-mono text-[11px] uppercase tracking-[0.2em] transition-colors duration-150 ${active ? "text-neutral-50" : "text-neutral-500 hover:text-neutral-300"}`}>
            <Icon className={`w-3.5 h-3.5 ${active ? "text-blue-400" : ""}`} />
            {label}
            {badges[id] != null && <span className="text-[9px] px-1 border border-neutral-800 text-neutral-400">{badges[id]}</span>}
            <span className={`absolute left-0 right-0 -bottom-px h-px bg-blue-500 transition-transform duration-200 origin-left ${active ? "scale-x-100" : "scale-x-0"}`} />
          </button>
        );
      })}
    </nav>
  );
}
