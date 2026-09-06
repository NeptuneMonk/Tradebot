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
from rh_discovery import T_BUY, T_SELL
from models import BotConfig


def test_keystore_created_encrypted_and_reloadable():
    addr = rh_wallet.address()
    assert addr.startswith("0x") and len(addr) == 42
    raw = open(os.environ["RH_WALLET_PATH"]).read()
    assert "crypto" in raw and rh_wallet._ACCT.key.hex() not in raw
    assert rh_wallet._load_or_create().address == addr
    assert oct(os.stat(os.environ["RH_WALLET_PATH"]).st_mode)[-3:] == "600"


def test_import_private_key_switches_account():
    from eth_account import Account
    a = Account.create()
    assert rh_wallet.import_private_key(a.key.hex()) == a.address and rh_wallet.address() == a.address


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
