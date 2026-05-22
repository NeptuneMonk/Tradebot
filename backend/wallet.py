"""
Solana wallet module.
Generates a keypair on first use, persists to wallet.json (preview-only).
NEVER move this file outside the preview environment.
"""
import os
import json
import base58
from pathlib import Path
from solders.keypair import Keypair
from solders.pubkey import Pubkey

WALLET_PATH = Path(os.environ.get("WALLET_SECRET_PATH", "/app/backend/wallet.json"))


def _load_or_create_keypair() -> Keypair:
    if WALLET_PATH.exists():
        with open(WALLET_PATH, "r") as f:
            data = json.load(f)
        secret = base58.b58decode(data["secret_key_b58"])
        return Keypair.from_bytes(secret)
    kp = Keypair()
    payload = {
        "public_key": str(kp.pubkey()),
        "secret_key_b58": base58.b58encode(bytes(kp)).decode("utf-8"),
    }
    WALLET_PATH.write_text(json.dumps(payload, indent=2))
    os.chmod(WALLET_PATH, 0o600)
    return kp


_KEYPAIR: Keypair = _load_or_create_keypair()


def get_keypair() -> Keypair:
    return _KEYPAIR


def get_pubkey() -> Pubkey:
    return _KEYPAIR.pubkey()


def get_pubkey_str() -> str:
    return str(_KEYPAIR.pubkey())


def get_secret_b58() -> str:
    """Return private key (b58). Preview-only diagnostic."""
    return base58.b58encode(bytes(_KEYPAIR)).decode("utf-8")
