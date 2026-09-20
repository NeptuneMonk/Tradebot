"""Creator Wallet Audit — master gate: check evaluation with a synthetic Helius history, policy for unavailable data."""
import asyncio
import time

import pytest

import creator_audit as ca
from models import BotConfig

CREATOR = "Crea1111111111111111111111111111111111111111"
MINT = "Mint1111111111111111111111111111111111111pump"
H = 3600.0


def _tx(ts, *, inbound=None, source=None, program=None, create=False, sell=False):
    tx = {"timestamp": ts, "signature": f"sig{ts}", "type": "CREATE" if create else ("SWAP" if source else "TRANSFER"),
          "source": source or ("PUMP_FUN" if create else "SYSTEM_PROGRAM"), "nativeTransfers": [], "tokenTransfers": [], "instructions": []}
    if inbound:
        tx["nativeTransfers"].append({"fromUserAccount": "Funder", "toUserAccount": CREATOR, "amount": inbound})
    if program:
        tx["instructions"].append({"programId": program, "accounts": []})
    if sell:
        tx["tokenTransfers"].append({"mint": MINT, "fromUserAccount": CREATOR, "toUserAccount": "Buyer"})
    return tx


class _Cur:
    def __init__(self, rows): self.rows = rows
    def limit(self, n): return self
    def __aiter__(self):
        async def gen():
            for r in self.rows:
                yield r
        return gen()


class _DB:
    def __init__(self, creator_doc=None, launches=None):
        self._doc = creator_doc
        self._launches = launches or []
        self.creators = self
        self.launches = self
    async def find_one(self, flt, proj=None): return self._doc
    def find(self, flt, proj=None): return _Cur(self._launches if "chain" in flt else [])


def _cfg(**kw):
    c = BotConfig()
    c.creator_audit_enabled = True
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _run(cfg, db, history, deep=False, meta=None, chain="sol", deploy_ts=None):
    ca._cache.clear()
    deploy_ts = deploy_ts or time.time() - 60
    async def fake_hist(creator, until): return history, deep
    async def fake_meta(mint): return meta if meta is not None else {"name": "Good Token", "symbol": "GOOD", "uri": "https://x/y.json", "uri_reachable": True, "ok": True}
    async def fake_black(db_, ttl_s=300): return set()
    ca._sol_history, ca._sol_metadata = fake_hist, fake_meta
    import creator_greylist
    creator_greylist._get_blacklisted_creators = fake_black
    return asyncio.run(ca.audit(cfg, db, chain=chain, creator=CREATOR, mint=MINT, deploy_ts=deploy_ts, name="Good Token", symbol="GOOD")), deploy_ts


def _status(res, key):
    return next(c for c in res["checks"] if c["key"] == key)["status"]


def test_clean_creator_passes():
    now = time.time(); d = now - 60
    hist = [_tx(d - 3 * 86400, inbound=2_000_000_000), _tx(d - 2 * 86400, source="JUPITER"), _tx(d - 86400, program=ca.PUMP_PROGRAM), _tx(d, create=True)]
    res, _ = _run(_cfg(), _DB({"tokens_failed": 0}), hist, deploy_ts=d)
    assert res["verdict"] == "pass", res
    assert all(_status(res, k) == "pass" for k in ("funded_before", "prior_dex", "funding_lead", "wallet_age", "deploys_per_hour", "tags", "metadata"))
    assert _status(res, "post_activity") == "n/a" and _status(res, "verified") == "n/a"


def test_throwaway_wallet_fails_lead_age_and_dex():
    now = time.time(); d = now - 60
    hist = [_tx(d - 40, inbound=500_000_000), _tx(d, create=True)]
    res, _ = _run(_cfg(), _DB({"tokens_failed": 0}), hist, deploy_ts=d)
    assert res["verdict"] == "skip"
    assert _status(res, "funding_lead") == "fail" and _status(res, "wallet_age") == "fail" and _status(res, "prior_dex") == "fail"
    assert res["reason"].startswith("creator-audit: prior dex") or "funding" in res["reason"] or "wallet age" in res["reason"]


def test_per_hour_from_history_and_our_feed():
    now = time.time(); d = now - 60
    hist = [_tx(d - 5 * 86400, inbound=1_000_000_000, source="RAYDIUM"), _tx(d - 30, create=True), _tx(d, create=True)]
    res, _ = _run(_cfg(), _DB({"tokens_failed": 0}), hist, deploy_ts=d)
    assert _status(res, "deploys_per_hour") == "fail"                  # batch deploy = 2 in the hour
    from datetime import datetime, timezone
    launches = [{"detected_at": datetime.fromtimestamp(d - 1800, tz=timezone.utc), "mint": "other"}]
    hist2 = [_tx(d - 5 * 86400, inbound=1_000_000_000, source="RAYDIUM"), _tx(d, create=True)]
    res2, _ = _run(_cfg(), _DB({"tokens_failed": 0}, launches), hist2, deploy_ts=d)
    assert _status(res2, "deploys_per_hour") == "fail"
    res3, _ = _run(_cfg(creator_audit_max_deploys_per_hour=2), _DB({"tokens_failed": 0}, launches), hist2, deploy_ts=d)
    assert _status(res3, "deploys_per_hour") == "pass"


def test_creator_sell_after_deploy_fails_post_activity_even_when_optional():
    now = time.time(); d = now - 600
    hist = [_tx(d + 30, sell=True), _tx(d - 5 * 86400, inbound=1_000_000_000, source="RAYDIUM"), _tx(d, create=True)]
    res, _ = _run(_cfg(), _DB({"tokens_failed": 0}), hist, deploy_ts=d)
    assert _status(res, "post_activity") == "fail" and res["verdict"] == "skip"


def test_rug_tag_and_metadata_fail():
    now = time.time(); d = now - 60
    hist = [_tx(d - 5 * 86400, inbound=1_000_000_000, source="RAYDIUM"), _tx(d, create=True)]
    res, _ = _run(_cfg(), _DB({"tokens_failed": 2}), hist, deploy_ts=d)
    assert _status(res, "tags") == "fail"
    res2, _ = _run(_cfg(), _DB({"tokens_failed": 0}), hist, meta={"name": "", "symbol": "GOOD", "uri": "", "ok": True}, deploy_ts=d)
    assert _status(res2, "metadata") == "fail"


def test_unavailable_policy_pass_vs_skip():
    d = time.time() - 60
    res, _ = _run(_cfg(creator_audit_unavailable="pass"), _DB({"tokens_failed": 0}), None, deploy_ts=d)
    assert res["verdict"] == "pass" and "funding_lead" in res["unavailable"]
    res2, _ = _run(_cfg(creator_audit_unavailable="skip"), _DB({"tokens_failed": 0}), None, deploy_ts=d)
    assert res2["verdict"] == "skip" and "fail-closed" in res2["reason"]


def test_deep_history_counts_as_established_wallet():
    d = time.time() - 60
    hist = [_tx(d - 3600 * 5, source="JUPITER"), _tx(d, create=True)]      # fetched window is shallow but the wallet has more
    res, _ = _run(_cfg(), _DB({"tokens_failed": 0}), hist, deep=True, deploy_ts=d)
    assert _status(res, "wallet_age") == "pass" and _status(res, "funded_before") == "pass" and _status(res, "funding_lead") == "pass"


def test_cache_hit_returns_same_result():
    d = time.time() - 60
    hist = [_tx(d - 5 * 86400, inbound=1_000_000_000, source="RAYDIUM"), _tx(d, create=True)]
    res, _ = _run(_cfg(), _DB({"tokens_failed": 0}), hist, deploy_ts=d)
    async def boom(*a, **k): raise AssertionError("history refetched despite cache")
    ca._sol_history = boom
    again = asyncio.run(ca.audit(_cfg(), _DB({"tokens_failed": 0}), chain="sol", creator=CREATOR, mint=MINT, deploy_ts=d))
    assert again is res


def test_solscan_profile_drives_checks(monkeypatch):
    """With a Solscan profile the history checks come from the explorer (funded-by, labels, transactions) and Helius is not called."""
    d = time.time() - 60
    prof = {"provider": "solscan", "errors": {},
            "detail": {"lamports": 5_000_000_000},
            "funded_by": {"funded_by": "Exchange1111", "block_time": d - 3 * 86400, "tx_hash": "h"},
            "metadata": {"account_label": "Binance Hot Wallet", "account_tags": []},
            "token": {"name": "Good Token", "symbol": "GOOD", "metadata_uri": "https://x/y.json", "metadata": {"image": "x"}},
            "defi": [{"activity_type": "ACTIVITY_TOKEN_SWAP"}],
            "txs": [{"block_time": d - 2 * 86400, "program_ids": ["JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"], "tx_hash": "a"},
                    {"block_time": d, "program_ids": [ca.PUMP_PROGRAM], "parsed_instructions": [{"type": "create", "program_id": ca.PUMP_PROGRAM}], "tx_hash": "b"}],
            "deep": False}
    async def fake_prof(creator, mint, deploy_ts): return prof
    async def boom(*a, **k): raise AssertionError("Helius must not be called when Solscan answered")
    monkeypatch.setattr(ca, "_solscan_profile", fake_prof)
    monkeypatch.setattr(ca, "_sol_history", boom)
    ca._cache.clear()
    res = asyncio.run(ca.audit(_cfg(), _DB({"tokens_failed": 0}), chain="sol", creator=CREATOR, mint=MINT, deploy_ts=d))
    assert res["verdict"] == "pass", res
    assert _status(res, "funding_lead") == "pass" and _status(res, "wallet_age") == "pass" and _status(res, "prior_dex") == "pass"
    assert next(c for c in res["checks"] if c["key"] == "_provider")["detail"] == "solscan"
    prof["metadata"] = {"account_label": "Scam: token drainer", "account_tags": ["scam"]}
    ca._cache.clear()
    res2 = asyncio.run(ca.audit(_cfg(), _DB({"tokens_failed": 0}), chain="sol", creator=CREATOR, mint=MINT, deploy_ts=d))
    assert _status(res2, "tags") == "fail" and res2["verdict"] == "skip"


def test_rh_post_activity_is_unavailable_not_fail(monkeypatch):
    """RH has no explorer: an unobservable check must defer to creator_audit_unavailable, never hard-fail every creator."""
    import rh_wallet

    async def _rpc(method, params, timeout=8.0):
        return hex(10 ** 18) if method == "eth_getBalance" else hex(5)
    monkeypatch.setattr(rh_wallet, "rpc", _rpc)
    monkeypatch.setattr(ca, "_tags", lambda db, c: _async({"ok": True, "blacklisted": False, "rugs": 0}))
    now = time.time()
    cfg = _cfg(creator_audit_require_post_activity=True, creator_audit_unavailable="pass", creator_audit_min_prior_dex=3)
    res = asyncio.run(ca.audit(cfg, _DB(), chain="rh", creator="0x" + "a" * 40, mint="0x" + "b" * 40, deploy_ts=now - 30, deploy_block=100, name="Tok", symbol="TOK"))
    assert _status(res, "post_activity") == "unavailable"
    assert res["verdict"] == "pass", res["reason"]
    cfg.creator_audit_unavailable = "skip"
    ca._cache.clear()
    res = asyncio.run(ca.audit(cfg, _DB(), chain="rh", creator="0x" + "c" * 40, mint="0x" + "d" * 40, deploy_ts=now - 30, deploy_block=100, name="Tok", symbol="TOK"))
    assert res["verdict"] == "skip" and "unavailable" in (res["reason"] or "")


async def _async(v):
    return v
