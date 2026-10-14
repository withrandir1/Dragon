"""Minimal Solana primitives (stdlib only): base58, PDA derivation, pump.fun bonding curve parsing."""
import hashlib

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def is_on_curve(b: bytes) -> bool:
    """Ed25519 point decompression check (what Solana uses to reject PDAs on the curve)."""
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    v = (_D * y2 + 1) % _P
    if v == 0:
        return False
    x2 = (y2 - 1) * pow(v, _P - 2, _P) % _P
    return x2 == 0 or pow(x2, (_P - 1) // 2, _P) == 1


def find_program_address(seeds, program_id: str) -> str:
    prog = b58decode(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not is_on_curve(h):
            return b58encode(h)
    raise ValueError("no PDA")


def bonding_curve_address(mint: str) -> str:
    return find_program_address([b"bonding-curve", b58decode(mint)], PUMP_PROGRAM)


def parse_bonding_curve(data: bytes):
    """BondingCurve: 8 discriminator, 5×u64 reserves/supply, bool complete, Pubkey creator."""
    if len(data) < 49:
        return None
    out = {"complete": bool(data[48])}
    out["creator"] = b58encode(data[49:81]) if len(data) >= 81 and any(data[49:81]) else None
    return out


def is_pump_mint(mint: str) -> bool:
    return mint.endswith("pump")
