import { Zap, Leaf, Gauge, Rocket, Flame, Activity } from "lucide-react";

/**
 * Speed Mode slider — 6-tier preset bundle:
 *   0 ECO | 1 NORMAL | 2 FAST | 3 AGGRESSIVE | 4 TURBO | 5 AUTO
 *
 * Each preset bundles priority_fee_microlamports + slippage_bps. The user
 * can also choose "manual" via a separate toggle to fall back to the raw
 * inputs (kept in the Advanced section).
 */

const MODES = [
  { id: "eco",        label: "ECO",        sub: "100k · 3%",  color: "text-emerald-300", border: "border-emerald-700", icon: Leaf },
  { id: "normal",     label: "NORMAL",     sub: "300k · 4%",  color: "text-cyan-300",    border: "border-cyan-700",    icon: Gauge },
  { id: "fast",       label: "FAST",       sub: "700k · 5%",  color: "text-blue-300",    border: "border-blue-700",    icon: Zap },
  { id: "aggressive", label: "AGGRESSIVE", sub: "1.5M · 7%",  color: "text-amber-300",   border: "border-amber-700",   icon: Rocket },
  { id: "turbo",      label: "TURBO",      sub: "3M · 10%",   color: "text-red-300",     border: "border-red-700",     icon: Flame },
  { id: "auto",       label: "AUTO",       sub: "live tune",  color: "text-fuchsia-300", border: "border-fuchsia-700", icon: Activity },
];

export default function SpeedModeSlider({ value, onChange }) {
  const idx = Math.max(0, MODES.findIndex((m) => m.id === value));
  const current = MODES[idx] || MODES[1];
  const Icon = current.icon;

  return (
    <div className="border border-neutral-800 bg-neutral-950/60 p-3" data-testid="speed-mode-slider">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[10px] uppercase tracking-[0.2em] text-neutral-500">Speed Mode</span>
        <div className={`flex items-center gap-1.5 ${current.color} text-[11px] font-mono uppercase tracking-[0.15em]`}>
          <Icon className="w-3 h-3" />
          {current.label} · <span className="text-neutral-500 normal-case">{current.sub}</span>
        </div>
      </div>

      {/* Native range slider — zero deps, works everywhere */}
      <input
        type="range"
        min="0"
        max="5"
        step="1"
        value={idx >= 0 ? idx : 1}
        onChange={(e) => onChange(MODES[parseInt(e.target.value, 10)].id)}
        data-testid="speed-mode-range"
        className="w-full h-1 bg-neutral-800 appearance-none cursor-pointer accent-blue-500 mb-1"
      />

      {/* Tick labels under the slider */}
      <div className="grid grid-cols-6 text-[9px] font-mono uppercase tracking-[0.1em] text-neutral-600 -mt-0.5">
        {MODES.map((m, i) => (
          <button
            key={m.id}
            onClick={() => onChange(m.id)}
            data-testid={`speed-mode-${m.id}`}
            className={`text-center transition-colors duration-100 ${
              i === idx ? `${m.color} font-semibold` : "hover:text-neutral-400"
            }`}
          >
            {m.label.slice(0, 4)}
          </button>
        ))}
      </div>
    </div>
  );
}

export { MODES };
