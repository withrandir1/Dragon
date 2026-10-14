"""Offline demo engine on synthetic data: shows the UI without network access."""
import os
import random
import threading
import time

from .engine import Engine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_demo_engine():
    import memescan.config as mc, memescan.engine as me
    from memescan.config import Config
    from tests.fakes import FakeGecko, FakeRPC, pool, trades
    mc.DATA_DIR = me.DATA_DIR = os.path.join(ROOT, "data", "demo")
    g = FakeGecko()
    names = ["WIF", "POPCAT", "MEW", "GOAT", "PNUT", "MOODENG", "FWOG", "MICHI", "CHILLGUY", "FARTCOIN", "NEWRUG"]
    for i, n in enumerate(names):
        mint = (n.replace("I", "i").replace("O", "o") + "1" * 40)[:40] + "pump"  # base58-valid
        g.pools.append(pool(f"pool{n}", mint, n, price=0.01 * (i + 1), age_h=3 if n == "NEWRUG" else 24 * (5 + i)))
        g.trades_by_pool[f"pool{n}"] = trades(900, 45 - 3 * i, 600, 10 + 2 * i)
    from memescan.solana_util import b58encode
    rpc = FakeRPC(holders=[("LPvault", 300_000_000, False, None)] +
                  [(f"wallet{i}abcdefghijk", 20_000_000 - i * 1_000_000, True, "devfunder" if i < 2 else f"f{i}")
                   for i in range(12)], creator=b58encode(b"\x07" * 32), creator_amount=4_000_000)
    for i, n in enumerate(names):
        mint = g.pools[i]["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
        rpc.add_pool(f"pool{n}", mint, base=1_000_000_000, quote=500 * (i + 1))

    def walk():  # synthetic price moves: early coins trend up, later ones chop
        while True:
            for i, n in enumerate(names):
                k = f"ta_vq_pool{n}"
                rpc.balances[k] *= 1 + random.gauss(0.0006 - 0.0001 * i, 0.0015)
            time.sleep(0.5)
    threading.Thread(target=walk, daemon=True).start()
    return Engine(Config(scan_interval_sec=15), gecko=g, rpc=rpc)
