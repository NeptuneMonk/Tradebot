"""Seed graduation P0 test rows in Mongo & verify APIs. Iteration 24."""
import os, sys, time, json, uuid, datetime, requests
sys.path.insert(0, "/app/backend")
from pymongo import MongoClient
from solders.keypair import Keypair

URL = "https://micro-stake-trader.preview.emergentagent.com"
TOK = open("/app/memory/.tok").read().strip()
HDR = {"Authorization": f"Bearer {TOK}"}

client = MongoClient("mongodb://localhost:27017")
db = client["pump_bot_db"]

def iso_now():
    return datetime.datetime.utcnow().isoformat()

# Clean prior seeds
db.trades.delete_many({"seed_test": True})

# --- Solana graduating row ---
sol_mint = str(Keypair().pubkey())
sol_id = str(uuid.uuid4())
sol_doc = {
    "_id": sol_id, "id": sol_id, "seed_test": True,
    "chain": "solana", "protocol": "pumpfun", "mode": "paper",
    "status": "active", "book": "hunt",
    "venue_stage": "graduating",
    "graduating_since": time.time() + 600,  # far future keeps grace from expiring
    "mint": sol_mint, "symbol": "TESTGRAD",
    "entry_sol": 0.05, "entry_usd": 8.0, "entry_tokens": 1_000_000,
    "entry_price_sol": 5e-8, "entry_time": iso_now(),
    "risk_score": 42,
}
db.trades.insert_one(sol_doc)

# --- Solana pumpswap-migrated row ---
sol2_mint = str(Keypair().pubkey())
sol2_id = str(uuid.uuid4())
sol2_doc = {
    "_id": sol2_id, "id": sol2_id, "seed_test": True,
    "chain": "solana", "protocol": "pumpswap", "mode": "paper",
    "status": "active", "book": "hunt",
    "venue_stage": "pumpswap",
    "pumpswap_pool": "PoolTest111111111111111111111111",
    "mint": sol2_mint, "symbol": "TESTPS",
    "entry_sol": 0.05, "entry_usd": 8.0, "entry_tokens": 1_000_000,
    "entry_price_sol": 5e-8, "entry_time": iso_now(),
    "risk_score": 42,
}
db.trades.insert_one(sol2_doc)

# --- RH pool row ---
import secrets
rh_mint = "0x" + secrets.token_hex(20)
rh_id = str(uuid.uuid4())
rh_doc = {
    "_id": rh_id, "id": rh_id, "seed_test": True,
    "chain": "rh", "book": "rh_pons", "protocol": "pons", "mode": "paper",
    "status": "active",
    "venue": "pool",
    "graduated_during_hold": True,
    "graduated_at_pnl_pct": 18.4,
    "mint": rh_mint, "symbol": "TESTRH",
    "entry_quote": 0.004, "quote_symbol": "ETH", "entry_usd": 10.0,
    "entry_tokens": 2_000_000.0, "entry_price_quote": 2e-9,
    "entry_time": iso_now(),
}
db.trades.insert_one(rh_doc)

# --- Held-through-migrate stuck row ---
htm_mint = str(Keypair().pubkey())
htm_id = str(uuid.uuid4())
htm_doc = {
    "_id": htm_id, "id": htm_id, "seed_test": True,
    "chain": "solana", "protocol": "pumpfun", "mode": "paper",
    "status": "exit_failed_terminal", "book": "hunt",
    "venue_stage": "held_through_migrate",
    "mint": htm_mint, "symbol": "TESTHELD",
    "entry_sol": 0.05, "entry_usd": 8.0, "entry_tokens": 1_000_000,
    "entry_price_sol": 5e-8, "entry_time": iso_now(),
    "exit_time": iso_now(),
    "exit_reason": "held through migration test seed",
    "pnl_sol": None, "pnl_usd": None, "pnl_pct": None,
    "mark_price_sol": 5e-8,
}
db.trades.insert_one(htm_doc)

print(json.dumps({"sol_mint": sol_mint, "sol_id": sol_id, "sol2_id": sol2_id,
                  "rh_mint": rh_mint, "rh_id": rh_id, "htm_id": htm_id}, indent=2))

# API assertions
def _get(path):
    r = requests.get(f"{URL}{path}", headers=HDR, timeout=15)
    return r.status_code, r.json() if r.headers.get("content-type","").startswith("application/json") else r.text

print("--- /api/trades/active ---")
sc, active = _get("/api/trades/active")
print("status:", sc, "rows:", len(active) if isinstance(active, list) else "n/a")
sol_row = next((t for t in active if t.get("id")==sol_id), None)
sol2_row = next((t for t in active if t.get("id")==sol2_id), None)
rh_row = next((t for t in active if t.get("id")==rh_id), None)
assert sol_row and sol_row.get("venue_stage")=="graduating", f"sol graduating row missing/wrong: {sol_row}"
assert sol2_row and sol2_row.get("venue_stage")=="pumpswap", f"sol pumpswap row missing/wrong: {sol2_row}"
assert rh_row and rh_row.get("venue")=="pool", f"rh pool row missing/wrong: {rh_row}"
print("  sol graduating OK, sol pumpswap OK, rh pool OK")

print("--- /api/trades/stuck ---")
sc, stuck = _get("/api/trades/stuck")
print("status:", sc, "rows:", len(stuck) if isinstance(stuck, list) else stuck)
htm_row = next((t for t in stuck if t.get("id")==htm_id), None) if isinstance(stuck, list) else None
assert sc == 200, f"stuck endpoint status {sc}: {stuck}"
assert htm_row, f"held-through-migrate row missing from stuck: {stuck}"
assert htm_row.get("venue_stage") == "held_through_migrate"
assert htm_row.get("pnl_sol") is None and htm_row.get("pnl_usd") is None and htm_row.get("pnl_pct") is None
print("  htm row present with venue_stage=held_through_migrate and pnl null")

print("--- /api/pl/summary ---")
sc, pl = _get("/api/pl/summary")
print("status:", sc, "keys:", list(pl.keys()) if isinstance(pl, dict) else pl)

print("--- /api/trades/history ---")
sc, hist = _get("/api/trades/history")
print("status:", sc)
hist_list = hist if isinstance(hist, list) else (hist.get("trades") or hist.get("items") or [])
htm_in_hist = any(t.get("id")==htm_id for t in hist_list)
assert not htm_in_hist, "exit_failed_terminal row must NOT appear in closed history"
print("  htm row correctly excluded from history")

print("\nALL API CHECKS PASSED")
