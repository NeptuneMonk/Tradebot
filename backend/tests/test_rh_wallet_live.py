"""RH wallet + live execution plumbing (no broadcast): keystore, calldata, log parsing, gates."""
import asyncio, os, sys
from types import SimpleNamespace
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import tempfile
os.environ["RH_WALLET_PATH"] = os.path.join(tempfile.mkdtemp(), "rh_wallet.json")
os.environ["RH_WALLET_PASS_PATH"] = os.path.join(os.path.dirname(os.environ["RH_WALLET_PATH"]), "rh_wallet.pass")
import rh_wallet
import rh_live
from pathlib import Path
# never touch the real hot-wallet files, whatever import order pytest used
rh_wallet.WALLET_PATH = Path(os.environ["RH_WALLET_PATH"])
rh_wallet.PASS_PATH = Path(os.environ["RH_WALLET_PASS_PATH"])
rh_wallet._ACCT = rh_wallet._load_or_create()
from rh_discovery import T_BUY, T_SELL
from models import BotConfig


def test_keystore_created_encrypted_and_reloadable():
    addr = rh_wallet.address()
    assert addr.startswith("0x") and len(addr) == 42
    raw = open(rh_wallet.WALLET_PATH).read()
    assert "crypto" in raw and rh_wallet._ACCT.key.hex() not in raw
    assert rh_wallet._load_or_create().address == addr
    assert oct(os.stat(rh_wallet.WALLET_PATH).st_mode)[-3:] == "600"


def test_import_private_key_switches_account():
    from eth_account import Account
    orig = rh_wallet._ACCT
    a = Account.create()
    try:
        assert rh_wallet.import_private_key(a.key.hex()) == a.address and rh_wallet.address() == a.address
    finally:
        rh_wallet._persist(orig)
        rh_wallet._ACCT = orig


def test_calldata_selectors_match_onchain():
    me = "0x64354B3c927B659aA5E6F6001a19647FC6B41D49"
    d = rh_wallet.calldata(rh_live.BUY_SIG, ["uint256", "uint256", "address"], [8113855773333334, 0, me])
    assert d.startswith("0x59a87bc1") and len(d) == 2 + 8 + 64 * 3
    assert d[10:74] == "000000000000000000000000000000000000000000000000001cd3824320d756"
    s = rh_wallet.calldata(rh_live.SELL_SIG, ["uint256", "uint256", "address"], [1, 0, me])
    assert s.startswith("0xd04c6983")


def test_trade_log_parsing_from_real_receipt_shape():
    token = "0x86034231a0c14ade6bda15dc90ead927eb50621b"
    rc = {"logs": [
        {"address": "0xother", "topics": [T_BUY], "data": "0x" + "00" * 96},
        {"address": token.upper().replace("0X", "0x"), "topics": [T_BUY, "0x0", "0x0"],
         "data": "0x" + "0000000000000000000000000000000000000000000000000a32ec77dae70100"
                       "0000000000000000000000000000000000000000000d703e283ba958ee663e8c"
                       "000000000000000000000000000000000000000000000000001a1bf6f563970c"
                       "0000000000000000000000000000000000000000000000000000000000000000"},
    ]}
    q, t, f = rh_live._trade_log(rc, token, T_BUY)
    assert q == 0x0a32ec77dae70100 and t == 0x0d703e283ba958ee663e8c and f == 0x1a1bf6f563970c
    assert rh_live._trade_log(rc, token, T_SELL) is None


def test_live_gate_requires_flag_eth_quote_and_no_kill():
    import rh_paper
    st = SimpleNamespace(config=BotConfig(), rh_discovery=SimpleNamespace(tracking={}))
    tr = rh_paper.RHPaperTrader(st)
    assert tr.live_ok({"quote_symbol": "ETH"}) is False
    st.config.rh_live_trading = True
    assert tr.live_ok({"quote_symbol": "ETH"}) is True
    assert tr.live_ok({"quote_symbol": "DJT"}) is False
    tr.live_kill_tripped = True
    assert tr.live_ok({"quote_symbol": "ETH"}) is False
    assert tr._active() is False  # bot not enabled
    st.config.enabled = True
    assert tr._active() is True   # live flag alone activates the RH trader


def test_config_defaults():
    c = BotConfig()
    assert (c.rh_live_trading, c.rh_live_slippage_pct, c.rh_gas_reserve_eth, c.rh_daily_kill_switch_usd) == (False, 8.0, 0.002, 20.0)


def test_sell_approves_curve_before_selling_when_allowance_missing(monkeypatch):
    """Verified on-chain 2026-09-07: every direct PONS seller approve()s the curve first;
    our 3 live sells reverted with allowance=0. sell(token=...) must approve, then sell."""
    calls = []
    me = rh_wallet.address()
    curve, token = "0x59bfc19200000000000000000000000000000001", "0x71880db900000000000000000000000000000002"

    async def fake_allowance(tok, spender, owner=None):
        return 0
    async def fake_simulate(to, data, value=0, sender=None):
        calls.append(("sim", to.lower(), data[:10]))
        return "0x"
    async def fake_send(to, data="0x", value=0, gas_limit=None):
        calls.append(("send", to.lower(), data[:10]))
        return "0x" + "ab" * 32
    async def fake_receipt(tx, timeout=90.0):
        return {"ok": True, "gas_cost_wei": 7, "logs": [], "blockNumber": "0x10"}

    monkeypatch.setattr(rh_wallet, "allowance", fake_allowance)
    monkeypatch.setattr(rh_wallet, "simulate", fake_simulate)
    monkeypatch.setattr(rh_wallet, "send", fake_send)
    monkeypatch.setattr(rh_wallet, "wait_receipt", fake_receipt)

    fill = asyncio.run(rh_live.sell(curve, 1000, 1e-9, 5.0, token=token))
    sends = [c for c in calls if c[0] == "send"]
    assert sends[0] == ("send", token.lower(), "0x095ea7b3"), "first tx must be approve(curve, MAX) on the token"
    assert sends[1] == ("send", curve.lower(), "0xd04c6983"), "then sell() on the curve"
    assert fill["gas_cost_wei"] == 14  # approve gas + sell gas both booked
    # approve payload targets the curve with MAX_UINT256
    approve_data = rh_wallet.calldata("approve(address,uint256)", ["address", "uint256"], [rh_wallet.checksum(curve), rh_wallet.MAX_UINT256])
    assert approve_data.endswith("f" * 64)


def test_sell_skips_approve_when_allowance_sufficient(monkeypatch):
    calls = []
    async def fake_allowance(tok, spender, owner=None):
        return 10**30
    async def fake_simulate(to, data, value=0, sender=None):
        return "0x"
    async def fake_send(to, data="0x", value=0, gas_limit=None):
        calls.append(data[:10]); return "0x" + "cd" * 32
    async def fake_receipt(tx, timeout=90.0):
        return {"ok": True, "gas_cost_wei": 3, "logs": [], "blockNumber": "0x10"}
    monkeypatch.setattr(rh_wallet, "allowance", fake_allowance)
    monkeypatch.setattr(rh_wallet, "simulate", fake_simulate)
    monkeypatch.setattr(rh_wallet, "send", fake_send)
    monkeypatch.setattr(rh_wallet, "wait_receipt", fake_receipt)
    asyncio.run(rh_live.sell("0x" + "1" * 40, 5, 1e-9, 5.0, token="0x" + "2" * 40))
    assert calls == ["0xd04c6983"]
