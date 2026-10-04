"""Outsourced dev-reputation adapter (cut-the-fat Phase 3b). Dark until REPUTATION_BASE_URL is set.

reputation.family (2026-10): the site is a JS SPA but ships an undocumented JSON API — `/api/token/{mint}` returns
`dev.rank` (CRAZY/PROVEN/GOOD/UNKNOWN/FARMER) and a top-level `fake` bool (+ `fakeReason`); 404 → unknown mint.
Working config: REPUTATION_BASE_URL=https://reputation.family/api/token/{mint}  REPUTATION_TIER_PATH=dev.rank  REPUTATION_FAKE_CHART_PATH=fake

Env:
  REPUTATION_BASE_URL   e.g. https://api.example/rep/{mint}  — `{mint}` / `{creator}` placeholders; no placeholder → `/{mint}` appended
  REPUTATION_API_KEY    sent as `Authorization: Bearer …` and `x-api-key` (optional)
  REPUTATION_TIER_PATH  dotted JSON path to the tier string (optional; auto-detects tier/rank/label/reputation keys)
  REPUTATION_FAKE_CHART_PATH  dotted JSON path to the fake-chart flag (optional; auto-detects fake_chart/fakeChart)

Tiers normalise to CRAZY | PROVEN | GOOD | FARMER | UNKNOWN. Gate rules (bot._enter_impl):
  FARMER or fake-chart → skip on every book (master gate; operator manual buys exempt)
  hunt book            → requires CRAZY / PROVEN / GOOD; UNKNOWN, miss, timeout → skip (no local fallback, by design)
One HTTP lookup per mint: positive cache 15 min, negative cache 60 s."""
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TIERS = ("CRAZY", "PROVEN", "GOOD", "FARMER")
HUNT_ALLOW = {"CRAZY", "PROVEN", "GOOD"}
POS_TTL_S = 15 * 60
NEG_TTL_S = 60
TIMEOUT_S = 2.5
_TIER_KEYS = ("tier", "rank", "label", "reputation", "class", "grade")
_FAKE_KEYS = ("fake_chart", "fakeChart", "fake", "is_fake_chart")


def configured() -> bool:
    return bool(os.environ.get("REPUTATION_BASE_URL", "").strip())


def _dig(doc: Any, path: str) -> Any:
    cur = doc
    for part in [p for p in path.split(".") if p]:
        if isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)] if int(part) < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _find_key(doc: Any, keys: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 4 or not isinstance(doc, dict):
        return None
    for k in keys:
        if k in doc and doc[k] is not None:
            return doc[k]
    for v in doc.values():
        if isinstance(v, dict):
            hit = _find_key(v, keys, depth + 1)
            if hit is not None:
                return hit
    return None


def normalize_tier(raw: Any) -> str:
    s = str(raw or "").strip().upper()
    for t in TIERS:
        if t in s:
            return t
    return "UNKNOWN"


def parse(doc: Any) -> dict:
    tier_raw = _dig(doc, os.environ["REPUTATION_TIER_PATH"]) if os.environ.get("REPUTATION_TIER_PATH") else _find_key(doc, _TIER_KEYS)
    fake_raw = _dig(doc, os.environ["REPUTATION_FAKE_CHART_PATH"]) if os.environ.get("REPUTATION_FAKE_CHART_PATH") else _find_key(doc, _FAKE_KEYS)
    fake = str(fake_raw).strip().lower() in ("1", "true", "yes") if fake_raw is not None else False
    return {"tier": normalize_tier(tier_raw), "fake_chart": fake, "raw_tier": tier_raw}


def _url(mint: str, creator: str | None) -> str:
    base = os.environ.get("REPUTATION_BASE_URL", "").strip()
    if "{mint}" in base or "{creator}" in base:
        return base.replace("{mint}", mint).replace("{creator}", creator or "")
    return f"{base.rstrip('/')}/{mint}"


class ReputationClient:
    def __init__(self):
        self._cache: dict[str, tuple[float, dict]] = {}
        self.stats = {"lookups": 0, "hits": 0, "misses": 0, "errors": 0, "skips_farmer": 0, "skips_hunt": 0}

    async def lookup(self, mint: str, creator: str | None = None, fresh: bool = False) -> dict:
        """Returns {"tier", "fake_chart", "ok"}; ok=False on miss / error / timeout (cached NEG_TTL_S).
        `fresh=True` re-queries past a cached miss (the site indexes a launch a second or two after creation)."""
        now = time.time()
        hit = self._cache.get(mint)
        if hit and hit[0] > now and not (fresh and not hit[1].get("ok")):
            self.stats["hits"] += 1
            return hit[1]
        self.stats["lookups"] += 1
        headers = {"Accept": "application/json", "User-Agent": "pump.bot/1.0 (+reputation adapter; one lookup per mint, cached 15m)",
                   "Referer": "https://reputation.family/"}
        key = os.environ.get("REPUTATION_API_KEY", "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
            headers["x-api-key"] = key
        result = {"tier": "UNKNOWN", "fake_chart": False, "ok": False}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as c:
                r = await c.get(_url(mint, creator), headers=headers)
            if r.status_code == 200:
                parsed = parse(r.json())
                result = {**parsed, "ok": parsed["tier"] != "UNKNOWN" or parsed["fake_chart"]}
            else:
                self.stats["misses"] += 1
        except Exception as e:
            self.stats["errors"] += 1
            logger.debug(f"reputation lookup {mint[:8]}…: {e}")
        self._cache[mint] = (now + (POS_TTL_S if result["ok"] else NEG_TTL_S), result)
        if len(self._cache) > 2000:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:500]:
                self._cache.pop(k, None)
        return result

    def gate(self, rep: dict, book: str) -> str | None:
        """Skip reason or None. FARMER / fake-chart block every book; hunt needs an allow-tier."""
        if rep.get("tier") == "FARMER" or rep.get("fake_chart"):
            self.stats["skips_farmer"] += 1
            return f"reputation:{'fake-chart' if rep.get('fake_chart') and rep.get('tier') != 'FARMER' else 'FARMER'}"
        if book == "hunt" and rep.get("tier") not in HUNT_ALLOW:
            self.stats["skips_hunt"] += 1
            return f"reputation:{rep.get('tier') or 'UNKNOWN'}"
        return None

    @staticmethod
    def is_crazy(rep: dict) -> bool:
        """Dev-watch trigger: CRAZY rank with no fake-chart flag."""
        return bool(rep.get("ok")) and rep.get("tier") == "CRAZY" and not rep.get("fake_chart")

    def snapshot(self) -> dict:
        return {"configured": configured(), "url_set": bool(os.environ.get("REPUTATION_BASE_URL")), "key_set": bool(os.environ.get("REPUTATION_API_KEY")),
                "cached": len(self._cache), **self.stats}
