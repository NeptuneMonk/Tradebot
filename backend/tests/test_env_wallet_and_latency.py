"""EMERGENT_CHANGES S + #10 — env-based wallet key gates live trading; balance cache; blockhash cache."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import base58  # noqa: E402
from solders.keypair import Keypair  # noqa: E402

import wallet  # noqa: E402
import landing  # noqa: E402
from models import BotConfig  # noqa: E402


def test_live_key_check_refuses_file_key(monkeypatch):
    for k in wallet.ENV_KEY_VARS:
        monkeypatch.delenv(k, raising=False)
    ok, why = wallet.live_key_check()
    assert ok is False and "SOLANA_PRIVATE_KEY" in why
    # key-less default: no key file on disk → ephemeral in-memory key, nothing persisted
    assert wallet.key_source() == "ephemeral" and wallet.is_ephemeral()
    assert not wallet.WALLET_PATH.exists()


def test_set_keypair_does_not_write_a_file_by_default(monkeypatch, tmp_path):
    for k in wallet.ENV_KEY_VARS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("WALLET_PERSIST_KEYFILE", raising=False)
    monkeypatch.setattr(wallet, "WALLET_PATH", tmp_path / "paper_wallet.json")
    orig = wallet.get_keypair()
    try:
        kp = Keypair()
        wallet.set_keypair(kp)
        assert wallet.get_pubkey_str() == str(kp.pubkey()) and not (tmp_path / "paper_wallet.json").exists()
        monkeypatch.setenv("WALLET_PERSIST_KEYFILE", "true")
        wallet.set_keypair(Keypair())
        assert (tmp_path / "paper_wallet.json").exists()
    finally:
        wallet._KEYPAIR = orig


def test_live_key_check_accepts_env_key(monkeypatch):
    kp = Keypair()
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", base58.b58encode(bytes(kp)).decode())
    ok, _ = wallet.live_key_check()
    assert ok is True and wallet.key_source() == "env:SOLANA_PRIVATE_KEY"
    assert str(wallet._load_or_create_keypair().pubkey()) == str(kp.pubkey())
    # JSON byte-array form (solana-keygen) is accepted too
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", str(list(bytes(kp))))
    assert str(wallet._load_or_create_keypair().pubkey()) == str(kp.pubkey())


def test_new_config_fields_defaults():
    cfg = BotConfig()
    assert cfg.fast_fail_split_enabled is False and cfg.fast_fail_add_at_pct == 5.0
    assert cfg.socials_fetch_blocking is False and cfg.entry_balance_cache_s == 10


def test_blockhash_cache_hits_within_max_age(monkeypatch):
    calls = {"n": 0}

    async def fake_rpc(method, params):
        calls["n"] += 1
        return {"result": {"value": {"blockhash": f"BH{calls['n']}"}}}

    monkeypatch.setattr(landing, "rpc_call", fake_rpc)
    monkeypatch.setattr(landing, "_BH_CACHE", None)
    a = asyncio.run(landing.recent_blockhash())
    b = asyncio.run(landing.recent_blockhash())
    assert a == b == "BH1" and calls["n"] == 1
    landing._BH_CACHE = ("OLD", time.monotonic() - 25.0)
    assert asyncio.run(landing.recent_blockhash()) == "BH2"
