"""SQLite state. One file, no Mongo."""
from __future__ import annotations

import os
import sqlite3
import time


SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS seen (sig TEXT NOT NULL, leader TEXT NOT NULL, ts REAL, PRIMARY KEY (sig, leader));
CREATE TABLE IF NOT EXISTS leader_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, sig TEXT, leader TEXT, mint TEXT, side TEXT,
  token_raw TEXT, pre_raw TEXT, payment_mint TEXT, payment_raw TEXT, payment_kind TEXT,
  sol_delta INTEGER, venue TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, sig TEXT, leader TEXT, mint TEXT, action TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  leader TEXT, mint TEXT, status TEXT,
  tokens_raw TEXT, cost_sol REAL, entry_price REAL,
  opened_ts REAL, peak_price REAL, last_price REAL, decimals INTEGER,
  protocol TEXT, pool TEXT, curve TEXT, quote_vault TEXT, quote_mint TEXT,
  next_prompt_ts REAL, prompted INTEGER DEFAULT 0, created_sig TEXT
);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, position_id INTEGER, side TEXT, sol_amount REAL, tokens_raw TEXT,
  price REAL, impact_pct REAL, fee_pct REAL, slip_pct REAL, fee_sol REAL,
  source TEXT, reason TEXT, leader_sig TEXT
);
CREATE TABLE IF NOT EXISTS closes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, leader TEXT, mint TEXT, position_id INTEGER,
  pnl_sol REAL, cost_sol REAL, reason TEXT
);
CREATE TABLE IF NOT EXISTS sweeps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, sol_amount REAL, wallet TEXT, position_id INTEGER
);
CREATE TABLE IF NOT EXISTS mints (
  mint TEXT PRIMARY KEY, freeze INTEGER, transfer_fee INTEGER, decimals INTEGER, ts REAL
);
CREATE TABLE IF NOT EXISTS credits (
  day TEXT PRIMARY KEY, rpc REAL, ws_bytes INTEGER
);
"""

_POS_COLS = {
    "tokens_raw", "cost_sol", "entry_price", "peak_price", "last_price", "status",
    "next_prompt_ts", "prompted", "decimals", "protocol", "pool", "curve", "quote_vault", "quote_mint",
}


def _pos(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tokens_raw"] = int(d["tokens_raw"] or 0)
    d["prompted"] = int(d["prompted"] or 0)
    return d


class DB:
    def __init__(self, path: str):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def kv_get(self, key: str, default=None):
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def kv_set(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, str(value)),
        )
        self.conn.commit()

    def seen(self, sig: str, leader: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM seen WHERE sig=? AND leader=?", (sig, leader)).fetchone()
        return row is not None

    def mark_seen(self, sig: str, leader: str, ts: float | None = None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen(sig, leader, ts) VALUES(?,?,?)",
            (sig, leader, ts if ts is not None else time.time()),
        )
        self.conn.commit()

    def seen_among(self, leader: str, sigs: list[str]) -> set[str]:
        if not sigs:
            return set()
        marks = ",".join("?" for _ in sigs)
        rows = self.conn.execute(
            f"SELECT sig FROM seen WHERE leader=? AND sig IN ({marks})",
            [leader, *sigs],
        ).fetchall()
        return {row["sig"] for row in rows}

    def cursor(self, leader: str) -> str | None:
        return self.kv_get(f"cursor:{leader}")

    def set_cursor(self, leader: str, signature: str) -> None:
        self.kv_set(f"cursor:{leader}", signature)

    def add_leader_trade(self, fill, ts: float) -> None:
        self.conn.execute(
            """INSERT INTO leader_trades
               (ts, sig, leader, mint, side, token_raw, pre_raw, payment_mint, payment_raw, payment_kind, sol_delta, venue)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ts, fill.signature, fill.leader, fill.mint, fill.side, str(fill.token_raw),
                None if fill.pre_token_raw is None else str(fill.pre_token_raw),
                fill.payment_mint, str(fill.payment_raw), fill.payment_kind, int(fill.sol_delta_lamports), fill.venue,
            ),
        )
        self.conn.commit()

    def recent_sell_times(self, leader: str, mint: str, since_ts: float) -> list[float]:
        rows = self.conn.execute(
            "SELECT ts FROM leader_trades WHERE leader=? AND mint=? AND side='sell' AND ts>=?",
            (leader, mint, since_ts),
        ).fetchall()
        return [float(r["ts"]) for r in rows]

    def add_decision(self, ts, sig, leader, mint, action, reason) -> None:
        self.conn.execute(
            "INSERT INTO decisions(ts, sig, leader, mint, action, reason) VALUES(?,?,?,?,?,?)",
            (ts, sig, leader, mint, action, reason),
        )
        self.conn.commit()

    def open_positions(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM positions WHERE status='open' ORDER BY id").fetchall()
        return [_pos(r) for r in rows]

    def open_position(self, leader: str, mint: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE status='open' AND leader=? AND mint=?",
            (leader, mint),
        ).fetchone()
        return _pos(row) if row else None

    def position(self, pid: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
        return _pos(row) if row else None

    def insert_position(self, **fields) -> int:
        fields["tokens_raw"] = str(int(fields["tokens_raw"]))
        cols = list(fields)
        sql = f"INSERT INTO positions ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})"
        cur = self.conn.execute(sql, [fields[c] for c in cols])
        self.conn.commit()
        return int(cur.lastrowid)

    def update_position(self, pid: int, **fields) -> None:
        if "tokens_raw" in fields:
            fields["tokens_raw"] = str(int(fields["tokens_raw"]))
        unknown = set(fields) - _POS_COLS
        if unknown:
            raise KeyError(unknown)
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE positions SET {sets} WHERE id=?", [*fields.values(), pid])
        self.conn.commit()

    def add_fill(self, **fields) -> None:
        if "tokens_raw" in fields:
            fields["tokens_raw"] = str(int(fields["tokens_raw"]))
        cols = list(fields)
        sql = f"INSERT INTO fills ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})"
        self.conn.execute(sql, [fields[c] for c in cols])
        self.conn.commit()

    def add_close(self, ts, leader, mint, position_id, pnl_sol, cost_sol, reason) -> None:
        self.conn.execute(
            "INSERT INTO closes(ts, leader, mint, position_id, pnl_sol, cost_sol, reason) VALUES(?,?,?,?,?,?,?)",
            (ts, leader, mint, position_id, pnl_sol, cost_sol, reason),
        )
        self.conn.commit()

    def closes_for(self, leader: str, since_ts: float = 0.0) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ts, pnl_sol, cost_sol, reason, mint FROM closes WHERE leader=? AND ts>=? ORDER BY id",
            (leader, since_ts),
        ).fetchall()
        return [dict(r) for r in rows]

    def closes_since(self, ts: float) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM closes WHERE ts>=? ORDER BY id", (ts,)).fetchall()
        return [dict(r) for r in rows]

    def add_sweep(self, ts, sol_amount, wallet, position_id) -> None:
        self.conn.execute(
            "INSERT INTO sweeps(ts, sol_amount, wallet, position_id) VALUES(?,?,?,?)",
            (ts, sol_amount, wallet, position_id),
        )
        self.conn.commit()

    def sweeps_since(self, ts: float) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM sweeps WHERE ts>=? ORDER BY id", (ts,)).fetchall()
        return [dict(r) for r in rows]

    def mint_safety(self, mint: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM mints WHERE mint=?", (mint,)).fetchone()
        return dict(row) if row else None

    def save_mint(self, mint: str, freeze: bool, transfer_fee: bool, decimals: int, ts: float) -> None:
        self.conn.execute(
            "INSERT INTO mints(mint, freeze, transfer_fee, decimals, ts) VALUES(?,?,?,?,?) "
            "ON CONFLICT(mint) DO UPDATE SET freeze=excluded.freeze, transfer_fee=excluded.transfer_fee, decimals=excluded.decimals, ts=excluded.ts",
            (mint, int(freeze), int(transfer_fee), int(decimals), ts),
        )
        self.conn.commit()

    def credits_for(self, day: str) -> tuple[float, int]:
        row = self.conn.execute("SELECT rpc, ws_bytes FROM credits WHERE day=?", (day,)).fetchone()
        if not row:
            return 0.0, 0
        return float(row["rpc"] or 0), int(row["ws_bytes"] or 0)

    def save_credits(self, day: str, rpc: float, ws_bytes: int) -> None:
        self.conn.execute(
            "INSERT INTO credits(day, rpc, ws_bytes) VALUES(?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET rpc=excluded.rpc, ws_bytes=excluded.ws_bytes",
            (day, rpc, int(ws_bytes)),
        )
        self.conn.commit()
