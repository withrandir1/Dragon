import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memescan.config as config_mod
from memescan import strategy as S, whales as W
from memescan.config import Config
from tests.fakes import FakeGecko, FakeRPC, pool, trades

MINT = "Meme1111111111111111111111111111111111111111"
NEW = "New11111111111111111111111111111111111111111"


def clean_holders():
    # LP vault 30%, then 10 wallets of 1.5% each with independent funders
    hs = [("LPvault", 300_000_000, False, None)]
    hs += [(f"w{i}", 15_000_000, True, f"funder{i}") for i in range(10)]
    return hs


class TestFilters(unittest.TestCase):
    def test_new_and_illiquid_rejected(self):
        cfg = Config()
        p = S.normalize_pool(pool("p1", NEW, "NEW", age_h=5, liq=20_000))
        reasons = S.pool_filter(p, cfg)
        self.assertTrue(any("новая" in r for r in reasons))
        self.assertTrue(any("ликвидность" in r for r in reasons))

    def test_good_pool_passes(self):
        p = S.normalize_pool(pool("p1", MINT, "MEME"))
        self.assertEqual(S.pool_filter(p, Config()), [])

    def test_stable_excluded(self):
        p = S.normalize_pool(pool("p1", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "USDC"))
        self.assertTrue(S.pool_filter(p, Config()))


class TestFlow(unittest.TestCase):
    def test_buy_pressure_entry(self):
        f = S.flow_metrics(trades(1000, 40, 500, 20))   # 40k buys vs 10k sells, 40 buyers
        self.assertAlmostEqual(f["15m"]["buy_share"], 0.8)
        self.assertEqual(S.entry_check(f, Config()), [])

    def test_single_whale_buyer_blocked(self):
        t = trades(100, 30, 100, 5) + [{"attributes": {"kind": "buy", "volume_in_usd": "50000",
                                                       "tx_from_address": "whale", "block_timestamp": trades(1, 1, 1, 0)[0]["attributes"]["block_timestamp"]}}]
        f = S.flow_metrics(t)
        self.assertTrue(any("один покупатель" in r for r in S.entry_check(f, Config())))

    def test_balanced_no_entry(self):
        f = S.flow_metrics(trades(1000, 30, 1000, 30))
        self.assertTrue(S.entry_check(f, Config()))

    def test_old_trades_ignored(self):
        f = S.flow_metrics(trades(1000, 40, 0, 0), now=time.time() + 3600)
        self.assertEqual(f["15m"]["buy_usd"], 0)


class TestHolders(unittest.TestCase):
    def test_lp_excluded_and_clean_passes(self):
        h = W.analyze_holders(FakeRPC(holders=clean_holders()), MINT)
        self.assertIsNone(h["error"])
        self.assertEqual(h["holders"][0]["kind"], "program/LP")
        self.assertAlmostEqual(h["top10_pct"], 15.0)
        self.assertEqual(h["clusters"], [])
        self.assertEqual(S.holders_check(h, Config()), [])

    def test_cluster_detected(self):
        hs = clean_holders()
        hs = [(o, a, w, "devfunder" if o in ("w0", "w1", "w2", "w3", "w4", "w5", "w6", "w7", "w8", "w9") else f)
              for o, a, w, f in hs]
        h = W.analyze_holders(FakeRPC(holders=hs), MINT)
        self.assertEqual(len(h["clusters"]), 1)
        self.assertAlmostEqual(h["clusters"][0]["pct"], 15.0)
        self.assertEqual(h["clusters"][0]["funder"], "devfunder")
        cfg = Config(max_cluster_pct=10)
        self.assertTrue(any("кластер" in r for r in S.holders_check(h, cfg)))

    def test_chain_funding_clusters(self):
        hs = clean_holders()
        hs[1] = ("w0", 15_000_000, True, "w1")  # w1 funded w0 -> same cluster
        h = W.analyze_holders(FakeRPC(holders=hs), MINT)
        self.assertEqual(sorted(h["clusters"][0]["wallets"]), ["w0", "w1"])

    def test_exchange_funder_not_clustered(self):
        cex = next(iter(W.EXCHANGE_WALLETS))
        hs = [(o, a, w, cex if w else f) for o, a, w, f in clean_holders()]
        h = W.analyze_holders(FakeRPC(holders=hs), MINT)
        self.assertEqual(h["clusters"], [])

    def test_whale_and_mint_authority_rejected(self):
        hs = clean_holders() + [("bigwhale", 200_000_000, True, "x")]
        h = W.analyze_holders(FakeRPC(holders=hs, mint_authority="dev"), MINT)
        r = S.holders_check(h, Config())
        self.assertTrue(any("mint authority" in x for x in r))
        self.assertTrue(any("крупнейший" in x for x in r))


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        import memescan.engine as eng
        self._old = (config_mod.DATA_DIR, eng.DATA_DIR)
        config_mod.DATA_DIR = eng.DATA_DIR = self.tmp
        self.eng_mod = eng
        self.g = FakeGecko()
        self.g.pools = [pool("pMEME", MINT, "MEME", price=0.01), pool("pNEW", NEW, "NEW", age_h=3)]
        self.g.trades_by_pool = {"pMEME": trades(1000, 40, 500, 20), "pNEW": trades(5000, 100, 10, 1)}
        self.rpc = FakeRPC(holders=clean_holders())
        self.e = eng.Engine(Config(), gecko=self.g, rpc=self.rpc)

    def tearDown(self):
        config_mod.DATA_DIR, self.eng_mod.DATA_DIR = self._old

    def test_entry_then_whale_exit(self):
        self.e.scan_once()
        self.assertIn(MINT, self.e.positions)
        self.assertNotIn(NEW, [r["mint"] for r in self.e.rows])  # new coin filtered out
        # a top wallet dumps 60% of its bag
        self.rpc.balances["ta_w3"] *= 0.4
        self.e.scan_once()
        self.assertNotIn(MINT, self.e.positions)
        self.assertIn("кит", self.e.closed[-1]["exit_reason"])

    def test_flow_equilibrium_exit_and_pnl(self):
        self.e.scan_once()
        self.g.pools[0] = pool("pMEME", MINT, "MEME", price=0.013)
        self.g.trades_by_pool["pMEME"] = trades(1000, 30, 1000, 30)
        self.e.scan_once()
        c = self.e.closed[-1]
        self.assertIn("перевес ушёл", c["exit_reason"])
        self.assertAlmostEqual(c["pnl_pct"], (1.3 * 0.985 ** 2 - 1) * 100, places=4)

    def test_stop_loss(self):
        self.e.scan_once()
        self.g.pools[0] = pool("pMEME", MINT, "MEME", price=0.008)
        self.e.scan_once()
        self.assertIn("стоп", self.e.closed[-1]["exit_reason"])

    def test_rejected_by_holders_no_position(self):
        self.rpc.mint_authority = "dev"
        self.e.scan_once()
        self.assertEqual(self.e.positions, {})
        self.assertEqual(self.e.rows[0]["signal"], "rejected")

    def test_state_persists(self):
        self.e.scan_once()
        e2 = self.eng_mod.Engine(Config(), gecko=self.g, rpc=self.rpc)
        self.assertIn(MINT, e2.positions)


if __name__ == "__main__":
    unittest.main()
