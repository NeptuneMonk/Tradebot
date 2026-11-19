"""This build never signs, sends, or reads a key file."""
from pathlib import Path

import pytest

from tailbot.config import Config, load_config
from tailbot.live import LiveTradingBlocked, assert_paper_process, refuse_live

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "tailbot"
BANNED = ("Keypair", "sendTransaction", "VersionedTransaction", "from_secret", "nacl", "solders.keypair")


def test_source_has_no_signer():
    for path in PKG.rglob("*.py"):
        text = path.read_text()
        for word in BANNED:
            assert word not in text, f"{path.name} contains {word}"


def test_refuse_live_does_not_open_the_key(tmp_path, monkeypatch):
    key = tmp_path / "secret.json"
    key.write_text("SUPERSECRET")
    monkeypatch.setenv("TAILBOT_LIVE_KEY_PATH", str(key))
    live = Config(execution_mode="live", live_enabled=True)
    with pytest.raises(LiveTradingBlocked):
        refuse_live(live)
    with pytest.raises(LiveTradingBlocked):
        assert_paper_process(live)
    assert key.read_text() == "SUPERSECRET"
    # Paper config is the only one that may start. A key path in the environment changes nothing.
    assert_paper_process(Config())
    paper = Config(execution_mode="paper", live_enabled=False)
    with pytest.raises(LiveTradingBlocked):
        refuse_live(paper)


def test_example_config_is_paper():
    cfg = load_config(str(ROOT / "config.example.json"))
    assert cfg.execution_mode == "paper"
    assert cfg.live_enabled is False
    assert cfg.leaders
    assert len(cfg.leaders) <= 3
    assert_paper_process(cfg)
