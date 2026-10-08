"""
Custom quote mints on Pump.fun (create_v2): a coin may be paired with USDC, PUMP, a tokenized stock or another pump
coin instead of SOL. On the tape those coins report `sol_amount = 0` / `virtual_sol_reserves = 0` and carry the real
numbers in `quote_amount` / `virtual_quote_reserves` (quote units). ~25 % of trades and ~30 % of creates (2026-10-08).

`QuoteBook` keeps a SOL rate per quote mint (lamports per raw quote unit) — SOL/USDC are fixed math, everything else is
DexScreener price + on-chain decimals, refreshed every 60 s — and `normalize()` rewrites a Create / Trade / curve-state
dict so every downstream consumer keeps working in SOL-equivalents. The original quote fields stay on the dict.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger("quote_mints")

WSOL = "So11111111111111111111111111111111111111112"
DEFAULT_PUBKEY = "11111111111111111111111111111111"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PUMP = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
SOL_QUOTES = {WSOL, DEFAULT_PUBKEY, "", None}
LAMPORTS_PER_SOL = 1_000_000_000
REFRESH_S = 60.0
STALE_S = 600.0            # stop refreshing a quote nobody traded for 10 min
DEX_URL = "https://api.dexscreener.com/tokens/v1/solana/{mints}"
KNOWN_SYMBOLS = {USDC: "USDC", PUMP: "PUMP"}


def is_sol_quote(quote_mint: str | None) -> bool:
    return quote_mint in SOL_QUOTES


class QuoteBook:
    def __init__(self):
        self.rates: dict[str, dict] = {}        # quote_mint → {lamports_per_raw, symbol, decimals, price_usd, ts}
        self.seen: dict[str, float] = {}        # quote_mint → last trade ts
        self.stats = {"converted": 0, "unpriced": 0, "fetches": 0, "fetch_errors": 0}
        self._task: asyncio.Task | None = None
        self._inflight: set[str] = set()

    # ---------- rates ----------
    def lamports_per_raw(self, quote_mint: str | None) -> float | None:
        if is_sol_quote(quote_mint):
            return 1.0
        r = self.rates.get(quote_mint)
        return r["lamports_per_raw"] if r else None

    def symbol(self, quote_mint: str | None) -> str | None:
        if is_sol_quote(quote_mint):
            return None
        r = self.rates.get(quote_mint)
        return (r or {}).get("symbol") or KNOWN_SYMBOLS.get(quote_mint) or (quote_mint[:4] + "…")

    def note(self, quote_mint: str | None) -> None:
        """A quote mint was seen on the tape: make sure it gets priced (and keeps being refreshed)."""
        if is_sol_quote(quote_mint):
            return
        self.seen[quote_mint] = time.time()
        if quote_mint not in self.rates and quote_mint not in self._inflight:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return                                  # sync caller (tests / offline decode): the 60 s loop will pick it up
            self._inflight.add(quote_mint)
            loop.create_task(self._refresh(quote_mint))

    # ---------- normalisation ----------
    def normalize(self, ev: dict) -> dict:
        """Rewrite sol_amount / virtual_sol_reserves / real_sol_reserves from the quote fields for non-SOL coins.
        Adds quote_symbol + quote_rate_sol; leaves SOL coins untouched."""
        q = ev.get("quote_mint")
        if is_sol_quote(q):
            return ev
        self.note(q)
        ev["quote_symbol"] = self.symbol(q)
        rate = self.lamports_per_raw(q)
        if rate is None:
            self.stats["unpriced"] += 1
            ev["quote_priced"] = False
            return ev
        ev["quote_priced"] = True
        ev["quote_rate_sol"] = rate / LAMPORTS_PER_SOL
        for sol_key, quote_key in (("sol_amount", "quote_amount"), ("virtual_sol_reserves", "virtual_quote_reserves"),
                                   ("real_sol_reserves", "real_quote_reserves")):
            if ev.get(quote_key) is not None:
                ev[sol_key] = int(float(ev[quote_key]) * rate)
        self.stats["converted"] += 1
        return ev

    # ---------- pricing ----------
    async def _sol_usd(self) -> float:
        try:
            from solana_client import get_sol_usd_price
            return float(await get_sol_usd_price() or 0.0)
        except Exception:
            return 0.0

    async def _decimals(self, mint: str) -> int | None:
        try:
            from solana_client import rpc_call
            res = await rpc_call("getTokenSupply", [mint])
            return int(res["result"]["value"]["decimals"])
        except Exception as e:
            logger.debug(f"decimals lookup failed for {mint}: {e}")
            return None

    async def _refresh(self, mint: str) -> None:
        self.stats["fetches"] += 1
        try:
            sol_usd = await self._sol_usd()
            if mint == USDC:
                if sol_usd > 0:
                    self.rates[mint] = {"lamports_per_raw": LAMPORTS_PER_SOL / sol_usd / 1e6, "symbol": "USDC", "decimals": 6, "price_usd": 1.0, "ts": time.time()}
                return
            prev = self.rates.get(mint) or {}
            decimals = prev.get("decimals")
            if decimals is None:
                decimals = await self._decimals(mint)
            if decimals is None:
                return
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(DEX_URL.format(mints=mint), headers={"accept": "application/json"})
                pairs = r.json() if r.status_code == 200 else []
            best = None
            for p in pairs or []:
                if (p.get("baseToken") or {}).get("address") != mint:
                    continue
                if best is None or float((p.get("liquidity") or {}).get("usd") or 0) > float((best.get("liquidity") or {}).get("usd") or 0):
                    best = p
            if not best:
                return
            price_sol = None
            if (best.get("quoteToken") or {}).get("address") == WSOL and best.get("priceNative"):
                price_sol = float(best["priceNative"])
            elif best.get("priceUsd") and sol_usd > 0:
                price_sol = float(best["priceUsd"]) / sol_usd
            if not price_sol or price_sol <= 0:
                return
            self.rates[mint] = {"lamports_per_raw": price_sol * LAMPORTS_PER_SOL / (10 ** decimals), "decimals": decimals,
                                "symbol": (best.get("baseToken") or {}).get("symbol") or KNOWN_SYMBOLS.get(mint),
                                "price_usd": float(best.get("priceUsd") or 0) or (price_sol * sol_usd), "ts": time.time()}
        except Exception as e:
            self.stats["fetch_errors"] += 1
            logger.debug(f"quote price refresh failed for {mint}: {e}")
        finally:
            self._inflight.discard(mint)

    def snapshot(self) -> dict:
        return {"quotes": {m: {"symbol": r.get("symbol"), "price_usd": r.get("price_usd"), "age_s": round(time.time() - r["ts"])}
                           for m, r in self.rates.items()}, **self.stats}

    def start(self) -> None:
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._loop())
            except RuntimeError:
                self._task = None                       # constructed outside a loop (tests): started lazily by start_loops

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(REFRESH_S)
            now = time.time()
            for mint, last in list(self.seen.items()):
                if now - last > STALE_S:
                    continue
                if mint not in self._inflight:
                    self._inflight.add(mint)
                    try:
                        await self._refresh(mint)
                    except Exception:
                        self._inflight.discard(mint)


quote_book = QuoteBook()
