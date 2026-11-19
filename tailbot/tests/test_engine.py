"""Paper engine: copy, chase skip, proportional sell, stop, 24h hold, sweep, telegram."""
import asyncio
from tailbot.config import Config
from tailbot.credits import CreditMeter, busy_day, typical_day
from tailbot.db import DB
from tailbot.decode import Fill
from tailbot.engine import Engine
from tailbot.market import MarketInfo
from tailbot.paper import PaperBook
from tailbot.quotes import Quote
from tailbot.safety import MintSafety
from tailbot.telegram_bot import route_update

LEADER = "AvGiSdrkwtGYhrrFXZPtdhNhV4pASUR67TmRjfaKGdMP"
MINT = "2tKjPFvzeWqBmmxjtGBb3zfb8zfFVePjpBhHbE2epump"


class Clock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def now(self):
        return self.t

    async def sleep(self, dt):
        self.t += float(dt)


class Box:
    def __init__(self):
        self.msgs = []

    async def send(self, text, markup=None):
        self.msgs.append((text, markup))


class Quotes:
    def __init__(self, buy_px, sell_px):
        self.buy_px = buy_px if isinstance(buy_px, list) else [buy_px]
        self.sell_px = sell_px
        self.n = 0

    async def buy(self, mint, sol_in, market=None):
        px = self.buy_px[min(self.n, len(self.buy_px) - 1)]
        self.n += 1
        raw = int(sol_in / px * 1_000_000)
        return Quote(price_sol_per_ui=px, tokens_raw=raw, impact_pct=0.4, fee_pct=1.0, slip_pct=1.0, source="test", decimals=6)

    async def sell(self, mint, tokens_raw, market=None, decimals=6):
        ui = tokens_raw / (10 ** decimals)
        return Quote(price_sol_per_ui=self.sell_px, sol_out=ui * self.sell_px, tokens_raw=tokens_raw, source="test", decimals=decimals)

    async def sol_out_for_token(self, *args, **kwargs):
        return None


async def market(mint, pool=None):
    # Deep virtual curve so the exit-route check is independent of the test sell price.
    return MarketInfo("pumpfun", None, None, (30_000_000_000, 1_000_000_000_000_000), decimals=6, curve="Curve111")


async def safety(mint):
    return MintSafety(False, False, 6, True)


async def sol_usd():
    return 100.0


def make(tmp_path, pot=1.0, buy_px=0.01, sell_px=0.01, **cfg_kw):
    cfg = Config(leaders=[LEADER], paper_pot_sol=pot, sellall_pin="4242", **cfg_kw)
    db = DB(str(tmp_path / "t.sqlite"))
    paper = PaperBook(db, cfg)
    quotes = Quotes(buy_px, sell_px)
    box = Box()
    clock = Clock()
    engine = Engine(
        cfg, db, paper, quotes, CreditMeter(8_000, 25_000),
        alerter=box, market_loader=market, safety_loader=safety,
        sol_usd_fn=sol_usd, feed_down_fn=lambda: False,
    )
    engine.now = clock.now
    engine.sleep = clock.sleep
    return engine, box, clock


def buy(**kw):
    data = dict(
        signature="buy1", leader=LEADER, side="buy", mint=MINT,
        token_raw=100_000_000, token_decimals=6, pre_token_raw=0,
        payment_mint="SOL", payment_raw=1_000_000_000, payment_decimals=9, payment_kind="sol",
    )
    data.update(kw)
    return Fill(**data)


def test_copy_buy_and_proportional_sell(tmp_path):
    engine, box, _clock = make(tmp_path)

    async def go():
        await engine.on_leader_fill(buy())
        pos = engine.db.open_positions()
        assert len(pos) == 1
        assert any(text.startswith("COPY") for text, _m in box.msgs)
        held = int(pos[0]["tokens_raw"])
        await engine.on_leader_fill(Fill(
            signature="sell1", leader=LEADER, side="sell", mint=MINT,
            token_raw=50, token_decimals=6, pre_token_raw=100,
            payment_mint="SOL", payment_raw=500_000_000, payment_decimals=9, payment_kind="sol",
        ))
        pos = engine.db.position(pos[0]["id"])
        assert pos["status"] == "open"
        assert int(pos["tokens_raw"]) == held // 2 or abs(int(pos["tokens_raw"]) - held / 2) <= 1
        assert any(text.startswith("EXIT leader sold") for text, _m in box.msgs)

    asyncio.run(go())


def test_chase_skip_does_not_open(tmp_path):
    engine, box, clock = make(tmp_path, buy_px=0.012)

    async def go():
        await engine.on_leader_fill(buy(signature="chase"))
        assert engine.db.open_positions() == []
        assert any("above leader fill" in text for text, _m in box.msgs)
        assert clock.t == 1_700_000_000.0 + 15

    asyncio.run(go())


def test_stop_trail_momentum_and_24h(tmp_path):
    engine, box, clock = make(tmp_path, sell_px=0.005)

    async def go():
        await engine.on_leader_fill(buy(signature="s1"))
        pos = engine.db.open_positions()[0]
        entry = pos["entry_price"]
        engine.db.update_position(pos["id"], last_price=entry * 0.6, peak_price=entry)
        await engine.check_exits()
        assert engine.db.position(pos["id"])["status"] == "closed"
        assert any(text.startswith("EXIT stop") for text, _m in box.msgs)
        assert not any(text.startswith("SWEEP") for text, _m in box.msgs)

    asyncio.run(go())

    engine, box, clock = make(tmp_path, sell_px=0.011)
    # fresh db path — make() uses the same file. Use a subdir via a second call? same path overwrites schema
    # but the cash kv already exists from the previous run in the same file. Use a new folder name by
    # calling make only once per concern with isolated paths. This second make reuses tmp_path and the
    # existing cash. Isolate by deleting via a nested path.

    async def trail():
        # Rebuild on a clean book by constructing directly is heavier; the previous close returned cash.
        # Open another buy. Pot is about 1 SOL still (loss reduced it). Use a high mcap path.
        await engine.on_leader_fill(buy(signature="s2"))
        rows = [p for p in engine.db.open_positions()]
        assert rows, box.msgs
        pos = rows[0]
        entry = pos["entry_price"]
        engine.db.update_position(pos["id"], peak_price=entry * 1.6, last_price=entry * 1.1)
        engine.quotes.sell_px = entry * 1.1
        await engine.check_exits()
        assert engine.db.position(pos["id"])["status"] == "closed"
        assert any(text.startswith("EXIT trail") for text, _m in box.msgs)

    asyncio.run(trail())


def test_24h_prompt_does_not_sell(tmp_path):
    engine, box, clock = make(tmp_path / "day")

    async def go():
        await engine.on_leader_fill(buy())
        pos = engine.db.open_positions()[0]
        engine.db.update_position(pos["id"], next_prompt_ts=clock.t - 1, last_price=pos["entry_price"], peak_price=pos["entry_price"])
        await engine.check_exits()
        assert engine.db.position(pos["id"])["status"] == "open"
        prompts = [(text, markup) for text, markup in box.msgs if text.startswith("24h")]
        assert len(prompts) == 1 and prompts[0][1]["inline_keyboard"][0][0]["text"] == "Exit"
        await engine.check_exits()
        assert sum(1 for text, _m in box.msgs if text.startswith("24h")) == 1
        reply = await engine.on_callback(f"h:{pos['id']}")
        assert "holding" in reply
        assert engine.db.position(pos["id"])["status"] == "open"
        reply = await engine.on_callback(f"x:{pos['id']}")
        assert engine.db.position(pos["id"])["status"] == "closed"
        assert any(text.startswith("EXIT manual") for text, _m in box.msgs)

    asyncio.run(go())


def test_winning_close_sweeps_half(tmp_path):
    engine, box, _clock = make(tmp_path / "sw", pot=10.0, sell_px=0.02)

    async def go():
        await engine.on_leader_fill(buy(signature="win"))
        pos = engine.db.open_positions()[0]
        await engine.on_leader_fill(Fill(
            signature="win-sell", leader=LEADER, side="sell", mint=MINT,
            token_raw=100, token_decimals=6, pre_token_raw=100,
            payment_mint="SOL", payment_raw=2_000_000_000, payment_decimals=9, payment_kind="sol",
        ))
        assert engine.db.position(pos["id"])["status"] == "closed"
        assert any(text.startswith("SWEEP") and "EG9FyiBDGgRTTJjsiB26dtBq9j5L41S8nukZRsvpMXGp" in text for text, _m in box.msgs)
        assert engine.paper.pending_sweep() == 0.0
        # Pot kept the other half of the profit: cash is above the original 10.
        assert engine.paper.cash() > 10

    asyncio.run(go())


def test_bad_day_feed_pause_and_commands(tmp_path):
    engine, box, _clock = make(tmp_path / "bd")
    engine.db.kv_set("realized_sol", "-0.2")

    async def go():
        await engine.on_leader_fill(buy(signature="blocked"))
        assert engine.db.open_positions() == []
        assert any("daily loss pause" in text for text, _m in box.msgs)
        assert await engine.on_command("/override daily")
        await engine.on_leader_fill(buy(signature="after"))
        assert len(engine.db.open_positions()) == 1
        assert "paused" in await engine.on_command("/pause")
        await engine.on_leader_fill(buy(signature="paused", mint="MintPaused111111111111111111111111111111"))
        # unknown mint still a buy attempt; paused skips it. The mint string must be a pubkey for nothing else.
        assert any("paused" in text for text, _m in box.msgs)
        assert await engine.on_command("/resume")
        status = await engine.on_command("/status")
        assert status.startswith("PAPER")
        assert "PAPER" in await engine.on_command("/positions") or "tok" in await engine.on_command("/positions")
        assert LEADER in await engine.on_command("/leaders")
        off = await engine.on_command(f"/leader off {LEADER}")
        assert "off" in off
        await engine.on_leader_fill(buy(signature="off"))
        assert any("leader off" in text for text, _m in box.msgs)
        assert "on" in await engine.on_command(f"/leader on {LEADER}")
        assert "pot_pct" in await engine.on_command("/set pot_pct_per_buy 12")
        assert engine.cfg.pot_pct_per_buy == 12
        assert await engine.on_command("/sellall no") == "bad pin"
        assert engine.db.open_positions()
        sold = await engine.on_command("/sellall 4242")
        assert sold.startswith("sold")
        assert engine.db.open_positions() == []

    asyncio.run(go())


def test_telegram_ignores_other_chats():
    assert route_update({"message": {"chat": {"id": 1}, "text": "/status"}}, "2") is None
    assert route_update({"message": {"chat": {"id": 2}, "text": "/status"}}, "2") == ("command", "/status")
    update = {"callback_query": {"data": "x:1", "message": {"chat": {"id": 9}}}}
    assert route_update(update, "2") is None
    assert route_update({"callback_query": {"data": "h:3", "message": {"chat": {"id": "2"}}}}, "2") == ("callback", "h:3")


def test_momentum_exit_from_flow(tmp_path):
    engine, box, clock = make(tmp_path / "mo", sell_px=0.0125)

    async def go():
        await engine.on_leader_fill(buy(signature="mo"))
        pos = engine.db.open_positions()[0]
        entry = pos["entry_price"]
        engine.db.update_position(pos["id"], last_price=entry * 1.25, peak_price=entry * 1.25)
        now = clock.t
        engine.flow[MINT] = [(now - 180, "buy", 2.0), (now - 40, "buy", 1.0), (now - 5, "buy", 0.01)]
        engine.quotes.sell_px = entry * 1.25
        await engine.check_exits()
        assert engine.db.position(pos["id"])["status"] == "closed"
        assert any(text.startswith("EXIT momentum") for text, _m in box.msgs)

    asyncio.run(go())


def test_credit_scenarios_stay_in_budget():
    typical = typical_day()
    busy = busy_day()
    assert typical.total < 1_500
    assert busy.total < 3_000
    assert busy.total < 33_000
