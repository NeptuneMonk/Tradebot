"""Base58 for pubkeys. Same alphabet Solana uses; no third-party dependency."""
from __future__ import annotations

_ALPH = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPH)}


def b58encode(data: bytes) -> str:
    zeros = 0
    for b in data:
        if b == 0:
            zeros += 1
        else:
            break
    n = int.from_bytes(data, "big")
    chars: list[str] = []
    while n > 0:
        n, r = divmod(n, 58)
        chars.append(_ALPH[r])
    return "1" * zeros + "".join(reversed(chars))


def b58decode(text: str) -> bytes:
    if not text:
        raise ValueError("empty base58")
    zeros = 0
    for c in text:
        if c == "1":
            zeros += 1
        else:
            break
    n = 0
    for c in text:
        try:
            n = n * 58 + _INDEX[c]
        except KeyError as e:
            raise ValueError(f"invalid base58 {text!r}") from e
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * zeros + body


def pubkey_bytes(text: str) -> bytes:
    raw = b58decode(text)
    if len(raw) > 32:
        raise ValueError("pubkey longer than 32 bytes")
    return raw.rjust(32, b"\x00")


def is_pubkey(text: str) -> bool:
    try:
        return len(pubkey_bytes(text)) == 32 and bool(text)
    except Exception:
        return False
