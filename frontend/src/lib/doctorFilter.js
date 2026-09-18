export const BOOKS = ["scalp", "hunt", "runner", "rh_pons", "ladder"];
export const BOOK_CHAIN = { scalp: "sol", hunt: "sol", runner: "sol", ladder: "sol", rh_pons: "rh" };
export const EMPTY_FILTER = { chain: "all", book: "all" };

/** Does an item tagged with `book` (and optionally `chain`) survive the Doctor filter? Untagged items always show. */
export function matchesFilter(filter, book, chain) {
  if (!filter) return true;
  const c = chain || (book ? BOOK_CHAIN[book] : null);
  if (filter.book !== "all") return !book || book === filter.book;
  if (filter.chain !== "all") return !c || c === filter.chain;
  return true;
}

/** Best-effort book for a Doctor suggestion: metrics.book, else the config keys it touches. */
export function bookOfSuggestion(s) {
  if (s?.metrics?.book) return s.metrics.book;
  if (s?.book) return s.book;
  const keys = Object.keys(s?.actions || {}).join(" ");
  const m = keys.match(/book_exits\.(\w+)\./) || keys.match(/regime_gate_mult\.(\w+)\./) || keys.match(/(scalp|hunt|runner|rh_pons|ladder)_/);
  if (m) return m[1];
  if (/greylist_snipe/.test(keys)) return "hunt";
  if (/^rh_|\srh_/.test(keys)) return "rh_pons";
  return null;
}

export function loadFilter() {
  try { return { ...EMPTY_FILTER, ...(JSON.parse(localStorage.getItem("ui.doctor.filter") || "{}")) }; } catch { return EMPTY_FILTER; }
}
export function saveFilter(f) { localStorage.setItem("ui.doctor.filter", JSON.stringify(f)); }
