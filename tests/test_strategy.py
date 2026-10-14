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
from tests.fakes import FakeGecko, FakeRPC, FakeDex, pool, trades

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
        self.e = eng.Engine(Config(entry_require_momentum=False), gecko=self.g, rpc=self.rpc, dex=FakeDex())

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
        from memescan import economics as E
        cost = E.round_trip_cost(E.position_size(500_000, Config()), 500_000, Config())["total"]
        self.assertAlmostEqual(c["pnl_pct"], 30 - cost, places=4)
        self.assertAlmostEqual(c["size_usd"], 66.67)              # 1% of $1000 risked at a -15% stop

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


class TestVelocity(unittest.TestCase):
    def test_windows_and_trend(self):
        from memescan.ticker import velocity
        steady = velocity([(t, 1.001 ** t) for t in range(40)], 39)
        self.assertAlmostEqual(steady["v1"], 0.1, places=2)
        self.assertAlmostEqual(steady["v30"], (1.001 ** 30 - 1) * 100, places=2)
        self.assertEqual(steady["trend"], "flat")
        self.assertAlmostEqual(steady["inertia"], 60 * 100 * 0.0009995, places=1)  # ~6%/min
        accel = velocity([(t, 1.0 if t < 25 else 1.002 ** (t - 25)) for t in range(40)], 39)
        self.assertEqual(accel["trend"], "accel")
        stall = velocity([(t, 1.003 ** min(t, 30)) for t in range(40)], 39)
        self.assertEqual(stall["trend"], "decel")
        self.assertEqual(stall["v1"], 0.0)

    def test_not_enough_history(self):
        from memescan.ticker import velocity
        v = velocity([(t, 1.0) for t in range(10)], 9)
        self.assertIsNone(v["v15"])
        self.assertIsNone(v["v30"])
        self.assertEqual(v["v1"], 0.0)


class TestEconomics(unittest.TestCase):
    def test_size_and_cost(self):
        from memescan import economics as E
        cfg = Config()
        self.assertAlmostEqual(E.position_size(500_000, cfg), 66.67)          # risk cap
        self.assertAlmostEqual(E.position_size(10_000, cfg), 25.0)            # impact cap: 0.5% of $5K
        c = E.round_trip_cost(100, 50_000, cfg)
        self.assertAlmostEqual(c["fees"], 2.0)
        self.assertAlmostEqual(c["impact"], 0.8)                               # 2 * 100 / 25_000
        self.assertAlmostEqual(c["fixed"], 0.3)                                # 2 * 0.15 / 100
        self.assertAlmostEqual(c["total"], 3.1)

    def test_verdicts(self):
        from memescan import economics as E
        from memescan.research import Research
        cfg = Config(min_samples=3)
        r = Research()
        p = {"liquidity_usd": 500_000, "flow": {"15m": {"ratio": 2.5}}}
        self.assertEqual(E.entry_economics(p, r, cfg)["verdict"], "no_stats")
        r.done = [{"ratio15": 2.5, "r5": 1, "r15": x, "r30": 1} for x in (8, 9, 10)]
        e = E.entry_economics(p, r, cfg)
        self.assertEqual((e["verdict"], e["expected_pct"]), ("ok", 9.0))     # 9% >= 2 x 2.5%
        r.done = [{"ratio15": 2.5, "r5": 1, "r15": x, "r30": 1} for x in (1, 2, 3)]
        self.assertEqual(E.entry_economics(p, r, cfg)["verdict"], "weak")
        thin = {"liquidity_usd": 4_000, "flow": {"15m": {"ratio": 2.5}}}
        self.assertEqual(E.entry_economics(thin, r, cfg)["verdict"], "costly")


class TestResearch(unittest.TestCase):
    def test_forward_returns_by_bucket(self):
        from memescan.research import Research
        r = Research()
        p = {"mint": "A", "price_usd": 1.0, "liquidity_usd": 1, "flow": trades_flow(2.5)}
        r.record(p, 0)
        r.record(p, 60)                         # same coin within 5 min -> not double counted
        self.assertEqual(len(r.open), 1)
        r.update({"A": 1.10}, 5 * 60)
        r.update({"A": 1.20}, 15 * 60)
        r.update({"A": 0.90}, 30 * 60)
        self.assertEqual(len(r.done), 1)
        row = next(x for x in r.table()["rows"] if x["bucket"] == "×2–3")
        self.assertAlmostEqual(row["m5"], 10.0)
        self.assertAlmostEqual(row["m15"], 20.0)
        self.assertAlmostEqual(row["m30"], -10.0)
        self.assertEqual(r.expected(2.2, 15), (20.0, 1))

    def test_vanished_coin_dropped(self):
        from memescan.research import Research
        r = Research()
        r.record({"mint": "A", "price_usd": 1.0, "liquidity_usd": 1, "flow": trades_flow(1.5)}, 0)
        r.update({}, 31 * 60 + 600)
        self.assertEqual(r.done[0]["r30"], None)
        self.assertEqual(r.table()["rows"][1]["n30"], 0)


def trades_flow(ratio):
    return {"15m": {"ratio": ratio, "buy_usd": 1000}, "5m": {"ratio": ratio}}


class TestTickerRoutes(unittest.TestCase):
    def test_pumpswap_layout_parsed_and_verified(self):
        from memescan.ticker import PriceTicker, PUMPSWAP_PROGRAM, pumpswap_vaults
        mint = b58encode(b"\x09" * 32)   # real mints are 32-byte keys
        bv, qv = b58encode(b"\x01" * 32), b58encode(b"\x02" * 32)
        data = bytes(43) + b58decode(mint) + bytes(64) + b58decode(bv) + b58decode(qv) + bytes(40)
        self.assertEqual(pumpswap_vaults(data, mint), (bv, qv))
        self.assertIsNone(pumpswap_vaults(data, CREATOR))           # wrong mint -> layout not trusted
        rpc = FakeRPC()
        rpc.raw_accounts["poolPS"] = (PUMPSWAP_PROGRAM, data)
        rpc.balances[f"ta_{bv}"], rpc.balances[f"ta_{qv}"] = 1_000_000, 50.0
        # vault addresses from pool data are plain pubkeys; map them to fake token accounts
        rpc.multiple_accounts = lambda addrs: [{"data": {"parsed": {"info": {"tokenAmount": {
            "uiAmount": rpc.balances.get(f"ta_{a}", 0)}}}}} for a in addrs]
        t = PriceTicker(rpc, dex=FakeDex())
        t.set_watch({mint: "poolPS"})
        t.tick(now=1000)
        self.assertEqual(t.route["poolPS"][0], "chain")
        self.assertAlmostEqual(t.price(mint), 50.0 / 1_000_000)
        self.assertEqual(t.velocity(mint, 1000)["src"], "chain")

    def test_unknown_amm_falls_back_to_dexscreener(self):
        from memescan.ticker import PriceTicker
        dex = FakeDex()
        dex.price["poolRAY"] = 0.00042
        t = PriceTicker(FakeRPC(), dex=dex)
        t.set_watch({MINT: "poolRAY"})
        t.tick(now=1000)
        self.assertEqual(t.route["poolRAY"], ("dexscreener",))
        self.assertEqual(t.velocity(MINT, 1000)["src"], "dexscreener")
        t.tick(now=1000.5)                                     # throttled to every 2s
        self.assertEqual(dex.calls, 1)

    def test_status_before_data(self):
        from memescan.ticker import PriceTicker
        t = PriceTicker(FakeRPC(), dex=FakeDex())
        t.set_watch({MINT: "poolX"})
        self.assertEqual(t.velocity(MINT)["status"], "ищу пул…")
        t.tick(now=1000)
        self.assertEqual(t.velocity(MINT)["status"], "DexScreener не знает пул")


class TestLiveFlow(unittest.TestCase):
    def test_vault_deltas_to_buys_and_sells(self):
        from memescan.ticker import PriceTicker
        t = PriceTicker(FakeRPC(), dex=FakeDex())
        t.set_watch({MINT: "p"})
        t._flow(MINT, 0, 1000, 100)            # baseline
        t._flow(MINT, 1, 990, 102)             # tokens out, SOL in -> buy 2
        t._flow(MINT, 2, 995, 101)             # tokens in, SOL out -> sell 1
        t._flow(MINT, 3, 1100, 111)            # both up -> liquidity add, ignored
        f = t.live_flow(MINT, now=3)
        self.assertAlmostEqual(f["1m"]["buy_q"], 2)
        self.assertAlmostEqual(f["1m"]["sell_q"], 1)
        self.assertAlmostEqual(f["1m"]["ratio"], 2.0)
        self.assertFalse(f["5m"]["covered"])
        self.assertTrue(t.live_flow(MINT, now=301)["5m"]["covered"])


class TestMomentumAndLive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        import memescan.engine as eng
        self._old = (config_mod.DATA_DIR, eng.DATA_DIR)
        config_mod.DATA_DIR = eng.DATA_DIR = self.tmp
        self.eng_mod = eng
        self.g = FakeGecko()
        self.g.pools = [pool("pMEME", MINT, "MEME", price=0.01)]
        self.g.trades_by_pool = {"pMEME": trades(1000, 40, 500, 20)}
        self.rpc = FakeRPC(holders=clean_holders(), creator=CREATOR, creator_amount=5_000_000)
        self.rpc.add_pool("pMEME", MINT, base=1_000_000, quote=100.0)
        self.e = eng.Engine(Config(), gecko=self.g, rpc=self.rpc, dex=FakeDex())

    def tearDown(self):
        config_mod.DATA_DIR, self.eng_mod.DATA_DIR = self._old

    def feed(self, seconds, step, t0):
        for i in range(seconds):
            self.rpc.balances["ta_vq_pMEME"] *= step
            self.e.ticker.tick(now=t0 + i)

    def test_waits_for_live_momentum_then_enters(self):
        self.e.scan_once()
        self.assertEqual(self.e.rows[0]["signal"], "pending")
        self.assertEqual(self.e.positions, {})
        t0 = time.time() - 40
        self.feed(35, 1.001, t0)          # price rising ~0.1%/s
        self.e.on_tick()
        self.assertIn(MINT, self.e.positions)

    def test_falling_price_blocks_entry(self):
        t0 = time.time() - 40
        self.e.ticker.set_watch({MINT: "pMEME"})
        self.feed(35, 0.999, t0)          # falling
        self.e.scan_once()
        self.assertEqual(self.e.positions, {})
        self.assertIn("не растёт", self.e.rows[0]["holder_reasons"][0])

    def test_creator_exit_within_a_second(self):
        t0 = time.time() - 80
        self.e.ticker.set_watch({MINT: "pMEME"})
        self.feed(35, 1.001, t0)
        self.e.scan_once()
        self.assertIn(MINT, self.e.positions)
        self.e.ticker.tick(now=t0 + 36)                         # ticker now also reads whale balances
        self.e.on_tick()
        self.assertIn(MINT, self.e.positions)
        self.rpc.balances[f"ta_{CREATOR}"] *= 0.5                # creator dumps half
        self.e.ticker.tick(now=t0 + 37)
        self.e.on_tick()
        self.assertNotIn(MINT, self.e.positions)
        self.assertIn("создатель", self.e.closed[-1]["exit_reason"])

    def drive(self, seconds, buy_step, t0):
        """Simulate trades through the pool: buys push SOL in / tokens out."""
        for i in range(seconds):
            self.rpc.balances["ta_vq_pMEME"] *= buy_step
            self.rpc.balances["ta_vb_pMEME"] /= buy_step
            self.e.ticker.tick(now=t0 + i)

    def test_realtime_entry_without_scan(self):
        self.g.trades_by_pool["pMEME"] = trades(1000, 30, 1000, 30)   # GeckoTerminal: balanced, no entry
        self.e.scan_once()
        self.assertEqual(self.e.positions, {})
        self.e.get_holders(MINT)                                     # holders cached
        self.e.cfg.min_buy_usd_15m = 1_000                           # tiny fake pool
        self.drive(905, 1.0005, time.time() - 905)                   # 15 min of steady on-chain buying
        self.e._realtime_entries()
        self.assertIn(MINT, self.e.positions)

    def test_realtime_flow_exit(self):
        t0 = time.time() - 40
        self.e.ticker.set_watch({MINT: "pMEME"})
        self.drive(35, 1.001, t0)
        self.e.scan_once()
        self.assertIn(MINT, self.e.positions)
        # 15 min of mostly selling on-chain -> live 15m ratio < x2 -> exit without waiting for a scan
        for i in range(36, 960):
            step = 1.001 if i % 3 == 0 else 0.9995
            self.rpc.balances["ta_vq_pMEME"] *= step
            self.rpc.balances["ta_vb_pMEME"] /= step
            self.e.ticker.tick(now=t0 + i)
        self.e._realtime_flow_exits(now=t0 + 960)
        self.assertNotIn(MINT, self.e.positions)
        self.assertIn("on-chain", self.e.closed[-1]["exit_reason"])

    def test_live_stop_every_second(self):
        t0 = time.time() - 80
        self.e.ticker.set_watch({MINT: "pMEME"})
        self.feed(35, 1.001, t0)
        self.e.scan_once()
        self.assertIn(MINT, self.e.positions)
        self.rpc.balances["ta_vq_pMEME"] *= 0.8    # -20% dump within a second
        self.e.ticker.tick(now=t0 + 36)
        self.e.on_tick()
        self.assertNotIn(MINT, self.e.positions)
        self.assertIn("стоп", self.e.closed[-1]["exit_reason"])


def wallet_keys(n, seed=1):
    """n distinct on-curve (wallet-like) pubkeys."""
    import hashlib
    from memescan.solana_util import is_on_curve
    out, i = [], 0
    while len(out) < n:
        h = hashlib.sha256(f"{seed}-{i}".encode()).digest()
        if is_on_curve(h):
            out.append(b58encode(h))
        i += 1
    return out


class TestReplay(unittest.TestCase):
    def ev(self, t, signer, post, dtok, price):
        return {"t": t, "slot": t, "signer": signer, "post": post, "delta": {signer: dtok}, "dsol": 0,
                "dtok": dtok, "price": price}

    def test_signal_fires_when_top_holder_starts_selling(self):
        from memescan.replay import replay
        creator, whale, early1, user, other = wallet_keys(5)
        from memescan.solana_util import bonding_curve_address
        curve = bonding_curve_address(creator)  # a real PDA: off-curve -> not a wallet
        ev = [
            self.ev(0, creator, {creator: 20e6, curve: 980e6}, 20e6, 1.0),
            self.ev(10, early1, {early1: 30e6}, 30e6, 1.1),
            self.ev(20, whale, {whale: 80e6}, 80e6, 1.5),
            self.ev(30, user, {user: 5e6}, 5e6, 2.0),          # entry
            self.ev(60, other, {other: 1e6}, 1e6, 3.0),
            self.ev(90, other, {other: 2e6}, 1e6, 4.0),         # peak
            self.ev(100, whale, {whale: 50e6}, -30e6, 3.6),     # whale sells 37.5% -> signal
            self.ev(101, other, {other: 3e6}, 1e6, 3.5),
            self.ev(103, other, {other: 2e6}, -1e6, 3.2),
            self.ev(110, creator, {creator: 0}, -20e6, 1.0),    # dump
            self.ev(200, user, {user: 0}, -5e6, 0.8),           # user exits too late
        ]
        r = replay(ev, 3, user, creator)
        self.assertEqual(r["tracked"]["top3"][0], whale)
        self.assertEqual(r["signal"]["t"], 100)
        self.assertIn("топ-3", r["signal"]["reason"])
        self.assertEqual(r["exits"][0], 3.5)                 # next trade after the signal
        self.assertEqual(r["exits"][2], 3.2)                 # 2s later
        self.assertEqual(r["peak"], 4.0)
        self.assertEqual(r["user_exit"]["price"], 0.8)
        self.assertEqual(r["final"], 0.8)

    def test_parse_tx(self):
        from memescan.replay import parse_tx
        tx = {"blockTime": 5, "slot": 9, "transaction": {"message": {"accountKeys": [{"pubkey": "U"}]}},
              "meta": {"err": None, "preBalances": [2_000_000_000], "postBalances": [1_000_000_000],
                       "preTokenBalances": [], "postTokenBalances": [
                           {"mint": "M", "owner": "U", "uiTokenAmount": {"uiAmount": 1000.0}}]}}
        e = parse_tx(tx, "M")
        self.assertEqual((e["signer"], e["dtok"], e["dsol"]), ("U", 1000.0, -1.0))
        self.assertAlmostEqual(e["price"], 0.001)


if __name__ == "__main__":
    unittest.main()
