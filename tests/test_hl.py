"""Тесты сборщика Hyperliquid, контракта load_hourly, бэктеста и paper — офлайн, на фикстурах формы ответов API.

dragon_core здесь подменён тестовой заглушкой (FakeCore): это проверка обвязки, а не стратегии.
"""
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

H = 3_600_000
REQ = ["open", "high", "low", "close", "volume", "oi", "funding", "liq_long", "liq_short", "cvd", "top_net"]

fake = types.ModuleType("dragon_core")
fake.REQUIRED_COLUMNS = REQ
fake.script = {}


def _step(df):
    d = fake.script.get(df.index[-1], "HOLD")
    return {"decision": d, "score": 1.0, "reasons": ["test"], "plan": None}


fake.step = _step
sys.modules.setdefault("dragon_core", fake)

import hl_collector as hc  # noqa: E402
import backtest as bt  # noqa: E402
import core_adapter as ca  # noqa: E402


def fill_db(path, coin="BTC", hours=10, t0=1_700_000_000_000 // H * H, cover=True, liq=True, top=True):
    db = hc.connect(path)
    for i in range(hours):
        t = t0 + i * H
        px = 100.0 + i
        db.execute("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?)", (coin, t, px, px + 1, px - 1, px, 10, 5))
        db.execute("INSERT INTO funding VALUES (?,?,?,?)", (coin, t, 0.0001, 0))
        db.execute("INSERT INTO oi_snap VALUES (?,?,?,?,?,?)", (coin, t + 60_000, 1, 1000 + i, px, 0.0001))
        if liq:
            db.execute("INSERT INTO liq VALUES (?,?,?,?,?)", (coin, t, 5.0, 3.0, "coinalyze"))
        if top:
            db.execute("INSERT INTO top_snap VALUES (?,?,?,?,?,?)", (coin, t + 120_000, 3, 1, 10, 0.5))
        db.execute("INSERT INTO flow VALUES (?,?,?,?,?)", (coin, t, 30.0, 10.0, 4))
        if cover:
            db.executemany("INSERT INTO ws_cover VALUES (?,?)", [(coin, t + m * 60_000) for m in range(60)])
    db.commit()
    db.close()
    return t0


class TestParsers(unittest.TestCase):
    def test_candles_funding(self):
        c = hc.parse_candles("BTC", [{"t": 1, "T": 2, "s": "BTC", "i": "1h", "o": "1", "c": "2", "h": "3", "l": "0.5",
                                      "v": "10", "n": 7}])
        self.assertEqual(c[0], ("BTC", 1, 1.0, 3.0, 0.5, 2.0, 10.0, 7))
        f = hc.parse_funding("ETH", [{"coin": "ETH", "fundingRate": "-0.0002", "premium": "-0.0005",
                                      "time": 1683849600076}])
        self.assertEqual(f[0][1], 1683849600000)

    def test_asset_ctxs(self):
        resp = [{"universe": [{"name": "BTC"}, {"name": "SOL"}, {"name": "ETH"}]},
                [{"openInterest": "2", "markPx": "100", "funding": "0.0001"},
                 {"openInterest": "9", "markPx": "1", "funding": "0"},
                 {"openInterest": "3", "markPx": "10", "funding": "-0.0001"}]]
        rows = hc.parse_asset_ctxs(resp, ("BTC", "ETH"), 5)
        self.assertEqual(rows, [("BTC", 5, 2.0, 200.0, 100.0, 0.0001), ("ETH", 5, 3.0, 30.0, 10.0, -0.0001)])

    def test_top_net(self):
        board = {"leaderboardRows": [
            {"ethAddress": f"0x{i}", "windowPerformances": [["day", {"pnl": "0"}], ["month", {"pnl": str(i)}]]}
            for i in range(60)]}
        top = hc.top_wallets(board, 50)
        self.assertEqual(len(top), 50)
        self.assertEqual(top[0], "0x59")
        states = [{"assetPositions": [{"position": {"coin": "BTC", "szi": "1", "positionValue": "300"}},
                                      {"position": {"coin": "ETH", "szi": "-2", "positionValue": "50"}}]},
                  {"assetPositions": [{"position": {"coin": "BTC", "szi": "-1", "positionValue": "100"}}]}]
        net = hc.net_positions(states, ("BTC", "ETH"))
        self.assertEqual(net["BTC"], (300.0, 100.0, 2))
        self.assertAlmostEqual(hc.top_net_value(300, 100), 0.5)
        self.assertEqual(hc.top_net_value(0, 50), -1.0)
        self.assertEqual(hc.top_net_value(0, 0), 0.0)

    def test_cvd_by_aggressor(self):
        fl = hc.trades_to_flow([{"coin": "BTC", "side": "B", "px": "100", "sz": "2", "time": 60_001},
                                {"coin": "BTC", "side": "A", "px": "100", "sz": "0.5", "time": 60_500}])
        self.assertEqual(fl[("BTC", 60_000)], [200.0, 50.0, 2])

    def test_ws_frame_partial(self):
        ws = hc.MiniWS.__new__(hc.MiniWS)
        frame = bytes([0x81, 5]) + b"hello"
        ws.buf = frame[:4]
        self.assertIsNone(ws._frame())  # неполный кадр не потребляется
        self.assertEqual(ws.buf, frame[:4])
        ws.buf = frame + bytes([0x81, 1])
        self.assertEqual(ws._frame(), (0x80, 1, b"hello"))
        self.assertEqual(ws.buf, bytes([0x81, 1]))


class TestHistory(unittest.TestCase):
    def test_backfill_and_funding_pagination(self):
        now = 10_000 * H
        depth = 7000  # «биржа» хранит 7000 последних свечей и funding с часа 0

        def fake_info(body):
            if body["type"] == "candleSnapshot":
                r = body["req"]
                lo = max(r["startTime"], now - depth * H)
                ts = [t for t in range(lo // H * H, r["endTime"], H) if t >= lo][-5000:]
                return [{"t": t, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1", "n": 1} for t in ts]
            if body["type"] == "fundingHistory":
                ts = [t for t in range(0, now, H) if t >= body["startTime"]][:500]
                return [{"coin": body["coin"], "fundingRate": "0.0001", "premium": "0", "time": t + 37} for t in ts]
            raise AssertionError(body)

        orig = hc.info
        hc.info = fake_info
        try:
            db = hc.connect(os.path.join(tempfile.mkdtemp(), "h.db"))
            hc.backfill_candles(db, "BTC", now_ms=now)
            hc.update_funding(db, "BTC")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candles").fetchone()[0], depth)
            self.assertEqual(db.execute("SELECT COUNT(*), MIN(t) FROM funding").fetchone(), (10_000, 0))
            self.assertEqual(hc.update_funding(db, "BTC"), 0)  # повтор не тянет заново
        finally:
            hc.info = orig


class TestContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")

    def test_exact_columns_and_values(self):
        t0 = fill_db(self.db)
        df = hc.load_hourly("btc", self.db, required=REQ)
        self.assertEqual(list(df.columns), REQ)
        self.assertEqual(len(df), 10)
        self.assertEqual(df.index[0], pd.Timestamp(t0, unit="ms", tz="UTC"))
        self.assertTrue(df.notna().all().all())
        self.assertEqual(df["cvd"].iloc[-1], 200.0)  # 10 часов × (30−10)
        self.assertEqual(df["oi"].iloc[0], 1000)

    def test_missing_sources_are_nan_not_filled(self):
        fill_db(self.db, cover=False, liq=False, top=False)
        df = hc.load_hourly("BTC", self.db, required=REQ)
        self.assertTrue(df["cvd"].isna().all())
        self.assertTrue(df["liq_long"].isna().all())
        self.assertTrue(df["top_net"].isna().all())
        st = hc.completeness(self.db, ("BTC",))["BTC"]
        self.assertIsNone(st["__all__"])

    def test_partial_ws_coverage_hour_is_nan(self):
        t0 = fill_db(self.db, hours=3)
        db = hc.connect(self.db)
        db.execute("DELETE FROM ws_cover WHERE minute >= ? AND minute < ?", (t0 + H, t0 + H + 5 * 60_000))
        db.commit()
        db.close()
        df = hc.load_hourly("BTC", self.db, required=REQ)
        self.assertTrue(np.isnan(df["cvd"].iloc[1]))
        self.assertFalse(np.isnan(df["cvd"].iloc[2]))

    def test_unknown_column_fails_loudly(self):
        fill_db(self.db)
        with self.assertRaises(hc.ContractError):
            hc.load_hourly("BTC", self.db, required=REQ + ["whale_mood"])

    def test_aliases(self):
        fill_db(self.db)
        df = hc.load_hourly("BTC", self.db, required=["timestamp", "close", "open_interest", "funding_rate"])
        self.assertEqual(list(df.columns), ["timestamp", "close", "open_interest", "funding_rate"])


class TestBacktest(unittest.TestCase):
    def setUp(self):
        self.idx = pd.date_range("2026-01-01", periods=6, freq="h", tz="UTC")
        self.data = pd.DataFrame({c: 1.0 for c in REQ}, index=self.idx)
        self.px = pd.Series(100.0, index=self.idx)
        self.fund = pd.Series(0.0, index=self.idx)
        fake.script.clear()

    def test_flat_price_costs_only(self):
        fake.script[self.idx[0]] = "LONG"
        fake.script[self.idx[2]] = "FLAT"
        t, eq, _ = bt.simulate("BTC", self.data, self.px, self.fund, fee=0.00045, slip=0.0005)
        self.assertEqual(len(t), 1)
        expected = (100 * 0.9995) / (100 * 1.0005) - 1 - 2 * 0.00045
        self.assertAlmostEqual(t["net_ret"].iloc[0], expected, places=10)
        self.assertEqual(t["entry_time"].iloc[0], self.idx[1] + pd.Timedelta(hours=1))  # close бара t+1

    def test_no_lookahead_fill_next_close(self):
        px = pd.Series([100, 110, 120, 130, 140, 150.0], index=self.idx)
        fake.script[self.idx[0]] = "LONG"
        fake.script[self.idx[1]] = "FLAT"
        t, _, _ = bt.simulate("BTC", self.data, px, self.fund, fee=0, slip=0)
        self.assertAlmostEqual(t["net_ret"].iloc[0], 120 / 110 - 1)

    def test_short_pays_negative_funding(self):
        fake.script[self.idx[0]] = "SHORT"
        fake.script[self.idx[3]] = "FLAT"
        fund = pd.Series(-0.001, index=self.idx)
        t, _, _ = bt.simulate("BTC", self.data, self.px, fund, fee=0, slip=0)
        self.assertAlmostEqual(t["net_ret"].iloc[0], -0.003)  # 3 часа × шорт при отрицательной ставке

    def test_nan_rows_skipped(self):
        d = self.data.copy()
        d.iloc[0, 0] = np.nan
        fake.script[self.idx[0]] = "LONG"
        t, _, dec = bt.simulate("BTC", d, self.px, self.fund)
        self.assertTrue(t.empty)
        self.assertEqual(len(dec), 5)

    def test_report_small_sample_warning(self):
        tmp = tempfile.mkdtemp()
        tr = pd.DataFrame({"side": [1], "entry_hour_utc": [3], "net_ret": [0.01], "exit_reason": ["signal"],
                           "hold_h": [2.0]})
        p = os.path.join(tmp, "r.md")
        bt.write_report(p, tr, {}, {"BTC": (self.idx[0], self.idx[-1], 6)}, 0.00045, 0.0005)
        self.assertIn("меньше 30", open(p).read())

    def test_unknown_decision_raises(self):
        with self.assertRaises(ca.CoreShapeError):
            ca.normalize({"decision": "MAYBE"}, 0)
        self.assertEqual(ca.normalize({"action": "hold"}, -1)["target"], -1)
        self.assertEqual(ca.normalize({"signal": 1}, 0)["target"], 1)

    def test_cycles(self):
        tmp = tempfile.mkdtemp()
        idx = pd.date_range("2026-01-01", periods=24 * 14, freq="h", tz="UTC")
        f = pd.DataFrame({"liq_long_z": np.where(idx.hour == 14, 3.0, 0.0), "liq_short_z": 0.0, "cvd_chg": 0.0},
                         index=idx)
        p = os.path.join(tmp, "c.md")
        bt.write_cycles(p, {"BTC": (f, "test")})
        txt = open(p).read()
        self.assertIn("| 14 | 100.0% | 24.00", txt)


class TestEndToEnd(unittest.TestCase):
    def test_run_and_paper_tick(self):
        import paper
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "t.db")
        t0 = fill_db(db, hours=8)
        fake.script.clear()
        fake.script[pd.Timestamp(t0 + 2 * H, unit="ms", tz="UTC")] = "LONG"
        fake.script[pd.Timestamp(t0 + 5 * H, unit="ms", tz="UTC")] = "EXIT"
        trades, _ = bt.run(("BTC",), tmp, db)
        self.assertEqual(len(trades), 1)
        self.assertTrue(os.path.exists(os.path.join(tmp, "report.md")))
        self.assertTrue(os.path.exists(os.path.join(tmp, "cycles.md")))
        paper.LOG = os.path.join(tmp, "paper_log.csv")
        st = {}
        r = paper.tick("BTC", st, db_path=db)
        self.assertEqual(r["decision"], "HOLD")
        self.assertIsNone(paper.tick("BTC", st, db_path=db))  # тот же час второй раз не пишем
        paper.write_summary({"BTC": st}, os.path.join(tmp, "s.md"))
        self.assertIn("paper", open(os.path.join(tmp, "s.md")).read())

    def test_paper_matches_backtest(self):
        import paper
        tmp = tempfile.mkdtemp()
        t0 = fill_db(os.path.join(tmp, "b.db"), hours=8)
        fake.script.clear()
        fake.script[pd.Timestamp(t0 + 2 * H, unit="ms", tz="UTC")] = "LONG"
        fake.script[pd.Timestamp(t0 + 5 * H, unit="ms", tz="UTC")] = "EXIT"
        trades, _ = bt.run(("BTC",), tmp, os.path.join(tmp, "b.db"))
        paper.LOG = os.path.join(tmp, "pl.csv")
        st = {}
        for h in range(1, 9):  # paper видит данные по мере поступления
            db = os.path.join(tmp, f"p{h}.db")
            fill_db(db, hours=h, t0=t0)
            paper.tick("BTC", st, db_path=db)
        self.assertEqual(len(st["closed"]), 1)
        self.assertAlmostEqual(st["closed"][0]["net_ret"], trades["net_ret"].iloc[0], places=12)


if __name__ == "__main__":
    unittest.main()
