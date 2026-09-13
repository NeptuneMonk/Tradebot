"""Hot-wallet integrity: nonce-locked / data-carrying / foreign-owned accounts are flagged and block live arming."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wallet_integrity as wi

NONCE_PARSED = {"owner": wi.SYSTEM_PROGRAM, "lamports": 296011893, "executable": False,
                "data": {"program": "nonce", "space": 80,
                         "parsed": {"type": "initialized", "info": {"authority": "AmK8k6ZqE4Rnguw1q83XNQ934b2SWE1ni4vLP6Hz1P3r",
                                                                    "blockhash": "3g4D", "feeCalculator": {"lamportsPerSignature": "5000"}}}}}


def test_classify_plain_system_account_ok():
    assert wi.classify({"owner": wi.SYSTEM_PROGRAM, "data": ["", "base64"], "lamports": 5}) == \
        {"ok": True, "kind": "system", "reason": None, "nonce_authority": None}
    assert wi.classify(None)["kind"] == "unfunded" and wi.classify(None)["ok"]


def test_classify_nonce_account_is_compromised():
    out = wi.classify(NONCE_PARSED)
    assert out["ok"] is False and out["kind"] == "nonce-account" and out["nonce_authority"].startswith("AmK8")
    assert "NONCE" in out["reason"] and "Rotate the key" in out["reason"]


def test_classify_data_and_foreign_owner():
    import base64
    raw = {"owner": wi.SYSTEM_PROGRAM, "data": [base64.b64encode(b"\x00" * 80).decode(), "base64"]}
    assert wi.classify(raw)["kind"] == "data-carrying"
    assert wi.classify({"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": ["", "base64"]})["kind"] == "foreign-owner"


def test_check_caches_and_logs(monkeypatch):
    calls = []

    async def fake_rpc(method, params):
        calls.append(method)
        return {"result": {"value": NONCE_PARSED}}
    monkeypatch.setattr(wi, "rpc_call", fake_rpc)
    wi._cache.clear()
    a = asyncio.run(wi.check("Gbp9"))
    b = asyncio.run(wi.check("Gbp9"))
    assert a["ok"] is False and b is a and calls == ["getAccountInfo"]
    src = Path(__file__).resolve().parents[1].joinpath("server.py").read_text()
    assert "refusing to arm Solana live trading" in src
