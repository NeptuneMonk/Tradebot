# Pump.fun Micro-Stake Trading Bot — PRD

## Original problem statement
Build a functioning experimental trading bot operating inside the Emergent preview environment.
Detect new Pump.fun token launches, invest $0.50–$1.00 of SOL, exit early based on simple pattern logic.
For learning and experimentation only — no deployment outside preview.

## User explicit choices (verbatim)
- "Real funds will be sent to this wallet and used"
- "Helius RPC: https://beta.helius-rpc.com/?api-key=c8d03259-d874-42eb-bbbb-22b6750bcc6e"
- "Generate fresh keypair in sandbox — store in backend .env"
- Daily kill switch: $20
- "Allow me to increase trades with UI functions if needed" — UI configurable; server-side hard cap at $5/trade
- Classifier defaults: curve fill > 30% in 10s → exit_early; unique buyers > 15 in 5s → hold_briefly
- Visual: "Simple and functional. Built for speed"
- "No simulated launches. Real launches"

## Architecture
- **Backend (FastAPI + Python)**: solana-py + solders, Helius RPC (HTTPS + WSS logsSubscribe), Pump.fun Anchor instruction builders (buy/sell/create-ATA), constant-product AMM math for quotes, rule-based classifier, async bot orchestrator with position monitor & kill-switch, MongoDB persistence (trades, launches, config, rules).
- **Frontend (React + Tailwind + shadcn)**: Single-page "Control Room" dashboard, polling every 3s, IBM Plex Sans/Mono, sharp-edged dark UI, Recharts P/L sparkline, QR deposit address.

## Implemented (2026-02-22)
- ✅ Solana wallet auto-generation, persisted to `/app/backend/wallet.json` (preview-only)
- ✅ Helius WSS logsSubscribe listener — **confirmed streaming real Pump.fun mainnet launches**
- ✅ Pump.fun buy/sell instruction builders with priority fee + compute budget IXs
- ✅ Bonding curve PDA derivation, ATA derivation (off-curve allowed)
- ✅ Constant-product AMM quote math (buy & sell, with slippage bps)
- ✅ Rule-based classifier: exit_early / hold_briefly / abort_trade + risk score 0–100
- ✅ Bot orchestrator: paper-mode default, live-mode toggle, take-profit, stop-loss, max-hold timeout, daily kill switch
- ✅ MongoDB persistence: launches, trades, bot_config, classifier_rules
- ✅ Safety caps: max_trade_usd ≤ $5, min_trade_usd ≥ $0.10, slippage 50–5000 bps, kill switch ≤ $100
- ✅ Dashboard with wallet card (QR + copy), bot control, P/L summary + sparkline, daily loss meter, active trades, recent launches feed (live pulsing dot), trade history, classifier rules editor, status banner with kill-switch reset
- ✅ Backend test suite at `/app/backend/tests/test_pump_bot_api.py` — 16/16 passing

## Active wallet
- Address: `Gbp9yFREc9dPvnfSjBmi9udg3UCrMmjZh2rjaPebRPrR`
- Private key: `/app/backend/wallet.json` (chmod 600, preview-only)
- Current balance: 0 SOL (awaiting deposit)

## Bot defaults
- `enabled: false` (start manually)
- `live_trading: false` (paper mode by default; user must explicitly toggle)
- min_trade_usd: $0.50, max_trade_usd: $1.00, slippage: 500 bps (5%)
- kill switch: $20, TP: 25%, SL: 30%, max hold: 30s, priority fee: 500k µLamports

## P1 / Next backlog
- **P1**: Real-time price for held positions (currently uses bonding curve quote — accurate but no UI live ticker)
- **P1**: WebSocket push to frontend instead of 3s polling (lower latency)
- **P1**: Creator wallet history lookup (currently `creator_rugs=0` placeholder)
- **P2**: Mempool-level metric collection (unique_buyers, sol_inflow) — currently only curve_fill_pct is updated
- **P2**: Bonding curve state caching to reduce RPC load
- **P2**: SOL inflow tracking via additional logsSubscribe filter on `Instruction: Buy`
- **P2**: Withdraw-to-external-address endpoint
- **P3**: Multi-keypair / hot-wallet rotation
- **P3**: Jito bundle support for competitive sniping

## Update — 2026-02-22 (v2 enhancements)
- ✅ **Mempool-level metrics**: Listener now parses both Pump.fun `CreateEvent` and `TradeEvent`. Bot tracks per-mint buckets for 60s after launch: unique_buyers (set), sol_inflow_lamports, buy_count, curve_fill_pct. Persisted to launch doc every 2s and surfaced in UI as icon badges. Confirmed live: e.g. "Mutilization" → 21 buyers / 17.4 SOL inflow.
- ✅ **Social trending score (no X API)**: New `social.py` calls DuckDuckGo Instant Answer (primary, works from cloud IPs) + Wikipedia opensearch + CoinGecko search; 5-minute per-term cache. Returns 0..100 score (DDG abstract=60, heading=25, related ≤20, wiki=10, cg=10). Confirmed: "pocky"=80, "Sun"=55, "Dreamcore"=64; obscure names correctly score 0.
- ✅ **New classifier rule** `social_score_min` (default 0 = disabled). If >0, aborts entry when token's social score is below threshold.
- ✅ **SOL price source diversified**: Binance → Coinbase → CoinGecko fallback chain (Binance 451-blocks the cloud IP; Coinbase works reliably).
- ✅ **UI**: launch rows now show inline buyers/inflow/curve%/SOC badges; ClassifierRulesEditor includes "Min social score" field.
- ✅ Backend tests: 28/28 passing (`test_pump_bot_api.py` + `test_pump_bot_enhancements.py`).

### Known limitations (non-blocking)
- Wikipedia (403) and CoinGecko (429) often refuse cloud-IP traffic; score is effectively DDG-dominant.
- `curve_fill_pct` only rises meaningfully after ~30 SOL of buys (Pump.fun virtual reserve math); for most launches it stays low — fine, doesn't affect logic.

### Remaining backlog (P1+)
- P1: WebSocket push to frontend (replace 3s polling)
- P1: Creator wallet history lookup (real `creator_rugs` count)
- P2: Track tokens we DIDN'T enter — historical "what-if" P/L
- P2: Optional Telegram alerts
- P3: Jito bundle support
