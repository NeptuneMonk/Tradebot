# PUMP.BOT — Cut the Fat

A leaner version of the existing Pump.fun / Robinhood Chain trading bot that drops every data source and book that has produced no trading edge, so it runs fast and stable on the basic hosting tier.
Same entries, same exits, same money-making path (scanner → scalp → runner / re-entry); far fewer moving parts.

## Who it's for
The single operator running the published bot today, on the basic hosting package, who wants it to react faster, stop restarting, and be simple enough that a newcomer could run it from one screen.

## Core features and experience
- **Dead weight removed.** Ladder book (1,089 tokens watched, zero trades ever), SCAN tab (the LIVE tab's candidate window already shows the same list), Greylist sniper and its scoring, wallet-graph hunter, creator audit, creator-history backfill, research mode, serial-creator gate. All gone from the running bot and the UI. Historical data stays in the database untouched.
- **Fewer, cheaper data sources.** Token socials/images/metadata are fetched only for mints that pass the entry gate or get bought — not for every launch seen. Creator "reputation" is no longer computed in-house; a slot is reserved for an outsourced reputation feed later.
- **Lighter tracker.** The in-memory launch tracker is capped and shorter-lived; per-tick launch stats are no longer written to the database for every mint, and old launch rows expire automatically.
- **Doctor on a schedule.** Strategy Doctor, learning and autopsy run every 15 minutes instead of continuously; their recommendations and breakers behave the same.
- **Untouched:** scanner engine, scalp/hunt/runner/RH books and their exit math, re-entry, cost gate, R-sizing, kill switch, Live Doctor breakers, equity chart, trade history, the fast WebSocket feed and the leader-routing fix.

## User flow
1. Operator opens the dashboard. Tabs are LIVE, DOCTOR, BOOKS, CONTROL, WIKI — no SCAN, no LADDER.
2. LIVE shows the launch tape, candidate window, active positions, equity and history exactly as now.
3. CONTROL shows the essentials up top (Paper/Live, Max trade $, Daily stop $, book switches, Speed, Start/Stop); everything else sits in a collapsed Advanced drawer.
4. Operator presses Start. The bot scans, enters, promotes to runner and exits as before — with roughly half the memory, a fraction of the network and database traffic, and no OOM restarts.
5. DOCTOR refreshes its findings every 15 minutes and still trips/lifts breakers in real time.

## UI/UX feel
Unchanged visual language — dark control-room, monospace, sharp edges. The difference is subtraction: fewer tabs, fewer cards, one screen that a first-time user can operate. Advanced settings exist but are out of the way.

## Implementation phases

### Phase 1 — MVP (built now): remove dead weight and shrink the hot path
- Remove Ladder book: tab, watches, account subscriptions, background loop, API routes.
- Remove SCAN tab and its polling; the scanner engine keeps running and pushing candidates to the LIVE tab.
- Remove Greylist sniper, greylist scoring loop and panel, wallet-graph hunter, creator audit, creator-history backfill, research mode, serial-creator gate from the entry path and UI.
- Metadata/socials fetched only for gate-passing or entered mints.
- Launch tracker capped (~150 mints, shorter window); per-tick launch persistence limited to candidates and trades; 48-hour expiry on old launch rows.
- Doctor / learning / autopsy moved to a 15-minute schedule.
- Acceptance: no SCAN or LADDER tab; no ladder/greylist/audit network calls while running; paper bot starts, scalp/hunt/RH can open and close, runner promotion still works; memory and request counts visibly lower on the diagnostics strip.

### Phase 2 — Simple control surface
- Six-control CONTROL view with a collapsed Advanced drawer holding the rest.
- "Safe paper" preset: max trade $8, 2 seats, scalp + hunt + runner + RH on, autopilot off. Applying it never touches wallets or open positions.
- Trade-refusal and exit reasons kept plain-English on the ticker.

### Phase 3 — Outsourced reputation and self-protection
- Dark adapter for an external dev-reputation feed (e.g. reputation.family): configurable URL/key/field path, tiers normalised to CRAZY / PROVEN / GOOD / FARMER / UNKNOWN plus fake-chart flag; one lookup per new mint, cached. Off until a URL is configured; when on, Hunt requires an allowed tier and skips on any miss.
- Lite-mode watchdog: if pod memory or event-loop lag crosses a threshold, non-essential loops pause automatically and a badge shows it, instead of the platform killing the bot.
- Option to run one chain by default on the basic tier.

## Assumptions
- Scalp stays enabled by default: it is the only feeder of runner promotions and re-entries, which are the profitable lines in the ledger. Its seat count is left as configured.
- Robinhood chain stays on by default (toggle unchanged); it idles its discovery polling while the Doctor has the RH book paused.
- The scanner engine is kept; only its separate tab and REST polling are removed.
- Re-entry logic is kept as-is.
- Greylist, ladder, wallet-graph and audit collections remain in the database; nothing is deleted, only no longer read or written on the live path.
- "Easier for public users" means a simpler single-operator UI; login stays single-email and single-wallet. Multi-user is out of scope.
- No new books (no arbitrage), no changes to cost gate, SL/trail/clock math, Doctor breaker rules or the WebSocket transport.
- The basic hosting tier (current memory/CPU limits) is the target; no tier upgrade is assumed.
- Production secrets for Helius RPC/WSS are set by the operator in the deployment panel; the plan does not depend on them but trade speed does.
