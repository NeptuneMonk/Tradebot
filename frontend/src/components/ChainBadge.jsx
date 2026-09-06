export const CHAIN_META = {
  sol: { label: "SOL", cls: "border-teal-700 text-teal-300 bg-teal-950/40", title: "Solana · Pump.fun / PumpSwap" },
  rh: { label: "RH", cls: "border-lime-700 text-lime-300 bg-lime-950/40", title: "Robinhood Chain · PONS (watch-only)" },
};

export function ChainBadge({ chain, protocol, mint }) {
  const key = chain === "rh" ? "rh" : "sol";
  const meta = CHAIN_META[key];
  return (
    <span
      className={`inline-flex items-center gap-1 text-[9px] font-mono uppercase tracking-[0.15em] px-1.5 py-0.5 border ${meta.cls}`}
      title={meta.title}
      data-testid={`chain-badge-${key}-${mint || "x"}`}
    >
      {meta.label}
      {key === "rh" && protocol && <span className="text-lime-500/80 normal-case tracking-normal">·{protocol}</span>}
    </span>
  );
}

export function ChainFilterChips({ value, onChange, counts }) {
  const opts = [
    ["all", "All"],
    ["sol", "SOL"],
    ["rh", "RH"],
  ];
  return (
    <div className="inline-flex border border-neutral-800" data-testid="launch-chain-filter">
      {opts.map(([k, label]) => (
        <button
          key={k}
          type="button"
          data-testid={`launch-chain-filter-${k}`}
          onClick={() => onChange(k)}
          className={`px-2 py-0.5 text-[9px] font-mono uppercase tracking-[0.15em] transition-colors duration-100 ${
            value === k ? "bg-neutral-200 text-neutral-900" : "text-neutral-400 hover:text-neutral-100 hover:bg-neutral-900"
          }`}
        >
          {label}
          {counts && counts[k] != null && <span className="ml-1 opacity-60">{counts[k]}</span>}
        </button>
      ))}
    </div>
  );
}
