// Deep links into the WIKI tab from anywhere in the app.
//   openWiki("gates-sol")                      → jump to a section
//   openWiki("exits", "stop-loss")             → jump + highlight rows mentioning the term
//   openWiki(null, "flush")                    → open the wiki with the search box prefilled
export function openWiki(sectionId, term) {
  const hash = sectionId ? `#wiki/${sectionId}${term ? `:${encodeURIComponent(term)}` : ""}` : `#wiki/search:${encodeURIComponent(term || "")}`;
  window.history.replaceState(null, "", hash);
  window.dispatchEvent(new CustomEvent("open-wiki", { detail: parseWikiHash(hash) }));
}

export function parseWikiHash(hash = window.location.hash) {
  if (!hash.startsWith("#wiki")) return null;
  const rest = hash.slice("#wiki/".length);
  if (rest.startsWith("search:")) return { section: null, term: decodeURIComponent(rest.slice(7)) };
  const [section, term] = rest.split(":");
  return { section: section || null, term: term ? decodeURIComponent(term) : "" };
}

// CONTROL-card field → wiki section (by data-testid / label heuristics).
export function wikiSectionFor(testid = "", label = "") {
  const k = `${testid} ${label}`.toLowerCase();
  if (/^rh-|robinhood|rh_/.test(k)) return "gates-rh";
  if (/kill|breaker|doctor|autopilot|tempo|regime/.test(k)) return "doctor";
  if (/stop|take-?profit|\btp\b|\bsl\b|trail|hold|clock|momentum|flush|ripcord|rip-cord|exit|persist|dead/.test(k)) return "exits";
  if (/slip|priority|fee|gas|speed|cost/.test(k)) return "costs";
  if (/trade \$|size|stake|risk|budget|search|harvest/.test(k)) return "position-and-risk";
  if (/feed|helius|scanner|wss|socket/.test(k)) return "pipeline";
  return "gates-sol";
}

// Trade-history exit reason → wiki row to highlight.
export function wikiTargetForExit(reason = "") {
  const r = reason.toLowerCase().replace(/_/g, "-");
  const pick = (term, section = "exits") => ({ section, term });
  if (/flush/.test(r)) return pick("flush hold");
  if (/rip-?cord|ripcord|velocity decay|stale-exit|peak-mc|curve-fill|pattern-tp/.test(r)) return pick("rip-cord");
  if (/no-momentum|no momentum/.test(r)) return pick("no_momentum");
  if (/dead-tape|dead tape/.test(r)) return pick("dead-tape");
  if (/tracking-lost|no-pool/.test(r)) return pick("tracking_lost");
  if (/runner/.test(r)) return pick("runner");
  if (/r-trail/.test(r)) return pick("r_trail");
  if (/rug/.test(r)) return pick("rug_detected");
  if (/take-?profit|target/.test(r)) return pick("take-profit");
  if (/stop-?loss|\bsl\b/.test(r)) return pick("stop-loss");
  if (/trail/.test(r)) return pick("trailing-stop");
  if (/max[- _]?hold|clock/.test(r)) return pick("clock");
  if (/ladder/.test(r)) return pick("ladder leg");
  if (/kill/.test(r)) return pick("kill switch");
  if (/graceful/.test(r)) return pick("graceful stop", "doctor");
  if (/manual/.test(r)) return pick("manual");
  if (/gave up|rescued|balance was 0|failed/.test(r)) return pick("stop-loss");
  return pick("");
}
