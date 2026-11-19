"""Leader buy/sell decoding.

Size comes from the swap event or from the leader's token-balance delta.
The leader's native SOL delta is recorded and then ignored when they paid in
USDC/USDT, in another token, or when a relayer fee payer made their SOL
balance go up.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .pump_logs import (
    STABLES,
    WSOL,
    is_sol_quote,
    leader_events,
    logs_are_interesting,
)

# Native SOL moves smaller than this are fees, tips, or relayer rebates — not a buy.
NATIVE_SOL_DUST = 20_000_000  # 0.02 SOL


@dataclass
class Fill:
    signature: str
    leader: str
    side: str
    mint: str
    token_raw: int
    token_decimals: int
    pre_token_raw: int | None
    payment_mint: str | None
    payment_raw: int
    payment_decimals: int
    payment_kind: str  # sol | stable | token | unknown
    sol_delta_lamports: int = 0
    fee_payer: str | None = None
    relayer: bool = False
    venue: str = "unknown"
    pool: str | None = None
    source: str = "balances"
    venue_quote_raw: int | None = None
    quote_is_sol: bool | None = None
    ts: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def token_ui(self) -> float:
        scale = 10 ** self.token_decimals if self.token_decimals else 1
        return self.token_raw / scale

    @property
    def payment_ui(self) -> float:
        if not self.payment_mint:
            return 0.0
        scale = 10 ** self.payment_decimals if self.payment_decimals else 1
        return self.payment_raw / scale


def payment_sol(fill: Fill, sol_usd: float | None) -> float | None:
    """SOL-equivalent the leader actually paid or received. Never the native SOL delta
    when that delta is a rebate or when a token quote moved."""
    if fill.payment_kind == "sol":
        return fill.payment_ui
    if fill.payment_kind == "stable":
        if sol_usd and sol_usd > 0:
            return fill.payment_ui / sol_usd
        if fill.quote_is_sol and fill.venue_quote_raw:
            return fill.venue_quote_raw / 1e9
        return None
    if fill.quote_is_sol and fill.venue_quote_raw:
        return fill.venue_quote_raw / 1e9
    return None


def needs_fetch(logs: list[str] | None, leader: str, err) -> bool:
    """True only when logs mention a swap program and do not fully describe the leader's fill."""
    if err is not None:
        return False
    logs = logs or []
    events = leader_events(logs, leader)
    if any(ev.get("kind") == "pumpfun" and ev.get("mint") for ev in events):
        # A pump.fun TradeEvent carries mint, side, token amount, and the quote.
        # PumpSwap events in the same tx still lack the mint, but the curve event is enough
        # when it belongs to the leader. Mixed routes fall through to balances via meta
        # when the caller already has it; logs alone are complete for the curve leg.
        if all(ev.get("kind") == "pumpfun" for ev in events):
            return False
    if events and all(ev.get("kind") == "pumpfun" and ev.get("mint") for ev in events):
        return False
    if not logs_are_interesting(logs):
        return False
    return True


def fills_from_logs(logs: list[str], leader: str, signature: str) -> list[Fill]:
    """Complete fills from local events. Empty when the mint is not in the logs."""
    out = []
    for ev in leader_events(logs, leader):
        if ev.get("kind") != "pumpfun" or not ev.get("mint"):
            continue
        out.append(_fill_from_trade_event(ev, leader, signature))
    return out


def _fill_from_trade_event(ev: dict, leader: str, signature: str) -> Fill:
    quote = ev.get("quote_mint")
    if is_sol_quote(quote):
        payment_mint, payment_raw, payment_dec, kind = "SOL", int(ev.get("sol_amount") or 0), 9, "sol"
        quote_is_sol = True
    elif quote in STABLES:
        payment_mint, payment_raw, payment_dec, kind = quote, int(ev.get("quote_amount") or 0), 6, "stable"
        quote_is_sol = False
    else:
        payment_mint, payment_raw, payment_dec, kind = quote, int(ev.get("quote_amount") or 0), 0, "token"
        quote_is_sol = False
    return Fill(
        signature=signature,
        leader=leader,
        side="buy" if ev.get("is_buy") else "sell",
        mint=ev["mint"],
        token_raw=int(ev.get("token_amount") or 0),
        token_decimals=6,
        pre_token_raw=None,
        payment_mint=payment_mint,
        payment_raw=payment_raw,
        payment_decimals=payment_dec,
        payment_kind=kind,
        venue="pumpfun",
        source="logs",
        quote_is_sol=quote_is_sol,
        venue_quote_raw=int(ev.get("sol_amount") or 0) if quote_is_sol else int(ev.get("quote_amount") or 0),
        ts=float(ev.get("timestamp") or 0),
        extra={
            "virtual_sol_reserves": ev.get("virtual_sol_reserves"),
            "virtual_token_reserves": ev.get("virtual_token_reserves"),
            "virtual_quote_reserves": ev.get("virtual_quote_reserves"),
            "quote_mint": quote,
        },
    )


def account_keys(tx: dict) -> list[str]:
    msg = ((tx.get("transaction") or {}).get("message") or {})
    keys = []
    for k in msg.get("accountKeys") or []:
        keys.append(k if isinstance(k, str) else (k.get("pubkey") or ""))
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys.extend(loaded.get("writable") or [])
    keys.extend(loaded.get("readonly") or [])
    return keys


def tx_signers(tx: dict) -> set[str]:
    """Signers of the transaction (jsonParsed ``signer`` flags; json: first numRequiredSignatures keys)."""
    msg = ((tx.get("transaction") or {}).get("message") or {})
    raw = msg.get("accountKeys") or []
    if raw and not isinstance(raw[0], str):
        return {k.get("pubkey") for k in raw if k.get("signer")}
    n = int(((msg.get("header") or {}).get("numRequiredSignatures")) or 0)
    return set(raw[:n])


def _token_map(rows: list | None, leader: str) -> dict[str, dict]:
    acc: dict[str, dict] = {}
    for row in rows or []:
        if row.get("owner") != leader:
            continue
        mint = row.get("mint")
        if not mint:
            continue
        ui = row.get("uiTokenAmount") or {}
        raw = int(ui.get("amount") or 0)
        dec = int(ui.get("decimals") or 0)
        slot = acc.setdefault(mint, {"raw": 0, "decimals": dec})
        slot["raw"] += raw
        if dec:
            slot["decimals"] = dec
    return acc


def _pick_payment(deltas: dict[str, dict], sol_delta: int, *, spent: bool) -> tuple[str | None, int, int, str]:
    """``spent=True`` selects what the leader gave up (a buy). False selects proceeds (a sell)."""
    sign = -1 if spent else 1
    stables = [(m, d) for m, d in deltas.items() if m in STABLES and d["delta"] * sign > 0]
    wsol = [(m, d) for m, d in deltas.items() if m == WSOL and d["delta"] * sign > 0]
    native = (spent and sol_delta <= -NATIVE_SOL_DUST) or ((not spent) and sol_delta >= NATIVE_SOL_DUST)
    wsol_big = bool(wsol) and abs(max(wsol, key=lambda kv: abs(kv[1]["delta"]))[1]["delta"]) >= NATIVE_SOL_DUST
    if stables:
        mint, d = max(stables, key=lambda kv: abs(kv[1]["delta"]))
        # >= 0.1 of the stable unit counts as the payment even when SOL moved the other way
        # (relayer rebate). Dust below that falls through to a real SOL leg.
        meaningful = abs(d["delta"]) >= 10 ** max(int(d["decimals"]) - 1, 0)
        if meaningful or not (native or wsol_big):
            return mint, abs(d["delta"]), d["decimals"] or 6, "stable"
    if wsol_big:
        mint, d = max(wsol, key=lambda kv: abs(kv[1]["delta"]))
        return mint, abs(d["delta"]), 9, "sol"
    if native:
        return "SOL", abs(sol_delta), 9, "sol"
    others = [
        (m, d)
        for m, d in deltas.items()
        if m not in STABLES and m != WSOL and d["delta"] * sign > 0
    ]
    if others:
        mint, d = max(others, key=lambda kv: abs(kv[1]["delta"]))
        return mint, abs(d["delta"]), d["decimals"], "token"
    return None, 0, 0, "unknown"


def fills_from_meta(tx: dict, leader: str, signature: str, events: list[dict] | None = None) -> list[Fill]:
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    # Only the leader's own trades: they must sign (a relayer may pay the fee, but the leader still signs).
    # Unsigned balance changes are airdrops / third-party transfers or pump.fun drips, not leader buys.
    if leader not in tx_signers(tx):
        return []
    keys = account_keys(tx)
    fee_payer = keys[0] if keys else None
    sol_delta = 0
    if leader in keys:
        idx = keys.index(leader)
        pre_b = meta.get("preBalances") or []
        post_b = meta.get("postBalances") or []
        if idx < len(pre_b) and idx < len(post_b):
            sol_delta = int(post_b[idx]) - int(pre_b[idx])
    pre = _token_map(meta.get("preTokenBalances"), leader)
    post = _token_map(meta.get("postTokenBalances"), leader)
    mints = set(pre) | set(post)
    deltas: dict[str, dict] = {}
    for mint in mints:
        pre_raw = int(pre.get(mint, {}).get("raw") or 0)
        post_raw = int(post.get(mint, {}).get("raw") or 0)
        dec = int((post.get(mint) or pre.get(mint) or {}).get("decimals") or 0)
        deltas[mint] = {"delta": post_raw - pre_raw, "pre": pre_raw, "post": post_raw, "decimals": dec}

    fills: list[Fill] = []
    for mint, d in deltas.items():
        if mint in STABLES or mint == WSOL or d["delta"] == 0:
            continue
        side = "buy" if d["delta"] > 0 else "sell"
        pay_mint, pay_raw, pay_dec, kind = _pick_payment(deltas, sol_delta, spent=(side == "buy"))
        # The paired token we just classified as the other leg is not itself the payment
        # of this same mint. _pick_payment may select it; that's correct for the other leg
        # and wrong if it selects this mint. It only looks at the opposite sign, so it won't.
        fills.append(
            Fill(
                signature=signature,
                leader=leader,
                side=side,
                mint=mint,
                token_raw=abs(int(d["delta"])),
                token_decimals=int(d["decimals"] or 6),
                pre_token_raw=int(d["pre"]),
                payment_mint=pay_mint,
                payment_raw=pay_raw,
                payment_decimals=pay_dec,
                payment_kind=kind,
                sol_delta_lamports=sol_delta,
                fee_payer=fee_payer,
                relayer=bool(fee_payer and fee_payer != leader),
                venue="balances",
                source="balances",
            )
        )
    _enrich(fills, events or [])
    return fills


def _enrich(fills: list[Fill], events: list[dict]) -> None:
    for ev in events:
        if ev.get("kind") == "pumpfun":
            for fill in fills:
                if fill.mint == ev.get("mint") and fill.side == ("buy" if ev.get("is_buy") else "sell"):
                    fill.venue = "pumpfun"
                    if ev.get("virtual_token_reserves"):
                        fill.extra["virtual_sol_reserves"] = ev.get("virtual_sol_reserves")
                        fill.extra["virtual_token_reserves"] = ev.get("virtual_token_reserves")
        elif ev.get("kind") == "pumpswap":
            for fill in fills:
                if fill.side != ("buy" if ev.get("is_buy") else "sell"):
                    continue
                base = int(ev.get("base_amount") or 0)
                if base and fill.token_raw and abs(fill.token_raw - base) > max(1, fill.token_raw // 1000):
                    continue
                fill.venue = "pumpswap"
                fill.pool = ev.get("pool")
                fill.venue_quote_raw = int(ev.get("quote_amount") or 0)
                fill.extra["pool_base_reserves"] = ev.get("pool_base_reserves")
                fill.extra["pool_quote_reserves"] = ev.get("pool_quote_reserves")


def decode_activity(leader: str, signature: str, logs: list[str] | None, tx: dict | None) -> list[Fill]:
    events = leader_events(logs or [], leader)
    if tx is not None:
        return fills_from_meta(tx, leader, signature, events)
    return fills_from_logs(logs or [], leader, signature)
