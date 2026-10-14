import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memescan.config as config_mod
from memescan import strategy as S, whales as W
from memescan.config import Config
from memescan.solana_util import b58encode, find_program_address, PUMP_PROGRAM, is_on_curve, b58decode
from tests.fakes import FakeGecko, FakeRPC, pool, trades

MINT = "Meme111111111111111111111111111111111111pump"
NEW = "New1111111111111111111111111111111111111pump"
CREATOR = b58encode(b"\x07" * 32)


def clean_holders():
    # LP vault 30%, then 10 wallets 2.0% .. 1.1% (w0 biggest) with independent funders -> top-10 = 15.5%
    hs = [("LPvault", 300_000_000, False, None)]
    hs += [(f"w{i}", 20_000_000 - i * 1_000_000, True, f"funder{i}") for i in range(10)]
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

    def test_not_pump_excluded(self):
        p = S.normalize_pool(pool("p1", "Rand1111111111111111111111111111111111111111", "RAND", dex="raydium"))
        self.assertIn("не pump.fun", S.pool_filter(p, Config()))

    def test_bonding_curve_excluded_by_default(self):
        p = S.normalize_pool(pool("p1", MINT, "MEME", dex="pump-fun"))
        self.assertTrue(any("bonding curve" in r for r in S.pool_filter(p, Config())))
        self.assertEqual(S.pool_filter(p, Config(include_bonding_curve=True)), [])

    def test_stable_excluded(self):
        p = S.normalize_pool(pool("p1", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "USDC"))
        self.assertTrue(S.pool_filter(p, Config()))


class TestFlow(unittest.TestCase):
    def test_buy_pressure_entry(self):
        f = S.flow_metrics(trades(1000, 40, 500, 20))   # 40k buys vs 10k sells, 40 buyers
        self.assertAlmostEqual(f["15m"]["ratio"], 4.0)
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
        self.assertAlmostEqual(h["top10_pct"], 15.5)
        self.assertEqual(h["clusters"], [])
        self.assertEqual(S.holders_check(h, Config()), [])

    def test_cluster_detected(self):
        hs = clean_holders()
        hs = [(o, a, w, "devfunder" if o in ("w0", "w1", "w2", "w3", "w4", "w5", "w6", "w7", "w8", "w9") else f)
              for o, a, w, f in hs]
        h = W.analyze_holders(FakeRPC(holders=hs), MINT)
        self.assertEqual(len(h["clusters"]), 1)
        self.assertAlmostEqual(h["clusters"][0]["pct"], 15.5)
        self.assertEqual(h["clusters"][0]["funder"], "devfunder")
        cfg = Config(max_cluster_pct=10)
        self.assertTrue(any("кластер" in r for r in S.holders_check(h, cfg)))

    def test_chain_funding_clusters(self):
        hs = clean_holders()
        hs[1] = ("w0", 20_000_000, True, "w1")  # w1 funded w0 -> same cluster
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


class TestPumpCreator(unittest.TestCase):
    def test_pda_matches_known_pump_accounts(self):
        self.assertEqual(find_program_address([b"global"], PUMP_PROGRAM), "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
        self.assertEqual(find_program_address([b"mint-authority"], PUMP_PROGRAM), "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM")
        self.assertTrue(is_on_curve(b58decode("5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9")))

    def test_creator_found_and_flagged(self):
        h = W.analyze_holders(FakeRPC(holders=clean_holders(), creator=CREATOR, creator_amount=5_000_000), MINT)
        self.assertEqual(h["creator"], CREATOR)
        self.assertAlmostEqual(h["creator_pct"], 0.5)
        self.assertEqual(S.holders_check(h, Config()), [])
        roles = [w["role"] for w in W.snapshot(h)["wallets"]]
        self.assertEqual(roles[:4], ["top3", "top3", "top3", "other"])
        self.assertEqual(roles[-1], "creator")

    def test_big_creator_rejected(self):
        h = W.analyze_holders(FakeRPC(holders=clean_holders(), creator=CREATOR, creator_amount=150_000_000), MINT)
        self.assertTrue(any("создател" in r for r in S.holders_check(h, Config())))


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
        self.rpc = FakeRPC(holders=clean_holders(), creator=CREATOR, creator_amount=5_000_000)
        self.e = eng.Engine(Config(), gecko=self.g, rpc=self.rpc)

    def tearDown(self):
        config_mod.DATA_DIR, self.eng_mod.DATA_DIR = self._old

    def test_creator_sells_exit(self):
        self.e.scan_once()
        self.assertIn(MINT, self.e.positions)
        self.assertNotIn(NEW, [r["mint"] for r in self.e.rows])  # new coin filtered out
        self.rpc.balances[f"ta_{CREATOR}"] *= 0.85               # creator sells 15%
        self.e.scan_once()
        self.assertNotIn(MINT, self.e.positions)
        self.assertIn("создатель", self.e.closed[-1]["exit_reason"])

    def test_top3_sells_exit(self):
        self.e.scan_once()
        self.rpc.balances["ta_w2"] *= 0.7                         # #3 holder sells 30%
        self.e.scan_once()
        self.assertIn("топ-3", self.e.closed[-1]["exit_reason"])

    def test_non_top3_sale_ignored(self):
        self.e.scan_once()
        self.rpc.balances["ta_w5"] *= 0.3                         # #6 holder sells 70% -> not a rule
        self.e.scan_once()
        self.assertIn(MINT, self.e.positions)

    def test_hold_while_ratio_above_2x(self):
        self.e.scan_once()
        self.g.trades_by_pool["pMEME"] = trades(1000, 30, 1000, 12)   # x2.5 -> hold
        self.e.scan_once()
        self.assertIn(MINT, self.e.positions)

    def test_flow_equilibrium_exit_and_pnl(self):
        self.e.scan_once()
        self.g.pools[0] = pool("pMEME", MINT, "MEME", price=0.013)
        self.g.trades_by_pool["pMEME"] = trades(1000, 30, 1000, 17)   # x1.76 -> out
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
