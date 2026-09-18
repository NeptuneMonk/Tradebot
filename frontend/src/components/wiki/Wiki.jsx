import { useEffect, useMemo, useState } from "react";
import { Search, BookOpen, AlertTriangle, Lightbulb } from "lucide-react";
import { GROUPS, SECTIONS } from "./wikiContent";
import { parseWikiHash } from "@/lib/wikiNav";

const textOf = (s) => [s.title, ...s.body.flatMap((b) => {
  if (b.t === "kv") return b.v.flatMap((r) => r);
  if (b.t === "table") return [...b.cols, ...b.rows.flat()];
  if (b.t === "ul") return b.v;
  return [b.v];
})].join(" ").toLowerCase();

const hit = (hl, ...parts) => !!hl && parts.join(" ").toLowerCase().includes(hl.toLowerCase());
const HL = "bg-emerald-950/50 ring-1 ring-emerald-600";

function Block({ b, hl }) {
  if (b.t === "p") return <p className="text-sm text-neutral-300 leading-relaxed">{b.v}</p>;
  if (b.t === "h") return <h4 className="text-[11px] font-mono uppercase tracking-[0.2em] text-emerald-300 pt-2">{b.v}</h4>;
  if (b.t === "ul") return <ul className="space-y-1.5 text-sm text-neutral-300 leading-relaxed list-none">{b.v.map((x, i) => <li key={i} data-wiki-hit={hit(hl, x) || undefined} className={`pl-4 relative before:content-['▸'] before:absolute before:left-0 before:text-emerald-500 ${hit(hl, x) ? HL : ""}`}>{x}</li>)}</ul>;
  if (b.t === "tip") return <div className="flex gap-2 border border-emerald-900/60 bg-emerald-950/20 p-3 text-sm text-emerald-100"><Lightbulb className="w-4 h-4 shrink-0 mt-0.5 text-emerald-400" />{b.v}</div>;
  if (b.t === "warn") return <div className="flex gap-2 border border-amber-900/60 bg-amber-950/20 p-3 text-sm text-amber-100"><AlertTriangle className="w-4 h-4 shrink-0 mt-0.5 text-amber-400" />{b.v}</div>;
  if (b.t === "kv") return (
    <dl className="grid grid-cols-1 md:grid-cols-[minmax(160px,220px)_1fr] gap-x-6 gap-y-2 text-sm">
      {b.v.map(([k, v]) => <div key={k} className={`contents ${hit(hl, k, v) ? "[&>*]:bg-emerald-950/50" : ""}`}><dt className={`font-mono text-[12px] text-neutral-100 pt-0.5 ${hit(hl, k, v) ? "text-emerald-200" : ""}`} data-wiki-hit={hit(hl, k, v) || undefined}>{k}</dt><dd className="text-neutral-400 leading-relaxed">{v}</dd></div>)}
    </dl>
  );
  if (b.t === "table") return (
    <div className="overflow-x-auto border border-neutral-800">
      <table className="w-full text-xs">
        <thead><tr className="text-[9px] uppercase tracking-[0.2em] text-neutral-500 border-b border-neutral-800">{b.cols.map((c) => <th key={c} className="text-left font-normal px-3 py-2">{c}</th>)}</tr></thead>
        <tbody>{b.rows.map((r, i) => <tr key={i} data-wiki-hit={hit(hl, ...r) || undefined} className={`border-b border-neutral-900 align-top ${hit(hl, ...r) ? HL : ""}`}>{r.map((c, j) => <td key={j} className={`px-3 py-2 ${j === 0 ? "font-mono text-neutral-100 whitespace-nowrap" : "text-neutral-400"}`}>{c}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
  return null;
}

export default function Wiki() {
  const init = parseWikiHash() || {};
  const [q, setQ] = useState(init.section ? "" : (init.term || ""));
  const [active, setActive] = useState(init.section || SECTIONS[0].id);
  const [hl, setHl] = useState(init.section ? init.term || "" : "");
  useEffect(() => {
    const apply = (e) => {
      const d = e?.detail || parseWikiHash();
      if (!d) return;
      if (d.section) { setQ(""); setActive(d.section); setHl(d.term || ""); } else { setHl(""); setQ(d.term || ""); }
    };
    window.addEventListener("open-wiki", apply);
    return () => window.removeEventListener("open-wiki", apply);
  }, []);
  useEffect(() => {
    if (!hl) return undefined;
    const t = setTimeout(() => document.querySelector(`#wiki-${active} [data-wiki-hit]`)?.scrollIntoView({ block: "center" }), 150);
    return () => clearTimeout(t);
  }, [hl, active]);
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return needle ? SECTIONS.filter((s) => textOf(s).includes(needle)) : SECTIONS;
  }, [q]);
  useEffect(() => { if (!q) document.getElementById(`wiki-${active}`)?.scrollIntoView({ block: "start" }); }, [active, q]);
  const jump = (id) => { setQ(""); setHl(""); setActive(id); window.history.replaceState(null, "", `#wiki/${id}`); };
  return (
    <div className="grid grid-cols-1 lg:grid-cols-[260px_1fr] gap-6" data-testid="view-wiki">
      <aside className="lg:sticky lg:top-[110px] self-start space-y-4">
        <div className="flex items-center gap-2 text-[11px] font-mono tracking-[0.25em] text-neutral-200"><BookOpen className="w-4 h-4 text-emerald-400" /> TRADEBOT WIKI</div>
        <label className="flex items-center gap-2 border border-neutral-800 bg-neutral-950 px-2 py-1.5">
          <Search className="w-3.5 h-3.5 text-neutral-500" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="search terms, gates, exits…" data-testid="wiki-search"
            className="bg-transparent outline-none text-xs font-mono text-neutral-100 placeholder:text-neutral-600 w-full" />
        </label>
        <nav className="space-y-3 max-h-[60vh] overflow-auto pr-1" data-testid="wiki-index">
          {GROUPS.map((g) => (
            <div key={g}>
              <div className="text-[9px] uppercase tracking-[0.2em] text-neutral-500 mb-1">{g}</div>
              {SECTIONS.filter((s) => s.group === g).map((s) => (
                <button key={s.id} type="button" onClick={() => jump(s.id)} data-testid={`wiki-nav-${s.id}`}
                  className={`block w-full text-left text-xs py-1 pl-2 border-l transition-colors duration-100 ${active === s.id && !q ? "border-emerald-400 text-emerald-200" : "border-neutral-800 text-neutral-400 hover:text-neutral-100"}`}>{s.title}</button>
              ))}
            </div>
          ))}
        </nav>
      </aside>
      <div className="space-y-10 min-w-0">
        {q && <div className="text-[10px] font-mono uppercase tracking-[0.2em] text-neutral-500" data-testid="wiki-search-count">{shown.length} section{shown.length === 1 ? "" : "s"} match “{q}”</div>}
        {hl && !q && <div className="text-[10px] font-mono uppercase tracking-[0.2em] text-emerald-400" data-testid="wiki-highlight">highlighting “{hl}” <button type="button" className="ml-2 text-neutral-500 hover:text-neutral-200" onClick={() => setHl("")}>clear</button></div>}
        {shown.map((s) => (
          <section key={s.id} id={`wiki-${s.id}`} className="control-card space-y-4 scroll-mt-[120px]" data-testid={`wiki-section-${s.id}`}>
            <div className="text-[9px] uppercase tracking-[0.2em] text-neutral-500">{s.group}</div>
            <h3 className="text-base md:text-lg font-mono text-neutral-100 tracking-wide">{s.title}</h3>
            {s.body.map((b, i) => <Block key={i} b={b} hl={s.id === active ? hl : ""} />)}
          </section>
        ))}
        {shown.length === 0 && <div className="text-sm text-neutral-500">Nothing matches — try a gate name (e.g. “liquidity”), an exit (“trailing”), or a word from the skip ticker.</div>}
      </div>
    </div>
  );
}
