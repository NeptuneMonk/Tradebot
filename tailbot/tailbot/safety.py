"""Mint safety: block freeze authority and Token-2022 transfer-fee mints.

SPL mint (82 bytes): freeze-authority COption at offset 46.
Token-2022 mints with extensions are padded to 165 bytes, then a 1-byte account
type, then TLV entries. TransferFeeConfig is extension type 1.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MINT_LEN = 82
TOKEN2022_BASE = 165
TRANSFER_FEE_CONFIG = 1
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


@dataclass
class MintSafety:
    freeze: bool
    transfer_fee: bool
    decimals: int
    ok: bool
    reason: str = ""


def parse_mint(data: bytes) -> MintSafety:
    if len(data) < MINT_LEN:
        return MintSafety(False, False, 0, False, "mint account too short")
    decimals = data[44]
    freeze_tag = struct.unpack_from("<I", data, 46)[0]
    freeze = freeze_tag != 0
    fee = False
    if len(data) > TOKEN2022_BASE + 1:
        fee = TRANSFER_FEE_CONFIG in _tlv_types(data, TOKEN2022_BASE + 1)
    reason = ""
    if freeze:
        reason = "freeze authority"
    elif fee:
        reason = "transfer fee"
    return MintSafety(freeze, fee, int(decimals), not (freeze or fee), reason)


def _tlv_types(data: bytes, start: int) -> list[int]:
    types = []
    i = start
    # Walk type:u16, length:u16, value. Stop on a zero type (padding) or a truncated tail.
    while i + 4 <= len(data):
        typ, length = struct.unpack_from("<HH", data, i)
        if typ == 0:
            break
        if i + 4 + length > len(data):
            break
        types.append(typ)
        i += 4 + length
    return types
