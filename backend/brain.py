"""Brain export/import — carry the bot's learning (Doctor memory, creator intel,
trade history, config) between preview and published environments.

File format: gzip'd NDJSON. Line 1 = header, every other line = {"c": collection, "d": doc}
(bson json_util encoding so dates / ObjectIds round-trip)."""
import asyncio
import json
import logging
import os
import time
import uuid
import zlib
from datetime import datetime, timezone

from bson import json_util
from pymongo import InsertOne, ReplaceOne, UpdateOne

logger = logging.getLogger("brain")

SCHEMA_VERSION = 1
UPLOAD_DIR = "/tmp/brain_uploads"
BATCH = 500

GROUPS = {
    "config": ["bot_config", "classifier_rules", "bot_config_defaults"],
    "doctor": ["strategy_suggestions", "autopilot_state", "doctor_trail_state", "doctor_canary",
               "live_doctor_state", "doctor_blacklist"],
    "creators": ["creators", "wallet_links", "wallet_graph"],
    "trades": ["trades"],
    "ticks": ["tick_paths"],
}
DEFAULT_GROUPS = ["config", "doctor", "creators", "trades"]

# Environment-specific choices that must never be carried across.
LOCAL_CONFIG_KEYS = {"enabled", "live_trading", "rh_live_trading", "helius_tracker_enabled",
                     "rh_feed_enabled", "sweep_enabled", "sweep_cold_wallet"}
ALT_KEY = {"strategy_suggestions": "id", "doctor_blacklist": "fingerprint"}
TS_KEYS = ("updated_at", "greylist_score_updated_at", "last_seen", "pnl_reconciled_at", "exit_time",
           "entry_time", "applied_at", "created_at", "saved_at", "last_evaluated_at", "started_at",
           "discovered_at")

IMPORTS: dict[str, dict] = {}


def _cols(groups: list[str]) -> list[str]:
    return [c for g in groups if g in GROUPS for c in GROUPS[g]]


def _ts(doc: dict):
    for k in TS_KEYS:
        v = doc.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, datetime):
            return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).timestamp()
        if isinstance(v, str):
            try:
                d = datetime.fromisoformat(v.replace("Z", "+00:00"))
                return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
            except ValueError:
                continue
    return None


def _key(col: str, doc: dict):
    alt = ALT_KEY.get(col)
    if alt and doc.get(alt) is not None:
        return alt
    return "_id" if doc.get("_id") is not None else None


async def summary(db) -> dict:
    out = {}
    for g, cols in GROUPS.items():
        counts = {c: await db[c].estimated_document_count() for c in cols}
        out[g] = {"collections": counts, "total": sum(counts.values())}
    return {"groups": out, "defaults": DEFAULT_GROUPS}


async def export_stream(db, groups: list[str], env: str):
    comp = zlib.compressobj(6, zlib.DEFLATED, 31)
    cols = _cols(groups)
    header = {"schema_version": SCHEMA_VERSION, "exported_at": datetime.now(timezone.utc).isoformat(),
              "env": env, "groups": groups, "collections": cols}
    yield comp.compress((json.dumps(header) + "\n").encode())
    for col in cols:
        async for doc in db[col].find({}):
            line = json_util.dumps({"c": col, "d": doc}) + "\n"
            chunk = comp.compress(line.encode())
            if chunk:
                yield chunk
    yield comp.flush()


# ---------------------------------------------------------------- import ----

def begin_upload(filename: str, size: int) -> str:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    uid = uuid.uuid4().hex
    IMPORTS[uid] = {"state": "uploading", "filename": filename, "size": size, "received": 0,
                    "chunks": 0, "processed": 0, "stats": {}, "error": None, "started_at": time.time()}
    open(os.path.join(UPLOAD_DIR, uid), "wb").close()
    return uid


def append_chunk(uid: str, index: int, data: bytes) -> dict:
    st = IMPORTS.get(uid)
    if not st or st["state"] != "uploading":
        raise ValueError("unknown or finished upload")
    if index != st["chunks"]:
        raise ValueError(f"expected chunk {st['chunks']}, got {index}")
    with open(os.path.join(UPLOAD_DIR, uid), "ab") as f:
        f.write(data)
    st["received"] += len(data)
    st["chunks"] += 1
    return st


def _iter_lines(path: str):
    d = zlib.decompressobj(47)
    buf = b""
    with open(path, "rb") as f:
        while True:
            raw = f.read(1 << 20)
            if not raw:
                break
            buf += d.decompress(raw)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                yield buf[:nl]
                buf = buf[nl + 1:]
    buf += d.flush()
    if buf.strip():
        yield buf


def _bump(stats: dict, col: str, k: str, n: int = 1):
    s = stats.setdefault(col, {"added": 0, "updated": 0, "kept": 0, "skipped": 0})
    s[k] += n


async def _flush_batch(db, col: str, docs: list[dict], stats: dict):
    if not docs:
        return
    if col == "wallet_links":
        ops = [UpdateOne({"_id": d["_id"]}, {"$addToSet": {"linked_to_creators": {"$each": list(d.get("linked_to_creators") or [])}}}, upsert=True)
               for d in docs if isinstance(d.get("_id"), str)]
        if ops:
            await db[col].bulk_write(ops, ordered=False)
        _bump(stats, col, "updated", len(ops))
        return
    keyed: dict[str, list] = {}
    for d in docs:
        k = _key(col, d)
        if k is None:
            _bump(stats, col, "skipped")
            continue
        keyed.setdefault(k, []).append(d)
    ops = []
    for k, group in keyed.items():
        vals = [d[k] for d in group]
        existing = {e[k]: e async for e in db[col].find({k: {"$in": vals}})}
        for d in group:
            if k != "_id":
                d.pop("_id", None)
            ex = existing.get(d[k])
            if ex is None:
                ops.append(InsertOne(d))
                _bump(stats, col, "added")
                continue
            et, it = _ts(ex), _ts(d)
            if it is not None and (et is None or it > et):
                ops.append(ReplaceOne({k: d[k]}, d))
                _bump(stats, col, "updated")
            else:
                _bump(stats, col, "kept")
    if ops:
        await db[col].bulk_write(ops, ordered=False)


async def run_import(db, uid: str, groups: list[str], apply_config, apply_rules):
    st = IMPORTS[uid]
    st["state"] = "running"
    path = os.path.join(UPLOAD_DIR, uid)
    wanted = set(_cols(groups))
    stats = st["stats"]
    try:
        lines = _iter_lines(path)
        header = json.loads(next(lines))
        if header.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported brain file (schema {header.get('schema_version')})")
        st["source_env"] = header.get("env")
        st["exported_at"] = header.get("exported_at")
        batches: dict[str, list] = {}
        for raw in lines:
            rec = json_util.loads(raw)
            col, doc = rec.get("c"), rec.get("d")
            if col not in wanted or not isinstance(doc, dict):
                continue
            st["processed"] += 1
            if col == "bot_config":
                cfg = {k: v for k, v in doc.items() if k not in LOCAL_CONFIG_KEYS and k != "_id"}
                await apply_config(cfg)
                _bump(stats, col, "updated")
                continue
            if col == "classifier_rules":
                await apply_rules({k: v for k, v in doc.items() if k != "_id"})
                _bump(stats, col, "updated")
                continue
            if col == "trades" and doc.get("status") == "active":
                _bump(stats, col, "skipped")  # another environment's open position
                continue
            b = batches.setdefault(col, [])
            b.append(doc)
            if len(b) >= BATCH:
                await _flush_batch(db, col, b, stats)
                batches[col] = []
                await asyncio.sleep(0)
        for col, b in batches.items():
            await _flush_batch(db, col, b, stats)
        st["state"] = "done"
    except Exception as e:
        logger.exception("brain import failed")
        st["state"] = "error"
        st["error"] = str(e)
    finally:
        st["finished_at"] = time.time()
        try:
            os.remove(path)
        except OSError:
            pass
