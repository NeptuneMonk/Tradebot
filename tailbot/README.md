# tailbot

Paper-only Solana copy-trader. It watches up to three leader wallets, copies their buys, and sells the same fraction of our position when they sell. Fills, stops, and the profit sweep are simulated against live quotes and stored in SQLite. This process never loads a private key, never signs, and never sends a transaction.

Live execution is a flag that is off. Turning it on does not start trading: the process exits, and the send path raises before it would touch a key file. A signer is not implemented in this build.

## What it does

- One websocket on Helius standard methods only: `logsSubscribe` with `mentions: [wallet]` for each leader, plus `accountSubscribe` on the curve or quote vault of a coin we hold. `transactionSubscribe` is not used (it is not on the free plan). Every reconnect replays `getSignaturesForAddress` since the last cursor (at most `backfill_limit`, default 25). Buys older than the chase window are skipped as stale; sells of an open position still close it.
- Pump.fun `TradeEvent` logs are decoded locally. A parsed `getTransaction` is fetched only when the logs do not fully describe the leader's fill, or when a reconnect backfill finds a signature still inside the chase window.
- Buy size is the leader's token-balance delta and the quote they actually spent (SOL, USDC, USDT, or another token). A relayer fee payer and a rising SOL balance are ignored. The checked-in fixture is Wirelys `AvGiSdrkwtGYhrrFXZPtdhNhV4pASUR67TmRjfaKGdMP` paying 650.23 USDC for 13.1M of `2tKjPFvzeWqBmmxjtGBb3zfb8zfFVePjpBhHbE2epump` in `3amaPLE27t9yjRscLq6BNY8EPBYfpUqC9U2yExTsPMgZ3J7j66K5QTu5pj3tarpzNRURKHvsrERZy9n5n274mv9o` while their SOL balance went up by about 0.0015.
- Our size is a percent of the paper pot (default 10%, hard cap 20%), not a copy of the leader's dollar size.
- If our quote is more than 15% above the leader's fill, tailbot re-quotes every 5 seconds for up to 15 seconds and skips if it never comes back.
- Exits: leader sell (same fraction), hard stop −35%, trailing stop 25% once +50% with the stop floored at entry, momentum exit once +20% and buy flow on that pool slows sharply. At 24 hours it sends a Telegram prompt with Exit / Hold and does **not** sell.
- A 15% realized daily loss pauses buys until midnight America/Chicago. A leader turns off after 5 straight losses or worse than −25% over the last 20 closes. Both can be overridden from Telegram.
- On each winning close, 50% of the profit is earmarked for `EG9FyiBDGgRTTJjsiB26dtBq9j5L41S8nukZRsvpMXGp`. Amounts under 0.01 SOL batch. The sweep is a ledger line. Nothing is transferred.

## Run on a Linux box

Python 3.11+.

```bash
cd tailbot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

Edit `config.json`: put your leader wallets in `leaders` (at most 3) and change `sellall_pin`. Keep `execution_mode` at `"paper"` and `live_enabled` at `false`.

Secrets stay in the environment, not in the config file and not in git:

```bash
cp tailbot.env.example tailbot.env   # or write the file yourself
```

`tailbot.env` (mode 600) contains:

```
HELIUS_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

The chat id is the only account whose commands are accepted. Send `/status` to your bot once so you know the id (`getUpdates`, or a bot such as @userinfobot).

```bash
set -a
. ./tailbot.env
set +a
python3 -m tailbot
```

The log line `PAPER MODE — tailbot will not load a key, sign, or send a transaction` means it is in the only mode this build will run.

State is `data/tailbot.sqlite` (gitignored).

### systemd

```ini
# /etc/systemd/system/tailbot.service
[Unit]
Description=tailbot paper copy-trader
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/tailbot
EnvironmentFile=/opt/tailbot/tailbot.env
ExecStart=/opt/tailbot/.venv/bin/python3 -m tailbot
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

`EnvironmentFile` is not committed. `systemctl enable --now tailbot`.

### Tests

```bash
python3 -m pytest -q
```

No network and no API key. Decoding, risk, the chase window, exits, sweep math, and a paper engine pass are covered, including the USDC fixture.

## Telegram

Only `TELEGRAM_CHAT_ID` is answered. Other chats are ignored.

| Command | Effect |
|---|---|
| `/status` | Pot, cash, deployed, pause reasons, credits, feed |
| `/positions` | Open paper positions and unrealized % |
| `/pause` / `/resume` | Manual buy pause. Exits keep running |
| `/sellall <PIN>` | Paper-close everything. PIN is `sellall_pin` |
| `/leaders` | On/off and the auto-off reason |
| `/leader on\|off <wallet>` | `on` also resets that leader's loss window |
| `/override daily` | Ignore today's daily-loss pause until Chicago midnight |
| `/set <key> <value>` | Risk knobs (percents, seconds, SOL). Not `execution_mode` |

Alerts: every copy, every skip with the reason, every exit (`leader sold`, `stop`, `trail`, `momentum`, `manual`), each batched sweep, and a daily summary. A feed that stays down more than 60 seconds pauses new buys and sends one alert.

## Risk defaults

| Rule | Default |
|---|---|
| Pot per buy | 10%, hard cap 20% |
| Deployed | 60% of pot |
| Per leader | 20% of pot and 2 open mints |
| Skip leader buy | under 0.1 SOL (USDC/USDT converted at the SOL price; other tokens via a Jupiter quote) |
| Min market cap | $8,000 |
| Min PumpSwap liquidity | $10,000 |
| Flip | leader sold this mint in the last 30s |
| Token | any freeze authority, or a Token-2022 transfer-fee extension |
| Reserve / min buy | 0.05 SOL / 0.05 SOL |
| Chase | 15% over the leader, re-check every 5s, give up at 15s |
| Stop / trail | −35%; 25% trail armed at +50%, floored at entry |
| Momentum | after +20%, exit when the last 30s of buys are under 40% of the prior 150s rate and that baseline was at least 1 SOL |
| 24h | prompt, do not sell |
| Bad day | −15% realized vs equity at Chicago midnight |
| Leader off | 5 straight losses, or last 20 closes worse than −25% |
| Sweep | 50% of each win, batched at 0.01 SOL |

`/set` keys are the config names (`pot_pct_per_buy`, `stop_loss_pct`, …). Overrides persist in SQLite.

Paper buys book a Jupiter quote when one exists, otherwise constant-product math on SOL reserves (Pump.fun fee 1%, PumpSwap 0.30%, plus `paper_extra_slip_bps` of extra slip, default 100). A flat `paper_tx_fee_sol` (default 0.00008) is added to the cost. Impact, fee, and slip are rows in `fills`.

## Helius credits

Free plan is 1,000,000 credits per month, about **33,000 per day**. Published rates used here (see `tailbot/credits.py`, same byte rate as `backend/helius_budget.py`):

- Standard RPC, including `getTransaction`, `getSignaturesForAddress`, and `getAccountInfo`: **1 credit**. A reconnect asks for signatures once per leader (capped at 25) and only fetches a transaction when it is still inside the chase window.
- Standard websocket (`logsSubscribe`, `accountSubscribe`): **2 credits per 0.1 MB**
- `transactionSubscribe`, enhanced transactions, and the Pump program firehose are not used

`typical_day()` is about **400 credits**: 3 leaders × 40 txs, 8 KB log notifications, 35% of those need `getTransaction`, two open coins with 20,000 account updates each.

`busy_day()` is about **1,600 credits**: 3 × 120 txs, half fetched, four coins with 40,000 updates each.

A single hot coin at a few swaps per second for many hours is still account data, not program logs. Four coins at 200,000 updates would be about 6,400 websocket credits and would cross the soft ceiling (`helius_soft_ceiling`, default 8,000). Past that, new buys pause and curve/vault subscriptions drop; leader `getTransaction` continues until the hard ceiling (default 25,000), which is still under the free-tier day. The aim for a normal day is 1–3k.

## What came from `backend/`

Copied as plain functions so this process does not import modules that sign transactions or require Mongo and environment variables at import (`pumpfun.py` reads program ids from the environment and builds instructions):

| Source | Used for |
|---|---|
| `listener.py` + `pump_idl.json` `TradeEvent` | Local borsh decode of curve buys and sells, including `quote_mint` / `quote_amount` |
| `pumpswap_feed.py` `parse_amm_event` | PumpSwap `BuyEvent` / `SellEvent` (pool, user, base and quote amounts) |
| `pumpfun.decode_bonding_curve` | Curve account layout for marks and market cap |
| `pumpswap.py` pool header | Base/quote vaults so an open PumpSwap position can be watched cheaply |
| `fills.py` `paper_fill` | Constant-product impact, protocol fee, extra paper slip |
| `quote_mints.py` | SOL vs USDC/USDT vs "some other token" |
| `helius_budget.py` | Websocket credit math (2 credits / 0.1 MB) |
| `account_event_bus.py` | One socket, resubscribe after reconnect, backoff |
| `alerts.py` | Telegram Bot API over HTTPS. The chat id is the env var, not "whoever messages the bot" |
| `solana_client` token-balance delta | Shape of a parsed transaction. The leader's SOL delta is recorded and then not used as the trade size |
| `profit_sweep.py` | A paper sweep is a ledger entry that leaves the pot. The 50% per win and the 0.01 SOL batch are new |
| `jupiter.py` | Quote URL only. Swap instructions are not requested |

Not used, on purpose: `bot.py`, Mongo, `helius_sender.py`, `landing.py`, the live half of `profit_sweep.py`, `bankroll.py`, the book ladders in `exits.py`, `multihop.py` (that module builds a signed multi-hop instruction; paired coins are priced with Jupiter and balance deltas instead), `quote_prices.py` (Robinhood Chain).

## Layout

```
tailbot/
  config.example.json
  tailbot/            # package
  tests/fixtures/wirelys_usdc_buy.json
```

`execution_mode` other than `paper`, or `live_enabled: true`, makes the process exit. `tailbot/live.py` raises `LiveTradingBlocked` on every call, including when `TAILBOT_LIVE_KEY_PATH` is set. That variable is checked for emptiness only. The file is not opened.
