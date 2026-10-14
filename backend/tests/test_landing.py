"""Fast-landing executor: fee ladder, dual broadcast, late landing, revert handling (RPC fully mocked)."""
import asyncio
import pytest
from solders.keypair import Keypair
from solders.instruction import Instruction, AccountMeta
from solders.pubkey import Pubkey

import landing


def _ix(kp):
    return Instruction(program_id=Pubkey.from_string("11111111111111111111111111111111"), data=b"\x00",
                       accounts=[AccountMeta(kp.pubkey(), True, True)])


class FakeChain:
    """Scripted RPC: sigs land after `land_after_sends` sendTransaction calls, optionally with an error."""
    def __init__(self, land_after_sends=1, err=None, meta_err=None, never=False):
        self.sends, self.sent_sigs, self.land_after, self.err, self.meta_err, self.never = 0, [], land_after_sends, err, meta_err, never
        self.sender_posts = 0

    async def rpc(self, method, params, *a, **k):
        if method == "getLatestBlockhash":
            return {"result": {"value": {"blockhash": str(Keypair().pubkey())}}}
        if method == "sendTransaction":
            self.sends += 1
            return {"result": "sig"}
        if method == "getSignatureStatuses":
            sigs = params[0]
            out = []
            for i, s in enumerate(sigs):
                landed = (not self.never) and self.sends >= self.land_after and i == min(max(self.land_after - 1, 0), len(sigs) - 1)
                out.append({"confirmationStatus": "confirmed", "err": self.err} if landed else None)
            return {"result": {"value": out}}
        if method == "getTransaction":
            return {"result": {"meta": {"err": self.meta_err}}}
        raise AssertionError(method)


@pytest.fixture
def kp():
    return Keypair()


@pytest.fixture
def chain(monkeypatch):
    def _mk(**kw):
        fc = FakeChain(**kw)
        monkeypatch.setattr(landing, "rpc_call", fc.rpc)

        async def _sender(raw, mode="dual"):
            fc.sender_posts += 1
        monkeypatch.setattr(landing, "_send_sender", _sender)
        return fc
    return _mk


def test_fee_ladder_doubles_to_cap():
    assert landing.fee_ladder(1_000_000, 20_000_000) == [1_000_000, 2_000_000, 4_000_000, 8_000_000, 16_000_000, 20_000_000]
    assert landing.fee_ladder(3_000_000, 20_000_000) == [3_000_000, 6_000_000, 12_000_000, 20_000_000]
    assert landing.fee_ladder(25_000_000, 20_000_000) == [20_000_000]


def test_lands_first_rung_dual_route(kp, chain):
    fc = chain(land_after_sends=1)
    sig, prio = asyncio.run(landing.land_tx(kp, [_ix(kp)], priority_ladder=[1_000_000, 2_000_000], rung_s=0.2, budget_s=3.0))
    assert prio == 1_000_000 and sig and fc.sends == 1 and fc.sender_posts == 1


def test_bumps_fee_until_landing(kp, chain):
    fc = chain(land_after_sends=3)
    sig, prio = asyncio.run(landing.land_tx(kp, [_ix(kp)], priority_ladder=[1_000_000, 2_000_000, 4_000_000, 8_000_000], rung_s=0.1, budget_s=5.0))
    assert prio == 4_000_000 and fc.sends == 3


def test_not_landed_carries_every_signature(kp, chain):
    chain(never=True)
    with pytest.raises(landing.NotLanded) as ei:
        asyncio.run(landing.land_tx(kp, [_ix(kp)], priority_ladder=[1_000_000, 2_000_000, 4_000_000], rung_s=0.1, budget_s=3.0))
    assert len(ei.value.sigs) == 3


def test_revert_is_reported_not_retried_with_more_fee(kp, chain):
    fc = chain(land_after_sends=1, meta_err={"InstructionError": [2, {"Custom": 6003}]})
    with pytest.raises(landing.LandedButFailed) as ei:
        asyncio.run(landing.land_tx(kp, [_ix(kp)], priority_ladder=[1_000_000, 2_000_000, 4_000_000], rung_s=0.1, budget_s=3.0))
    assert "6003" in str(ei.value.err) and fc.sends == 1


def test_wait_for_landing_resolves_late(kp, chain):
    chain(land_after_sends=0)
    sig, err = asyncio.run(landing.wait_for_landing(["a", "b"], timeout_s=1.0, interval_s=0.05))
    assert sig == "a" and err is None


def test_rpc_only_when_sender_down(kp, chain, monkeypatch):
    fc = chain(land_after_sends=1)

    async def boom(raw, mode="dual"):
        raise RuntimeError("sender 503")
    monkeypatch.setattr(landing, "_send_sender", boom)
    sig, _ = asyncio.run(landing.land_tx(kp, [_ix(kp)], priority_ladder=[1_000_000], rung_s=0.2, budget_s=2.0))
    assert sig and fc.sends == 1


def test_parse_secret_b58_and_json_roundtrip():
    import json, base58
    from wallet import parse_secret
    kp = Keypair()
    b58 = base58.b58encode(bytes(kp)).decode()
    assert str(parse_secret(b58).pubkey()) == str(kp.pubkey())
    assert str(parse_secret(json.dumps(list(bytes(kp)))).pubkey()) == str(kp.pubkey())
    with pytest.raises(ValueError):
        parse_secret("abc")
