"""Always-on paper copy loop. One task watches leaders, one marks and exits, one polls Telegram."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .chase import run_chase
from .config import set_param
from .decode import Fill, decode_activity, fills_from_logs, needs_fetch, payment_sol
from .exits import decide_exit, pnl_pct
from .market import price_sol_per_ui
from .momentum import buy_flow_slow
from .pump_logs import STABLES, WSOL, decode_bonding_curve, token_account_amount
from .risk import BuyView, daily_loss_hit, evaluate_buy, is_flip, leader_auto_off, proportional_fraction
from .rpc import CreditsBlocked
from .safety import MintSafety, parse_mint
from .telegram_bot import parse_command

log = logging.getLogger("tailbot.engine")
CHI = ZoneInfo("America/Chicago")


class Engine:
    def __init__(self, cfg, db, paper, quotes, meter, rpc=None, alerter=None, market_loader=None, safety_loader=None, sol_usd_fn=None, feed_down_fn=None):
        self.cfg = cfg
        self.db = db
        self.paper = paper
        self.quotes = quotes
        self.meter = meter
        self.rpc = rpc
        self.alerter = alerter
        self.market_loader = market_loader
        self.safety_loader = safety_loader
        self.sol_usd_fn = sol_usd_fn
        self.feed_down_fn = feed_down_fn
        self.ws = None
        self.lock = asyncio.Lock()
        self.flow: dict[str, list] = {}
        self._quote_raw: dict[str, int] = {}
        self.watches: dict[str, dict] = {}
        self._chasing: set[tuple[str, str]] = set()
        self._sol_usd = 0.0
        self._sol_usd_at = 0.0
        self._last_mark = 0.0
        self._feed_alerted = False
        self._stop = False
        self.now = __import__("time").time
        self.sleep = asyncio.sleep

    def stop(self) -> None:
        self._stop = True

    async def alert(self, text: str, markup: dict | None = None) -> None:
        log.info("%s", text.replace("\n", " | "))
        if self.alerter is not None:
            await self.alerter.send(text, markup)

    def _day(self) -> str:
        return datetime.fromtimestamp(self.now(), CHI).date().isoformat()

    def _feed_down(self) -> bool:
        if self.feed_down_fn is not None:
            return bool(self.feed_down_fn())
        if self.ws is None:
            return False
        return self.ws.feed_is_down(self.now(), float(self.cfg.feed_down_s))

    def _leader_on(self, wallet: str) -> bool:
        if wallet not in self.cfg.leaders:
            return False
        return not (self.db.kv_get(f"off:{wallet}") or "").strip()

    def _daily_paused(self) -> bool:
        if self.db.kv_get("daily_override") == self._day():
            return False
        start = float(self.db.kv_get("day_start_equity") or 0)
        return daily_loss_hit(self.paper.realized(), start, float(self.cfg.daily_loss_pct))

    async def _sol(self) -> float:
        if self._sol_usd > 0 and self.now() - self._sol_usd_at < 60:
            return self._sol_usd
        if self.sol_usd_fn is None:
            return self._sol_usd
        try:
            price = float(await self.sol_usd_fn() or 0)
        except Exception:
            price = self._sol_usd
        if price > 0:
            self._sol_usd = price
            self._sol_usd_at = self.now()
        return self._sol_usd

    async def run(self) -> None:
        self._ensure_day()
        while not self._stop:
            try:
                if self.now() - self._last_mark >= float(self.cfg.mark_every_s):
                    await self._refresh_marks()
                    self._last_mark = self.now()
                await self.check_exits()
                await self._maybe_roll_day()
                self._flush_credits()
                self._sync_accounts()
                await self._feed_edge()
            except Exception:
                log.exception("monitor tick failed")
            await self.sleep(2)

    def _ensure_day(self) -> None:
        if self.db.kv_get("day_id") is None:
            self.db.kv_set("day_id", self._day())
            self.db.kv_set("day_started_ts", str(self.now()))
            self.db.kv_set("day_start_equity", f"{self.paper.pot():.12f}")

    async def _maybe_roll_day(self) -> None:
        today = self._day()
        stored = self.db.kv_get("day_id")
        if not stored or stored == today:
            if not stored:
                self._ensure_day()
            return
        start = float(self.db.kv_get("day_started_ts") or 0)
        closes = self.db.closes_since(start)
        sweeps = self.db.sweeps_since(start)
        pnl = sum(float(c["pnl_sol"] or 0) for c in closes)
        wins = sum(1 for c in closes if float(c["pnl_sol"] or 0) > 0)
        swept = sum(float(s["sol_amount"] or 0) for s in sweeps)
        credits = self.meter.total if self.meter else 0
        await self.alert(
            f"DAILY {stored} pnl {pnl:+.4f} SOL · {len(closes)} closes ({wins} wins) · swept {swept:.4f} SOL · credits {credits:.0f}"
        )
        self.db.kv_set("day_id", today)
        self.db.kv_set("day_started_ts", str(self.now()))
        self.db.kv_set("day_start_equity", f"{self.paper.pot():.12f}")
        self.db.kv_set("realized_sol", "0")
        self.db.kv_set("daily_override", "")
        if self.meter:
            self.meter.rpc = 0.0
            self.meter.ws_bytes = 0
            self.db.save_credits(today, 0, 0)

    def _flush_credits(self) -> None:
        if self.meter:
            self.db.save_credits(self._day(), self.meter.rpc, self.meter.ws_bytes)

    async def _feed_edge(self) -> None:
        down = self._feed_down()
        if down and not self._feed_alerted:
            self._feed_alerted = True
            await self.alert(f"feed down > {self.cfg.feed_down_s:g}s — new buys paused")
        elif not down and self._feed_alerted:
            self._feed_alerted = False
            await self.alert("feed restored")

    async def on_logs(self, leader: str, value: dict) -> None:
        if leader not in self.cfg.leaders:
            return
        err = value.get("err")
        sig = value.get("signature") or ""
        logs = value.get("logs") or []
        if not sig or self.db.seen(sig, leader):
            return
        if err is not None or not needs_fetch(logs, leader, err):
            self.db.mark_seen(sig, leader, self.now())
            if err is None:
                for fill in fills_from_logs(logs, leader, sig):
                    fill.ts = self.now()
                    await self.on_leader_fill(fill)
            return
        tx = await self._fetch_tx(sig)
        self.db.mark_seen(sig, leader, self.now())
        if not tx:
            return
        for fill in decode_activity(leader, sig, logs, tx):
            fill.ts = self.now()
            await self.on_leader_fill(fill)

    async def _fetch_tx(self, sig: str):
        if self.rpc is None:
            return None
        for _ in range(2):
            try:
                tx = await self.rpc.get_transaction(sig)
            except CreditsBlocked:
                await self.alert("SKIP helius credit ceiling — leader transaction not fetched")
                return None
            if tx:
                return tx
            await self.sleep(2)
        return None

    async def on_leader_fill(self, fill: Fill) -> None:
        if not fill.ts:
            fill.ts = self.now()
        async with self.lock:
            self.db.add_leader_trade(fill, fill.ts)
        if fill.side == "sell":
            pos = self.db.open_position(fill.leader, fill.mint)
            if pos:
                fraction = proportional_fraction(fill.token_raw, fill.pre_token_raw)
                await self._exit_position(pos, fraction, "leader sold")
            return
        if not self._leader_on(fill.leader):
            await self._skip(fill, "leader off")
            return
        await self._sol()
        leader_px = await self._leader_price(fill)
        market = await self._market(fill)
        safety = await self._safety(fill.mint)
        if safety.reason in ("mint account missing", "mint account too short", "helius credit ceiling"):
            await self._skip(fill, safety.reason)
            return
        skip_msg = None
        sol_in = None
        async with self.lock:
            decision = evaluate_buy(self._view(fill, leader_px, market, safety), self.cfg)
            if not decision.ok:
                skip_msg = self._record_skip(fill, decision.reason)
            else:
                sol_in = decision.sol_in
                self._chasing.add((fill.leader, fill.mint))
        if skip_msg:
            await self.alert(skip_msg)
            return
        key = (fill.leader, fill.mint)
        quoted_for = sol_in

        async def quote_fn():
            return await self.quotes.buy(fill.mint, quoted_for, market)

        result = await run_chase(
            leader_price=leader_px or 0.0,
            quote_fn=quote_fn,
            premium_pct=float(self.cfg.chase_premium_pct),
            recheck_s=float(self.cfg.chase_recheck_s),
            window_s=float(self.cfg.chase_window_s),
            sleep=self.sleep,
            now=self.now,
        )
        opened = None
        quote = result.quote
        async with self.lock:
            self._chasing.discard(key)
            if not result.ok or quote is None or not leader_px:
                skip_msg = self._record_skip(fill, result.reason or "no quote")
            else:
                decision = evaluate_buy(self._view(fill, leader_px, market, safety), self.cfg)
                if not decision.ok:
                    skip_msg = self._record_skip(fill, decision.reason)
                else:
                    sol_in = decision.sol_in
                    if abs(decision.sol_in - quoted_for) / max(quoted_for, 1e-9) > 0.05:
                        quote = None  # size moved; re-quote outside the lock
                    else:
                        opened = self._open(fill, decision.sol_in, quote, market)
        if quote is None and not skip_msg and result.ok:
            quote = await self.quotes.buy(fill.mint, sol_in, market)
            async with self.lock:
                if quote is None or quote.price_sol_per_ui <= 0:
                    skip_msg = self._record_skip(fill, "no quote")
                else:
                    premium = (quote.price_sol_per_ui / leader_px - 1.0) * 100.0
                    if premium > float(self.cfg.chase_premium_pct):
                        skip_msg = self._record_skip(fill, f"quote {premium:.1f}% above leader fill (max {self.cfg.chase_premium_pct:g}%)")
                    else:
                        decision = evaluate_buy(self._view(fill, leader_px, market, safety), self.cfg)
                        if not decision.ok:
                            skip_msg = self._record_skip(fill, decision.reason)
                        else:
                            opened = self._open(fill, decision.sol_in, quote, market)
                            sol_in = decision.sol_in
                            result.premium_pct = premium
        if skip_msg:
            await self.alert(skip_msg)
            return
        if opened and quote:
            premium = result.premium_pct or 0.0
            paid = f"{fill.payment_ui:.4g} {fill.payment_mint or '?'}"
            await self.alert(
                f"COPY buy {fill.leader[:4]}… {fill.mint[:6]}… {sol_in:.4f} SOL "
                f"@ {quote.price_sol_per_ui:.8g} ({premium:+.1f}% vs leader) via {quote.source}\n"
                f"leader paid {paid} · impact {quote.impact_pct:.2f}% · fee {quote.fee_pct:.2f}% · slip {quote.slip_pct:.2f}%"
            )
            self._sync_accounts()

    def _open(self, fill, sol_in, quote, market):
        return self.paper.open_long(
            leader=fill.leader, mint=fill.mint, tokens_raw=int(quote.tokens_raw), sol_in=sol_in,
            price=quote.price_sol_per_ui, now=self.now(), decimals=int(quote.decimals or fill.token_decimals or 6),
            signature=fill.signature, protocol=getattr(market, "protocol", None) or "unknown",
            pool=fill.pool or getattr(market, "pool", None), curve=getattr(market, "curve", None),
            quote_vault=getattr(market, "quote_vault", None), quote_mint=getattr(market, "quote_mint", None),
            impact_pct=quote.impact_pct, fee_pct=quote.fee_pct, slip_pct=quote.slip_pct, source=quote.source,
        )

    def _record_skip(self, fill, reason: str) -> str:
        self.db.add_decision(self.now(), fill.signature, fill.leader, fill.mint, "skip", reason)
        return f"SKIP {fill.leader[:4]}… {fill.mint[:6]}… {reason}"

    async def _skip(self, fill, reason: str) -> None:
        async with self.lock:
            text = self._record_skip(fill, reason)
        await self.alert(text)

    async def _leader_price(self, fill: Fill) -> float | None:
        sol = payment_sol(fill, self._sol_usd)
        if sol is None and fill.payment_kind == "token" and fill.payment_mint and self.quotes is not None:
            try:
                sol = await self.quotes.sol_out_for_token(fill.payment_mint, fill.payment_raw, fill.payment_decimals or 6)
            except Exception:
                sol = None
        if not sol or fill.token_ui <= 0:
            return None
        return sol / fill.token_ui

    async def _market(self, fill: Fill):
        if self.market_loader is not None:
            return await self.market_loader(fill.mint, fill.pool)
        from .market import load_market
        if self.rpc is None:
            return None
        return await load_market(self.rpc, fill.mint, self._sol_usd, fill.pool)

    async def _safety(self, mint: str) -> MintSafety:
        if self.safety_loader is not None:
            return await self.safety_loader(mint)
        cached = self.db.mint_safety(mint)
        if cached:
            freeze = bool(cached["freeze"])
            fee = bool(cached["transfer_fee"])
            return MintSafety(freeze, fee, int(cached["decimals"] or 6), not (freeze or fee))
        if self.rpc is None:
            return MintSafety(False, False, 6, False, "mint account missing")
        try:
            acct = await self.rpc.get_account(mint, kind="entry")
        except CreditsBlocked:
            return MintSafety(False, False, 6, False, "helius credit ceiling")
        if acct is None:
            return MintSafety(False, False, 6, False, "mint account missing")
        parsed = parse_mint(acct.data)
        self.db.save_mint(mint, parsed.freeze, parsed.transfer_fee, parsed.decimals, self.now())
        return parsed

    def _view(self, fill, leader_px, market, safety) -> BuyView:
        opens = [p for p in self.db.open_positions() if p["leader"] == fill.leader]
        leader_sol = (leader_px * fill.token_ui) if leader_px and fill.token_ui else None
        sells = self.db.recent_sell_times(fill.leader, fill.mint, self.now() - float(self.cfg.flip_window_s))
        return BuyView(
            pot_sol=self.paper.pot(),
            cash_sol=self.paper.cash(),
            deployed_sol=self.paper.deployed(),
            leader_deployed_sol=self.paper.deployed(fill.leader),
            leader_open_positions=len(opens),
            already_open_this_mint=any(p["mint"] == fill.mint for p in opens),
            leader_buy_sol=leader_sol,
            mcap_usd=getattr(market, "mcap_usd", None),
            protocol=getattr(market, "protocol", None) or "unknown",
            liquidity_usd=getattr(market, "liquidity_usd", None),
            freeze=bool(getattr(safety, "freeze", False)),
            transfer_fee=bool(getattr(safety, "transfer_fee", False)),
            flipped=is_flip(sells, self.now(), float(self.cfg.flip_window_s)),
            feed_down=self._feed_down(),
            paused_manual=self.db.kv_get("manual_pause") == "1",
            paused_daily=self._daily_paused(),
            paused_credits=bool(self.meter and self.meter.buys_paused()),
            leader_enabled=self._leader_on(fill.leader),
            tx_fee_sol=float(self.cfg.paper_tx_fee_sol),
        )

    async def _exit_position(self, pos: dict, fraction: float, reason: str) -> None:
        fresh = self.db.position(pos["id"])
        if not fresh or fresh["status"] != "open":
            return
        tokens = int(round(int(fresh["tokens_raw"]) * fraction))
        if tokens <= 0:
            return
        quote = None
        try:
            quote = await self.quotes.sell(fresh["mint"], tokens, None, fresh["decimals"])
        except Exception:
            log.exception("sell quote failed")
        if quote is not None and quote.sol_out > 0:
            sol_out = quote.sol_out
            price = quote.price_sol_per_ui
            meta = {"impact_pct": quote.impact_pct, "fee_pct": quote.fee_pct, "slip_pct": quote.slip_pct, "source": quote.source}
        else:
            ui = tokens / (10 ** int(fresh["decimals"] or 6))
            price = float(fresh["last_price"] or fresh["entry_price"] or 0)
            sol_out = ui * price * (10_000 - int(self.cfg.paper_extra_slip_bps)) / 10_000
            meta = {"source": "mark", "slip_pct": self.cfg.paper_extra_slip_bps / 100.0}
        async with self.lock:
            fresh = self.db.position(pos["id"])
            if not fresh or fresh["status"] != "open":
                return
            result = self.paper.close_fraction(fresh, fraction, sol_out, price, reason, self.now(), **meta)
            off = self._auto_off(fresh["leader"])
            if off:
                self.db.kv_set(f"off:{fresh['leader']}", off)
        pct = pnl_pct(price, float(fresh["entry_price"])) if price else 0
        await self.alert(
            f"EXIT {reason} {fresh['mint'][:6]}… {fraction:.0%} pnl {result.pnl_sol:+.4f} SOL ({pct:+.1f}%) via {meta.get('source')}"
        )
        if off:
            await self.alert(f"LEADER OFF {fresh['leader'][:4]}… {off}")
        if result.batched_sol:
            await self.alert(f"SWEEP {result.batched_sol:.4f} SOL → {self.cfg.sweep_wallet} (paper)")
        self._sync_accounts()

    def _auto_off(self, leader: str) -> str | None:
        if not self._leader_on(leader):
            return None
        reset = float(self.db.kv_get(f"reset:{leader}") or 0)
        closes = [(float(c["pnl_sol"]), float(c["cost_sol"])) for c in self.db.closes_for(leader, reset)]
        return leader_auto_off(
            closes,
            streak=int(self.cfg.leader_loss_streak),
            window=int(self.cfg.leader_window_trades),
            window_pnl_pct=float(self.cfg.leader_window_pnl_pct),
        )

    async def check_exits(self) -> None:
        for pos in self.db.open_positions():
            slow = buy_flow_slow(
                self.flow.get(pos["mint"], []), self.now(),
                short_s=float(self.cfg.momentum_short_s),
                baseline_s=float(self.cfg.momentum_baseline_s),
                drop_ratio=float(self.cfg.momentum_drop_ratio),
                min_baseline_sol=float(self.cfg.momentum_min_baseline_sol),
            )
            decision = decide_exit(
                entry=float(pos["entry_price"]), last=float(pos["last_price"]), peak=float(pos["peak_price"]),
                now=self.now(), next_prompt_ts=float(pos["next_prompt_ts"] or 0) or None,
                momentum_slow=slow, cfg=self.cfg,
            )
            if decision.kind == "prompt" and not pos["prompted"]:
                self.db.update_position(pos["id"], prompted=1, next_prompt_ts=0)
                pct = pnl_pct(float(pos["last_price"]), float(pos["entry_price"]))
                await self.alert(
                    f"24h {pos['mint'][:6]}… {pct:+.1f}% — Exit or Hold? No auto-exit.",
                    {"inline_keyboard": [[
                        {"text": "Exit", "callback_data": f"x:{pos['id']}"},
                        {"text": "Hold", "callback_data": f"h:{pos['id']}"},
                    ]]},
                )
            elif decision.kind == "exit":
                await self._exit_position(pos, 1.0, decision.reason)

    async def _refresh_marks(self) -> None:
        await self._sol()
        for pos in list(self.db.open_positions()):
            try:
                quote = await self.quotes.sell(pos["mint"], int(pos["tokens_raw"]), None, pos["decimals"])
            except Exception:
                continue
            if quote and quote.price_sol_per_ui > 0:
                self._mark(pos["mint"], quote.price_sol_per_ui)

    def _mark(self, mint: str, price: float) -> None:
        for pos in self.db.open_positions():
            if pos["mint"] != mint:
                continue
            peak = max(float(pos["peak_price"]), price)
            self.db.update_position(pos["id"], last_price=price, peak_price=peak)

    async def on_account(self, pubkey: str, data: bytes) -> None:
        meta = self.watches.get(pubkey)
        if not meta:
            return
        if meta["kind"] == "curve":
            state = decode_bonding_curve(data)
            if not state:
                return
            qmint = state["quote_mint"] or meta.get("quote_mint") or WSOL
            qdec = 6 if qmint in STABLES else 9
            quote_raw = state["virtual_quote_reserves"] if qmint in STABLES else state["virtual_sol_reserves"]
            self._on_reserves(meta["mint"], quote_raw, state["virtual_token_reserves"], qmint, qdec, int(meta["decimals"]))
        else:
            amount = token_account_amount(data)
            if amount is None:
                return
            self._on_quote_only(meta["mint"], amount, meta["quote_mint"], int(meta["quote_decimals"]))

    def _on_reserves(self, mint, quote_raw, base_raw, quote_mint, quote_decimals, token_decimals) -> None:
        qusd = 1.0 if quote_mint in STABLES else self._sol_usd
        if quote_mint in (WSOL, "SOL") or quote_mint not in STABLES:
            if quote_mint not in STABLES:
                qusd = self._sol_usd
        price = price_sol_per_ui(quote_raw, base_raw, token_decimals, quote_decimals, qusd or 0, self._sol_usd or 0) if qusd else None
        if price:
            self._mark(mint, price)
        self._flow_delta(mint, quote_raw, quote_mint, quote_decimals)

    def _on_quote_only(self, mint, quote_raw, quote_mint, quote_decimals) -> None:
        self._flow_delta(mint, quote_raw, quote_mint, quote_decimals)

    def _flow_delta(self, mint, quote_raw, quote_mint, quote_decimals) -> None:
        prev = self._quote_raw.get(mint)
        self._quote_raw[mint] = int(quote_raw)
        if prev is None or prev == quote_raw:
            return
        delta = int(quote_raw) - prev
        ui = abs(delta) / (10 ** quote_decimals)
        if quote_mint in STABLES:
            if self._sol_usd <= 0:
                return
            sol = ui / self._sol_usd
        else:
            sol = ui
        self.flow.setdefault(mint, []).append((self.now(), "buy" if delta > 0 else "sell", sol))
        cutoff = self.now() - float(self.cfg.momentum_baseline_s) * 2
        self.flow[mint] = [ev for ev in self.flow[mint] if ev[0] >= cutoff]

    def _sync_accounts(self) -> None:
        if self.ws is None:
            return
        if self.meter and self.meter.buys_paused():
            self.ws.set_accounts(set())
            return
        accounts: set[str] = set()
        self.watches = {}
        for pos in self.db.open_positions():
            if pos.get("curve") and pos.get("protocol") == "pumpfun":
                accounts.add(pos["curve"])
                self.watches[pos["curve"]] = {"mint": pos["mint"], "kind": "curve", "quote_mint": pos.get("quote_mint") or WSOL, "decimals": pos["decimals"]}
            elif pos.get("quote_vault"):
                accounts.add(pos["quote_vault"])
                qmint = pos.get("quote_mint") or WSOL
                self.watches[pos["quote_vault"]] = {
                    "mint": pos["mint"], "kind": "vault", "quote_mint": qmint,
                    "quote_decimals": 6 if qmint in STABLES else 9, "decimals": pos["decimals"],
                }
        self.ws.set_accounts(accounts)

    async def on_command(self, text: str) -> str:
        parsed = parse_command(text)
        if not parsed:
            return "commands: /status /positions /pause /resume /sellall <PIN> /leaders /leader on|off <wallet> /override daily /set <key> <value>"
        cmd, args = parsed
        if cmd == "/status":
            return self._status_text()
        if cmd == "/positions":
            return self._positions_text()
        if cmd == "/pause":
            self.db.kv_set("manual_pause", "1")
            return "paused — new buys off, exits still run"
        if cmd == "/resume":
            self.db.kv_set("manual_pause", "0")
            if self._daily_paused():
                return "manual pause cleared. daily loss pause still on — /override daily"
            return "resumed"
        if cmd == "/sellall":
            if len(args) != 1:
                return "usage: /sellall <PIN>"
            if args[0] != str(self.cfg.sellall_pin):
                return "bad pin"
            positions = self.db.open_positions()
            for pos in positions:
                await self._exit_position(pos, 1.0, "manual")
            return f"sold {len(positions)} position(s) (paper)"
        if cmd == "/leaders":
            return self._leaders_text()
        if cmd == "/leader":
            return self._cmd_leader(args)
        if cmd == "/override":
            if args != ["daily"]:
                return "usage: /override daily"
            self.db.kv_set("daily_override", self._day())
            return "daily loss pause overridden until midnight America/Chicago"
        if cmd == "/set":
            if len(args) != 2:
                return "usage: /set <key> <value>"
            return set_param(self.cfg, args[0], args[1], self.db.kv_set)
        return "unknown command. /status"

    async def on_callback(self, data: str) -> str:
        try:
            pid = int(data.split(":", 1)[1])
        except (IndexError, ValueError):
            return "ignored"
        pos = self.db.position(pid)
        if data.startswith("x:"):
            if not pos or pos["status"] != "open":
                return "already closed"
            await self._exit_position(pos, 1.0, "manual")
            return "exit sent"
        if data.startswith("h:"):
            if not pos or pos["status"] != "open":
                return "already closed"
            self.db.update_position(pos["id"], prompted=0, next_prompt_ts=self.now() + float(self.cfg.hold_prompt_s))
            return "holding another 24h"
        return "ignored"

    def _status_text(self) -> str:
        pot = self.paper.pot()
        deployed = self.paper.deployed()
        flags = []
        if self.db.kv_get("manual_pause") == "1":
            flags.append("paused")
        if self._daily_paused():
            flags.append("daily-loss")
        if self._feed_down():
            flags.append("feed-down")
        if self.meter and self.meter.buys_paused():
            flags.append("credit-ceiling")
        credits = f"{self.meter.total:.0f}/{self.cfg.helius_soft_ceiling:.0f}" if self.meter else "n/a"
        return (
            f"PAPER pot {pot:.4f} SOL · cash {self.paper.cash():.4f} · deployed {deployed:.4f} "
            f"({(deployed / pot * 100) if pot else 0:.0f}%) · sweep pending {self.paper.pending_sweep():.4f}\n"
            f"buys: {', '.join(flags) if flags else 'open'} · realized today {self.paper.realized():+.4f} SOL\n"
            f"positions {len(self.db.open_positions())} · leaders {sum(1 for w in self.cfg.leaders if self._leader_on(w))}/{len(self.cfg.leaders)}\n"
            f"helius credits today {credits} · feed {'down' if self._feed_down() else 'up'}"
        )

    def _positions_text(self) -> str:
        rows = self.db.open_positions()
        if not rows:
            return "no open positions"
        lines = []
        for pos in rows:
            pct = pnl_pct(float(pos["last_price"]), float(pos["entry_price"]))
            ui = int(pos["tokens_raw"]) / (10 ** int(pos["decimals"] or 6))
            lines.append(
                f"{pos['mint'][:6]}… {pct:+.1f}% · cost {pos['cost_sol']:.4f} SOL · {ui:.4g} tok · {pos['leader'][:4]}… · {pos['protocol']}"
            )
        return "\n".join(lines)

    def _leaders_text(self) -> str:
        if not self.cfg.leaders:
            return "no leaders configured"
        lines = []
        for wallet in self.cfg.leaders:
            reason = (self.db.kv_get(f"off:{wallet}") or "").strip()
            state = "off " + reason if reason else "on"
            lines.append(f"{state} {wallet}")
        return "\n".join(lines)

    def _cmd_leader(self, args: list[str]) -> str:
        if len(args) != 2 or args[0] not in ("on", "off"):
            return "usage: /leader on|off <wallet>"
        wallet = args[1]
        if wallet not in self.cfg.leaders:
            return "unknown leader"
        if args[0] == "on":
            self.db.kv_set(f"off:{wallet}", "")
            self.db.kv_set(f"reset:{wallet}", str(self.now()))
            return f"{wallet[:4]}… on (loss window reset)"
        self.db.kv_set(f"off:{wallet}", "manual")
        return f"{wallet[:4]}… off"
