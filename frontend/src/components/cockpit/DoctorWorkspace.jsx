import { memo, useState } from "react";
import { BOOKS, BOOK_CHAIN, EMPTY_FILTER, loadFilter, saveFilter } from "@/lib/doctorFilter";
import AutopilotCard from "../AutopilotCard";
import StrategyDoctorPanel, { TempoGauge } from "../StrategyDoctorPanel";
import DoctorLivePanels from "../DoctorLivePanels";
import LearningBooksPanel from "../LearningBooksPanel";
import CostTrackerCard from "../CostTrackerCard";
import Explain from "./Explain";

const TABS = [
  ["now", "NOW", "what is driving right now — bankrolls, sizing, breakers, canary, tempo, budget"],
  ["proposals", "PROPOSALS", "what the Doctor wants to change — apply, dismiss, revert"],
  ["forensics", "FORENSICS", "why — technique lab, learning books, autopsies, replay, costs"],
];

function Chip({ active, onClick, children, testid, cls = "border-emerald-700 text-emerald-300" }) {
  return (
    <button type="button" onClick={onClick} data-testid={testid}
      className={`px-2 py-0.5 border font-mono text-[10px] tracking-[0.1em] transition-colors duration-100 ${active ? `${cls} bg-neutral-900` : "border-neutral-800 text-neutral-500 hover:text-neutral-200"}`}>{children}</button>
  );
}

function FilterBar({ filter, onChange }) {
  const setChain = (chain) => onChange({ chain, book: filter.book !== "all" && BOOK_CHAIN[filter.book] !== chain && chain !== "all" ? "all" : filter.book });
  const setBook = (book) => onChange({ book, chain: book === "all" ? filter.chain : BOOK_CHAIN[book] });
  const books = BOOKS.filter((b) => filter.chain === "all" || BOOK_CHAIN[b] === filter.chain);
  return (
    <div className="flex items-center gap-1 flex-wrap" data-testid="doctor-filter">
      <span className="text-[9px] uppercase tracking-[0.2em] text-neutral-600 mr-1">filter</span>
      <Chip active={filter.chain === "all" && filter.book === "all"} onClick={() => onChange(EMPTY_FILTER)} testid="doctor-filter-all">ALL</Chip>
      <Chip active={filter.chain === "sol"} onClick={() => setChain("sol")} testid="doctor-filter-sol" cls="border-sky-700 text-sky-300">SOL</Chip>
      <Chip active={filter.chain === "rh"} onClick={() => setChain("rh")} testid="doctor-filter-rh" cls="border-lime-700 text-lime-300">RH</Chip>
      <span className="w-px h-3 bg-neutral-800 mx-1" />
      {books.map((b) => <Chip key={b} active={filter.book === b} onClick={() => setBook(filter.book === b ? "all" : b)} testid={`doctor-filter-${b}`}>{b.replace("_", " ")}</Chip>)}
    </div>
  );
}

function DoctorWorkspace({ config, onConfigUpdate, onApplied, status, auto, onReloadAuto }) {
  const [tab, setTab] = useState(() => localStorage.getItem("ui.doctor.tab") || "now");
  const [filter, setFilter] = useState(loadFilter);
  const pickTab = (t) => { setTab(t); localStorage.setItem("ui.doctor.tab", t); };
  const pickFilter = (f) => { setFilter(f); saveFilter(f); };
  return (
    <div className="control-card !p-0" data-testid="doctor-workspace">
      <div className="px-3 py-2 border-b border-neutral-800 flex items-center gap-4 flex-wrap">
        <div className="flex items-center" data-testid="doctor-tabs">
          {TABS.map(([id, label, hint]) => (
            <button key={id} type="button" onClick={() => pickTab(id)} data-testid={`doctor-tab-${id}`} title={hint}
              className={`relative px-4 py-1 font-mono text-[11px] tracking-[0.25em] transition-colors duration-150 ${tab === id ? "text-emerald-300" : "text-neutral-500 hover:text-neutral-200"}`}>
              {label}
              <span className={`absolute left-2 right-2 -bottom-2 h-[2px] bg-emerald-400 transition-transform duration-200 origin-left ${tab === id ? "scale-x-100" : "scale-x-0"}`} />
            </button>
          ))}
        </div>
        <div className="ml-auto"><FilterBar filter={filter} onChange={pickFilter} /></div>
      </div>

      <div className="p-3 space-y-3">
        {tab === "now" && (
          <div key="now" className="space-y-3 tile-in" data-testid="doctor-now">
            <Explain short="The Doctor runs every 30 min and keeps working when you are logged out. NOW shows what it is doing to sizing and gates at this minute." testid="doctor-explain-now">
              Each chain has its own bankroll (SOL wallet or paper pool · ETH wallet or paper pool), never mixed. Stake = bankroll × risk %, kill switch = bankroll × daily loss %, recomputed every 60 s. Breakers bench a book on live and shrink it to ×0.5 size with gates ×1.25 on paper. Tempo scales every gate by √(current flow ÷ baseline). One canary at a time — promoted only if R expectancy after the change beats baseline, reverted (and blacklisted 24 h) if worse or drawdown +15 %.
            </Explain>
            <TempoGauge tempo={status?.market_tempo} filter={filter} />
            <AutopilotCard config={config} onConfigUpdate={onConfigUpdate} section="now" filter={filter} status={auto} onReload={onReloadAuto} />
            <DoctorLivePanels parts={["trail", "helius"]} />
          </div>
        )}
        {tab === "proposals" && (
          <div key="proposals" className="space-y-3 tile-in" data-testid="doctor-proposals">
            <StrategyDoctorPanel config={config} onConfigUpdate={onConfigUpdate} onApplied={onApplied} section="proposals" filter={filter} />
            <DoctorLivePanels parts={["history"]} />
          </div>
        )}
        {tab === "forensics" && (
          <div key="forensics" className="space-y-3 tile-in" data-testid="doctor-forensics">
            <Explain short="Evidence only — nothing here changes config. Technique lab replays each book's own fills against a TP/SL/hold grid; learning books score expectancy in R; autopsies explain the worst exits." testid="doctor-explain-forensics" />
            <AutopilotCard config={config} onConfigUpdate={onConfigUpdate} section="forensics" filter={filter} status={auto} onReload={onReloadAuto} />
            <LearningBooksPanel filter={filter} />
            <CostTrackerCard apiBase={process.env.REACT_APP_BACKEND_URL || ""} />
          </div>
        )}
      </div>
    </div>
  );
}

export default memo(DoctorWorkspace);
