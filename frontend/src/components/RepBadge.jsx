import { useReputation } from "@/lib/useReputation";

const TONE = {
  CRAZY: "border-cyan-700 text-cyan-200 bg-cyan-950/40",
  PROVEN: "border-emerald-700 text-emerald-200 bg-emerald-950/40",
  GOOD: "border-lime-700 text-lime-200 bg-lime-950/30",
  UNKNOWN: "border-neutral-800 text-neutral-500",
  FARMER: "border-red-800 text-red-200 bg-red-950/50",
};
const HINT = {
  CRAZY: "dev rank CRAZY (reputation.family) — top tier, repeated real traction",
  PROVEN: "dev rank PROVEN — has graduated / hit real market caps before",
  GOOD: "dev rank GOOD — some real traction on record",
  UNKNOWN: "dev rank UNKNOWN — not enough history; Hunt will not enter, Scalp may",
  FARMER: "dev rank FARMER — 5+ launches with no traction or a fake chart in 7 days; the bot skips this dev on every book",
};

// Dev-reputation chip for a Solana mint. Renders nothing while the adapter is dark or the lookup is in flight.
export function RepBadge({ mint, creator, compact = false }) {
  const rep = useReputation(mint, creator);
  if (!rep || rep.dark) return null;
  const tier = rep.tier || "UNKNOWN";
  const label = compact ? tier.slice(0, 1) : tier;
  return (
    <span className="inline-flex items-center gap-1 align-middle" data-testid={`rep-badge-${mint}`} title={`${HINT[tier] || tier}${rep.fake_chart ? "\nFAKE CHART flagged by reputation.family" : ""}`}>
      <span className={`px-1 py-px border text-[9px] leading-none tracking-[0.15em] ${TONE[tier] || TONE.UNKNOWN}`}>{label}</span>
      {rep.fake_chart && <span className="px-1 py-px border border-amber-700 bg-amber-950/50 text-amber-200 text-[9px] leading-none tracking-[0.15em]">{compact ? "F!" : "FAKE"}</span>}
    </span>
  );
}
