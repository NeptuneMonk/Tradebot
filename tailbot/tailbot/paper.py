"""Paper ledger. Debits and credits the SQLite pot. Never builds a transaction."""
from __future__ import annotations

from dataclasses import dataclass

from .sweep import apply_win


@dataclass
class CloseResult:
    pnl_sol: float
    proceeds_sol: float
    cost_sol: float
    fraction: float
    closed: bool
    swept_sol: float
    batched_sol: float | None
    price: float


class PaperBook:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        if db.kv_get("cash_sol") is None:
            db.kv_set("cash_sol", f"{float(cfg.paper_pot_sol):.12f}")
        if db.kv_get("sweep_pending") is None:
            db.kv_set("sweep_pending", "0")
        if db.kv_get("realized_sol") is None:
            db.kv_set("realized_sol", "0")
        if db.kv_get("day_start_equity") is None:
            db.kv_set("day_start_equity", f"{float(cfg.paper_pot_sol):.12f}")
        if db.kv_get("day_started_ts") is None:
            db.kv_set("day_started_ts", "0")

    def cash(self) -> float:
        return float(self.db.kv_get("cash_sol") or 0)

    def set_cash(self, value: float) -> None:
        self.db.kv_set("cash_sol", f"{value:.12f}")

    def deployed(self, leader: str | None = None) -> float:
        total = 0.0
        for pos in self.db.open_positions():
            if leader is None or pos["leader"] == leader:
                total += float(pos["cost_sol"])
        return total

    def pot(self) -> float:
        return self.cash() + self.deployed()

    def pending_sweep(self) -> float:
        return float(self.db.kv_get("sweep_pending") or 0)

    def realized(self) -> float:
        return float(self.db.kv_get("realized_sol") or 0)

    def open_long(
        self,
        *,
        leader: str,
        mint: str,
        tokens_raw: int,
        sol_in: float,
        price: float,
        now: float,
        decimals: int,
        signature: str,
        protocol: str,
        pool: str | None,
        curve: str | None,
        quote_vault: str | None,
        quote_mint: str | None,
        impact_pct: float,
        fee_pct: float,
        slip_pct: float,
        source: str,
    ) -> dict:
        fee = float(self.cfg.paper_tx_fee_sol)
        cost = sol_in + fee
        if self.cash() - cost < -1e-9:
            raise RuntimeError("paper cash underflow")
        self.set_cash(self.cash() - cost)
        existing = self.db.open_position(leader, mint)
        if existing:
            new_tokens = int(existing["tokens_raw"]) + int(tokens_raw)
            new_cost = float(existing["cost_sol"]) + cost
            ui = new_tokens / (10 ** decimals)
            entry = new_cost / ui if ui else price
            peak = max(float(existing["peak_price"]), price, entry)
            self.db.update_position(
                existing["id"], tokens_raw=new_tokens, cost_sol=new_cost, entry_price=entry,
                peak_price=peak, last_price=price,
            )
            pid = existing["id"]
            opened = float(existing["opened_ts"])
        else:
            ui = int(tokens_raw) / (10 ** decimals)
            entry = cost / ui if ui else price
            pid = self.db.insert_position(
                leader=leader, mint=mint, status="open", tokens_raw=int(tokens_raw), cost_sol=cost,
                entry_price=entry, opened_ts=now, peak_price=max(price, entry), last_price=price,
                decimals=decimals, protocol=protocol, pool=pool, curve=curve, quote_vault=quote_vault,
                quote_mint=quote_mint, next_prompt_ts=now + float(self.cfg.hold_prompt_s), prompted=0,
                created_sig=signature,
            )
            opened = now
        self.db.add_fill(
            ts=now, position_id=pid, side="buy", sol_amount=sol_in, tokens_raw=int(tokens_raw),
            price=price, impact_pct=impact_pct, fee_pct=fee_pct, slip_pct=slip_pct, fee_sol=fee,
            source=source, reason="copy", leader_sig=signature,
        )
        pos = self.db.position(pid)
        pos["opened_ts"] = opened
        return pos

    def close_fraction(self, pos: dict, fraction: float, sol_out: float, price: float, reason: str, now: float, **quote_meta) -> CloseResult:
        fraction = max(0.0, min(1.0, float(fraction)))
        fee = float(self.cfg.paper_tx_fee_sol)
        proceeds = max(0.0, sol_out - fee)
        cost = float(pos["cost_sol"]) * fraction
        pnl = proceeds - cost
        self.set_cash(self.cash() + proceeds)
        tokens = int(pos["tokens_raw"])
        sold = int(round(tokens * fraction))
        left = max(0, tokens - sold)
        closed = fraction >= 0.999 or left <= 0
        if closed:
            left = 0
            cost_left = 0.0
            status = "closed"
        else:
            cost_left = float(pos["cost_sol"]) - cost
            status = "open"
        self.db.update_position(pos["id"], tokens_raw=left, cost_sol=cost_left, status=status, last_price=price or pos["last_price"])
        self.db.add_fill(
            ts=now, position_id=pos["id"], side="sell", sol_amount=proceeds, tokens_raw=sold,
            price=price, impact_pct=quote_meta.get("impact_pct") or 0.0,
            fee_pct=quote_meta.get("fee_pct") or 0.0, slip_pct=quote_meta.get("slip_pct") or 0.0,
            fee_sol=fee, source=quote_meta.get("source") or "", reason=reason, leader_sig=pos.get("created_sig"),
        )
        self.db.add_close(now, pos["leader"], pos["mint"], pos["id"], pnl, cost, reason)
        self.db.kv_set("realized_sol", f"{self.realized() + pnl:.12f}")
        batched = None
        taken = 0.0
        if pnl > 0:
            applied = apply_win(self.pending_sweep(), pnl, float(self.cfg.sweep_profit_pct), float(self.cfg.sweep_min_sol))
            self.set_cash(self.cash() - applied.taken_sol)
            self.db.kv_set("sweep_pending", f"{applied.pending_sol:.12f}")
            taken = applied.taken_sol
            if applied.batched_sol:
                self.db.add_sweep(now, applied.batched_sol, self.cfg.sweep_wallet, pos["id"])
                batched = applied.batched_sol
        return CloseResult(pnl, proceeds, cost, fraction, closed or left == 0, taken, batched, price)
