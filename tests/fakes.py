"""Offline fakes for GeckoTerminal and Solana RPC, shaped like the real responses."""
import time
from datetime import datetime, timezone

SYS = "11111111111111111111111111111111"
AMM = "AMMprogram1111111111111111111111111111111111"


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def pool(addr, mint, name, liq=500_000, dex="pumpswap", fdv=10_000_000, vol24=2_000_000, age_h=24 * 30, price=0.01,
         buys=200, sells=100, buyers=120, sellers=60, chg24=5.0, now=None):
    now = now or time.time()
    return {
        "attributes": {
            "address": addr, "name": f"{name} / SOL", "base_token_price_usd": str(price),
            "reserve_in_usd": str(liq), "fdv_usd": str(fdv), "market_cap_usd": None,
            "pool_created_at": iso(now - age_h * 3600),
            "volume_usd": {"h1": "200000", "h24": str(vol24)},
            "price_change_percentage": {"h1": "1", "h24": str(chg24)},
            "transactions": {"m15": {"buys": buys, "sells": sells, "buyers": buyers, "sellers": sellers}},
        },
        "relationships": {"base_token": {"data": {"id": f"solana_{mint}"}}, "dex": {"data": {"id": dex}}},
    }


def trades(buy_usd_each, n_buys, sell_usd_each, n_sells, now=None, span=600):
    now = now or time.time()
    out = []
    for i in range(n_buys):
        out.append({"attributes": {"kind": "buy", "volume_in_usd": str(buy_usd_each), "tx_from_address": f"buyer{i}",
                                   "block_timestamp": iso(now - span * i / max(n_buys, 1))}})
    for i in range(n_sells):
        out.append({"attributes": {"kind": "sell", "volume_in_usd": str(sell_usd_each), "tx_from_address": f"seller{i}",
                                   "block_timestamp": iso(now - span * i / max(n_sells, 1))}})
    return out


class FakeGecko:
    def __init__(self):
        self.pools = []
        self.trades_by_pool = {}

    def universe(self, pages=3, dexes=()):
        return self.pools

    def trades(self, pool_address):
        return self.trades_by_pool.get(pool_address, [])

    def pool(self, pool_address):
        return next(p for p in self.pools if p["attributes"]["address"] == pool_address)


class FakeRPC:
    """holders: list of (owner, amount, is_wallet, funder). One token account per owner: 'ta_<owner>'."""
    SYSTEM_PROGRAM = SYS

    def __init__(self, supply=1_000_000_000, holders=(), mint_authority=None, freeze_authority=None,
                 creator=None, creator_amount=0.0):
        self.supply = supply
        self.holders = list(holders)
        self.mint_authority = mint_authority
        self.freeze_authority = freeze_authority
        self.balances = {f"ta_{o}": amt for o, amt, _, _ in holders}
        self.creator = creator
        if creator and f"ta_{creator}" not in self.balances:
            self.balances[f"ta_{creator}"] = creator_amount
        self.calls = 0

    def mint_info(self, mint):
        return {"mintAuthority": self.mint_authority, "freezeAuthority": self.freeze_authority}

    def token_supply(self, mint):
        return self.supply

    def largest_accounts(self, mint):
        return [{"address": f"ta_{o}", "uiAmount": self.balances[f"ta_{o}"]} for o, *_ in self.holders]

    def multiple_accounts(self, addrs):
        self.calls += 1
        out = []
        for a in addrs:
            if a.startswith("ta_"):
                owner = a[3:]
                out.append({"data": {"parsed": {"info": {"owner": owner,
                                                         "tokenAmount": {"uiAmount": self.balances.get(a, 0)}}}}})
            else:
                h = next((h for h in self.holders if h[0] == a), None)
                out.append({"owner": SYS if h and h[2] else AMM})
        return out

    def account_bytes(self, address):
        if not self.creator:
            return None
        from memescan.solana_util import b58decode
        return bytes(8 + 40) + b"\x01" + b58decode(self.creator)

    def token_accounts_by_owner(self, owner, mint):
        a = f"ta_{owner}"
        return [(a, self.balances[a])] if a in self.balances else []

    def oldest_signature(self, address, limit=1000):
        return f"sig_{address}", True

    def transaction(self, sig):
        wallet = sig[4:]
        funder = next(h[3] for h in self.holders if h[0] == wallet)
        return {"transaction": {"message": {"accountKeys": [{"pubkey": funder}], "instructions": [
            {"program": "system", "parsed": {"type": "transfer",
                                             "info": {"source": funder, "destination": wallet, "lamports": 1}}}]}},
                "meta": {"innerInstructions": []}}
