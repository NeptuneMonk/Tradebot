# RPC provider notes (2026-09-14)
- Primary/secondary/WS URLs: backend/.env SOLANA_RPC_URL, SOLANA_RPC_FALLBACK_URL, SOLANA_WSS_URL. HELIUS_* kept only for the Enhanced API key (creator_history, wallet_graph) and legacy fallback.
- QuickNode Discover: 50,000 req/day, getMultipleAccounts ≤5 keys, every WSS notification counts as a request. Pump.fun logsSubscribe firehose is NOT viable there. Paid Build ($49): 80M credits, 30 credits/RPC call, 50 credits/WSS response → firehose still ~200M credits/day → never run the Pump.fun listener on QuickNode.
- Helius bills WSS per 0.1 MB → the firehose belongs on Helius (or Triton/other byte-billed WSS). Enhanced API (`/v0/addresses/{addr}/transactions`) was 45% of Helius credits — cached 24h now.
- The dashboard `/api/trades/stuck` poll (every 30 s per tab) used to cost ~50 RPC calls per poll. Now 1 batch + 60 s cache.
- Diagnostics: GET /api/diagnostics/account-bus → rpc_calls_by_method, monitor_rpc_reads_saved_by_push, rpc_provider.
