"""
Solana wallet module.
Generates a keypair on first use, persists to wallet.json (preview-only).
NEVER move this file outside the preview environment.
"""
import os
import time
import json
import base58
from pathlib import Path
from solders.keypair import Keypair
from solders.pubkey import Pubkey

WALLET_PATH = Path(os.environ.get("WALLET_SECRET_PATH", "/app/backend/wallet.json"))


def _load_or_create_keypair() -> Keypair:
    # Published/containers: keep the key out of git and out of the ephemeral filesystem — WALLET_SECRET_B58 wins.
    env_secret = os.environ.get("WALLET_SECRET_B58", "").strip()
    if env_secret:
        return Keypair.from_bytes(base58.b58decode(env_secret))
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


def key_source() -> str:
    return "env:WALLET_SECRET_B58" if os.environ.get("WALLET_SECRET_B58", "").strip() else f"file:{WALLET_PATH}"


def rotate_keypair() -> tuple[Keypair, Keypair, Path]:
    """Retire the current key file (kept as wallet.json.retired-<ts>, 0600) and hot-swap a fresh keypair.
    Refused when the key comes from the environment — rotate the secret there instead."""
    global _KEYPAIR
    if os.environ.get("WALLET_SECRET_B58", "").strip():
        raise RuntimeError("wallet key comes from WALLET_SECRET_B58 — rotate the secret in the service env, not here")
    old = _KEYPAIR
    retired = WALLET_PATH.with_name(f"{WALLET_PATH.name}.retired-{int(time.time())}")
    if WALLET_PATH.exists():
        WALLET_PATH.rename(retired)
        os.chmod(retired, 0o600)
    _KEYPAIR = _load_or_create_keypair()
    return old, _KEYPAIR, retired


def parse_secret(text: str) -> Keypair:
    """Accept a base58 secret (Phantom / Solflare export) or a JSON byte array (solana-keygen)."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty key")
    if raw.startswith("["):
        arr = json.loads(raw)
        if not isinstance(arr, list) or not all(isinstance(b, int) and 0 <= b <= 255 for b in arr):
            raise ValueError("JSON array must hold bytes 0-255")
        data = bytes(arr)
    else:
        data = base58.b58decode(raw)
    if len(data) == 32:
        return Keypair.from_seed(data)
    if len(data) != 64:
        raise ValueError(f"expected 64-byte secret (or 32-byte seed), got {len(data)} bytes")
    return Keypair.from_bytes(data)


def set_keypair(kp: Keypair, *, persist: bool = True) -> tuple[Keypair, Path | None]:
    """Hot-swap the active key (import / cross-pod sync). The previous key file is retired, never deleted."""
    global _KEYPAIR
    old = _KEYPAIR
    retired = None
    if persist and not os.environ.get("WALLET_SECRET_B58", "").strip():
        if WALLET_PATH.exists() and str(old.pubkey()) != str(kp.pubkey()):
            retired = WALLET_PATH.with_name(f"{WALLET_PATH.name}.retired-{int(time.time())}")
            WALLET_PATH.rename(retired)
            os.chmod(retired, 0o600)
        WALLET_PATH.write_text(json.dumps({"public_key": str(kp.pubkey()), "secret_key_b58": base58.b58encode(bytes(kp)).decode("utf-8")}, indent=2))
        os.chmod(WALLET_PATH, 0o600)
    _KEYPAIR = kp
    return old, retired
