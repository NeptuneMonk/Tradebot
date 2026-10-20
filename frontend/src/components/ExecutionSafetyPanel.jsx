import { useState } from "react";
import { ShieldCheck, ChevronDown, ChevronRight } from "lucide-react";

// EMERGENT_CHANGES execution-safety knobs (schema-driven so every field in BotConfig has a visible, editable control).
const GROUPS = [
  { title: "Exits", fields: [
    { key: "max_exit_slippage_bps", label: "Max exit slip (bps)", type: "int", step: 100, hint: "Ceiling for ALL exit-side slippage (normal, panic, retries, routed)." },
    { key: "exit_terminal_slippage_bps", label: "Terminal slip (bps)", type: "int", step: 500, hint: "One last capped attempt before parking — never unlimited." },
    { key: "exit_max_landed_reverts", label: "Max landed reverts", type: "int", step: 1, hint: "Genuine on-chain reverts before the emergency PumpSwap attempt + park." },
    { key: "exit_requote_every_attempt", label: "Re-quote every attempt", type: "bool", hint: "Fresh curve/pool read before every exit attempt." },
    { key: "hard_stop_extra_pct", label: "Hard stop extra %", type: "float", step: 1, hint: "pnl ≤ −(book stop + this) exits NOW, bypassing every delay layer." },
    { key: "max_hold_any_book_s", label: "Max hold any book (s)", type: "int", step: 60, hint: "Absolute clock for every non-manual position, from the PERSISTED entry time (survives restarts). 0 = off." },
    { key: "tp_chain_confirm_enabled", label: "Confirm TP on chain", type: "bool", hint: "A tape tick that says take-profit is re-checked against the curve / pool price before selling. Stops corrupt +300 %…+12000 % ticks from dumping a position at −3 %. One RPC read per TP trigger." },
    { key: "dead_tape_mode", label: "Dead tape", type: "enum", options: ["label", "exit"], hint: "label = flag the row \"DEAD TAPE?\" and HOLD — you decide · exit = close the position automatically." },
    { key: "dead_tape_exit_on_red", label: "Dead tape exits red (exit mode)", type: "bool", hint: "Exit mode only: search_dead_tape may close red positions too." },
  ]},
  { title: "Compute & fees", fields: [
    { key: "cu_limit_pumpfun_buy", label: "CU pump.fun buy", type: "int", step: 5000 },
    { key: "cu_limit_pumpfun_sell", label: "CU pump.fun sell", type: "int", step: 5000 },
    { key: "cu_limit_pumpswap_buy", label: "CU PumpSwap buy", type: "int", step: 5000 },
    { key: "cu_limit_pumpswap_sell", label: "CU PumpSwap sell", type: "int", step: 5000 },
    { key: "cu_limit_routed_mult", label: "Routed CU mult", type: "float", step: 0.1, hint: "Routed sells: simulate → unitsConsumed × this." },
    { key: "entry_fee_max_pct_of_position", label: "Entry fee max % of position", type: "float", step: 0.25, hint: "Priority + tip of ONE buy attempt ≤ this % of the stake." },
    { key: "exit_fee_max_pct_of_position", label: "Exit fee max % of position", type: "float", step: 0.25, hint: "Priority + tip of ONE sell attempt ≤ this % of position value." },
    { key: "exit_fee_budget_pct_of_position", label: "Exit fee BUDGET % of position", type: "float", step: 0.5, hint: "TOTAL fees across ALL sell attempts of one trade ≤ this %. When nearly spent, only floor-priority sells go out." },
    { key: "exit_sender_mode_normal", label: "Sender mode (normal exits)", type: "enum", options: ["swqos", "dual"], hint: "swqos = 5,000-lamport tip · dual = validators + Jito, 200,000-lamport tip." },
    { key: "exit_sender_mode_panic", label: "Sender mode (panic exits)", type: "enum", options: ["swqos", "dual"] },
    { key: "buy_fee_ladder_rungs", label: "Buy fee ladder rungs", type: "int", step: 1, hint: "1 = single tx (no duplicate copies); 2 = [p, 2p]; 3 = [p, 2p, 4p]. Idempotent across rungs." },
    { key: "land_late_watch_s", label: "Late-rung watch (s)", type: "int", step: 5, hint: "Keep polling unchosen rung signatures this long after a landing." },
  ]},
  { title: "Cost gate & venue fees", fields: [
    { key: "cost_gate_max_round_trip_pct", label: "Max round-trip cost %", type: "float", step: 0.5, hint: "Entry refused when slip + fee + priority + tip + failed-tx allowance (round trip) exceeds this." },
    { key: "cost_gate_cost_mult", label: "Cost mult", type: "float", step: 0.25, hint: "Expected move must beat round-trip cost × this." },
    { key: "cost_gate_adverse_fill_pct", label: "Adverse fill % / side", type: "float", step: 0.25, hint: "Expected price move between quote and fill on top of our own impact, charged on BOTH sides in the cost gate. At 1.0 the gate's fixed part is ≈ 2 % + protocol fee ×2 + 0.5 % shave + failed-tx allowance ≈ 6.5 % before any pool impact — near-graduation curve tokens sit right at the 8 % ceiling. 0.5 is realistic for a 60–80 SOL curve." },
    { key: "cost_gate_failed_tx_allowance_pct", label: "Failed-tx allowance %", type: "float", step: 0.25, hint: "Budget for reverted txs in the gate; also the per-tx failure probability simulated in paper mode." },
    { key: "protocol_fee_pct_pumpfun", label: "Protocol fee % pump.fun", type: "float", step: 0.05, hint: "Applied in max_sol_cost / min_sol_output and in paper fills." },
    { key: "protocol_fee_pct_pumpswap", label: "Protocol fee % PumpSwap", type: "float", step: 0.05 },
    { key: "skip_mayhem_coins", label: "Skip mayhem-mode coins", type: "bool", hint: "Token-2022 + mayhem fee recipient curves are skipped on every buy path." },
    { key: "pump_suffix_only", label: "Pump.fun mints only (…pump)", type: "bool", hint: "Every automatic Solana path (New / Seasoned momentum, snipes, smart-wallet triggers) only takes mints whose address ends in 'pump' — the vanity suffix every pump.fun launch carries. Non-pump mints (…moon, …bonk, random) are never tracked or scored. Manual buys are unaffected." },
    { key: "pumpswap_canonical_pool_only", label: "PumpSwap: pump.fun pools only", type: "bool", hint: "Only enter PumpSwap pools whose creator is pump.fun's migration authority (LP locked) — the quote side may be SOL, PUMP, USDC or another coin. Anyone can open a pool for any mint — pump.fun shows those tokens as 'unverified' — and pull the LP later: 4 such pools (wallet-created, token on the quote side) drained mid-hold on 2026-10-19 and printed +1576 % 'take-profits' that filled at −100 %. Inverted pools are always refused; manual / force buys are otherwise still allowed." },
    { key: "pool_drain_exit_pct", label: "Pool drained below % of entry depth", type: "float", step: 5, hint: "PumpSwap: when the pool's SOL depth falls under this % of what it had at entry the LP was pulled — price ticks are no longer take-profits and the position exits as 'pool drained' (rug). 0 = off." },
  ]},
  { title: "Entry latency & sizing", fields: [
    { key: "fast_fail_split_enabled", label: "Half-now / half-later", type: "bool", hint: "Opt-in: buy half at entry, add the rest once price is up Fast-fail add %." },
    { key: "fast_fail_add_at_pct", label: "Fast-fail add at %", type: "float", step: 0.5 },
    { key: "socials_fetch_blocking", label: "Socials fetch blocks entry", type: "bool", hint: "Off = metadata is never awaited on the entry path (missing → socials-pending skip)." },
    { key: "entry_balance_cache_s", label: "Balance cache (s)", type: "int", step: 1, hint: "Reuse a wallet balance read this fresh (minus SOL committed since)." },
    { key: "paper_entry_latency_ms", label: "Paper entry latency (ms)", type: "int", step: 50, hint: "Paper fills sleep this long and re-read the venue before booking." },
    { key: "paper_exit_latency_ms", label: "Paper exit latency (ms)", type: "int", step: 50 },
  ]},
  { title: "Robinhood Chain", fields: [
    { key: "rh_protocol_fee_pct", label: "RH curve fee %", type: "float", step: 0.1, hint: "Subtracted from the quote before the min-out tolerance." },
    { key: "rh_sell_fallback_slippage_pct", label: "RH sell fallback slip %", type: "float", step: 1, hint: "ONE retry at this capped slippage when the sell simulation reverts — never min-out 0." },
  ]},
  { title: "Smart-wallet holder check", fields: [
    { key: "smart_holder_check_enabled", label: "Holder cross-check", type: "bool", hint: "Every scan, read the holders of the busiest in-band candidates and match them against your smart-wallet list (manual first, promoted learned too). A match counts exactly like a tape hit — lifts creator BLOCK (manual), picks the trending-name leader, fires the hard trigger." },
    { key: "smart_holder_check_every_s", label: "Re-check every (s)", type: "int", step: 10 },
    { key: "smart_holder_check_max_per_scan", label: "Tokens per scan", type: "int", step: 1, hint: "DAS getTokenAccounts reads per scan (≤ 3 pages each). Blocked-by-creator / trending copies go first." },
    { key: "smart_holder_check_min_buyers", label: "Min buyers / holders", type: "int", step: 5 },
  ]},
  { title: "Trending names", fields: [
    { key: "trend_name_gate_enabled", label: "Trending-name gate", type: "bool", hint: "A ticker cloned ≥ N times inside the band window: buy ONLY the copy smart wallets pile into (leader = smart wallets → MC → buyers)." },
    { key: "trend_name_min_copies", label: "Copies = trending", type: "int", step: 1, hint: "Same-name launches inside the band window before the name counts as trending." },
    { key: "trend_name_leader_min_smart", label: "Leader min smart buys", type: "int", step: 1, hint: "The leader needs this many smart-wallet buys before ANY copy is bought. 0 = MC/buyers leader is enough." },
    { key: "trend_name_max_positions", label: "Max positions per name", type: "int", step: 1, hint: "Ours + the leader; the older copy leaves on its normal exits." },
    { key: "trend_name_hunt_enabled", label: "Hunt the top dog", type: "bool", hint: "When a name trends, wide-search every copy of the ticker from the last N hours (outside the band time gates), rank them (smart wallets → MC → 1h buys → holders) and enter ONLY the top dog." },
    { key: "trend_name_search_window_h", label: "Wide-search window (h)", type: "float", step: 0.5 },
    { key: "trend_name_hunt_every_s", label: "Re-search every (s)", type: "int", step: 10 },
    { key: "trend_name_holder_reads", label: "Holder reads per search", type: "int", step: 1, hint: "Top copies by MC that get a smart-wallet holder read (DAS) each search." },
  ]},
];

function Control({ f, value, onChange }) {
  const cls = "bg-neutral-950 border border-neutral-800 px-2 py-1 font-mono text-sm focus:border-blue-500 focus:outline-none";
  const testid = `exec-${f.key.replace(/_/g, "-")}`;
  if (f.type === "bool") {
    return (
      <select data-testid={testid} value={value ? "on" : "off"} onChange={(e) => onChange(e.target.value === "on")} className={cls}>
        <option value="on">on</option><option value="off">off</option>
      </select>
    );
  }
  if (f.type === "enum") {
    return (
      <select data-testid={testid} value={value ?? f.options[0]} onChange={(e) => onChange(e.target.value)} className={cls}>
        {f.options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    );
  }
  return (
    <input data-testid={testid} type="number" step={f.step} value={value ?? ""} className={cls}
           onChange={(e) => { const v = e.target.value; if (v === "") return; onChange(f.type === "int" ? parseInt(v, 10) : parseFloat(v)); }} />
  );
}

export default function ExecutionSafetyPanel({ local, setLocal }) {
  const [open, setOpen] = useState(false);
  const n = GROUPS.reduce((a, g) => a + g.fields.length, 0);
  return (
    <div className="mt-1 pt-3 border-t border-neutral-800/70" data-testid="execution-safety-panel">
      <button data-testid="execution-safety-toggle" onClick={() => setOpen((o) => !o)}
              className="w-full flex items-center justify-between text-[10px] uppercase tracking-[0.2em] text-neutral-500 hover:text-neutral-300">
        <span className="inline-flex items-center gap-2"><ShieldCheck className="w-3 h-3" /> Execution safety · {n} knobs</span>
        {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
      </button>
      {open && GROUPS.map((g) => (
        <div key={g.title} className="mt-3">
          <div className="text-[10px] uppercase tracking-[0.15em] text-neutral-600 mb-1.5">{g.title}</div>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-x-3 gap-y-2">
            {g.fields.map((f) => (
              <label key={f.key} className="flex flex-col gap-1" title={f.hint}>
                <span className="text-[10px] uppercase tracking-[0.12em] text-neutral-500 truncate">{f.label}</span>
                <Control f={f} value={local?.[f.key]} onChange={(v) => setLocal({ ...local, [f.key]: v })} />
              </label>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
