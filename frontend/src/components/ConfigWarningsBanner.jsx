import { useEffect, useState } from "react";
import { AlertOctagon, TriangleAlert, ChevronDown, ChevronUp } from "lucide-react";
import { api } from "@/lib/api";

// Amber strip: settings that are each valid but together mean an entry path can never fire (smart picks, r-size…).
export default function ConfigWarningsBanner({ config }) {
  const [data, setData] = useState(null);
  const [open, setOpen] = useState(true);
  useEffect(() => {
    let alive = true;
    const load = () => api.configWarnings().then((d) => alive && setData(d)).catch(() => {});
    load();
    const poll = setInterval(load, 30000);
    return () => { alive = false; clearInterval(poll); };
  }, [config]);
  const list = data?.warnings || [];
  if (!list.length) return null;
  const dead = list.filter((w) => w.level === "dead").length;
  return (
    <div className="border-b border-amber-900/70 bg-amber-950/30 px-6 py-2 text-[11px] font-mono" data-testid="config-warnings-banner">
      <button type="button" onClick={() => setOpen((o) => !o)} data-testid="config-warnings-toggle"
        className="inline-flex items-center gap-1.5 text-amber-200 uppercase tracking-[0.15em]">
        <AlertOctagon className="w-3.5 h-3.5" /> config check · {list.length} warning{list.length !== 1 ? "s" : ""}
        {dead > 0 && <span className="text-red-300">· {dead} dead-state{dead !== 1 ? "s" : ""}</span>}
        {open ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
      </button>
      {open && (
        <ul className="mt-1.5 flex flex-col gap-1">
          {list.map((w) => (
            <li key={w.key} className="flex flex-wrap items-baseline gap-x-2" data-testid={`config-warning-${w.key}`}>
              <TriangleAlert className={`w-3 h-3 shrink-0 self-center ${w.level === "dead" ? "text-red-300" : "text-amber-300"}`} />
              <span className={w.level === "dead" ? "text-red-100" : "text-amber-100/90"}>{w.msg}</span>
              <span className="text-neutral-400">→ {w.fix}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
