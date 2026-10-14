"""Scan loop + paper positions. One background thread, state exposed to the web UI."""
import json
import os
import threading
import time
import traceback

from . import strategy as S
from . import whales as W
from .config import Config, DATA_DIR
from .sources import Gecko, SolanaRPC, SourceError
from .ticker import PriceTicker

HOLDERS_TTL = 15 * 60
PENDING_TTL = 120   # how long an entry signal waits for 30s of live price data


class Engine:
    def __init__(self, cfg: Config = None, gecko=None, rpc=None):
        self.cfg = cfg or Config.load()
        self.gecko = gecko or Gecko(self.cfg.gecko_min_interval_sec)
        self.rpc = rpc or SolanaRPC(self.cfg.solana_rpc_url)
        self.lock = threading.RLock()
        self.rows = []              # top-10 by buy pressure
        self.universe_stats = {}
        self.positions = {}         # mint -> position
        self.closed = []
        self.holders = {}           # mint -> (ts, analysis)
        self.events = []
        self.last_scan = None
        self.last_error = None
        self.scanning = False
        self.pending = {}           # mint -> {"p", "h", "until"}: entry waiting for live momentum
        self.ticker = PriceTicker(self.rpc, self.cfg.tick_interval_sec)
        self.ticker.on_tick = self.on_tick
        self._wake = threading.Event()
        self._load()

    # ---------- persistence ----------
    def _state_path(self):
        return os.path.join(DATA_DIR, "state.json")

    def _load(self):
        try:
            with open(self._state_path()) as f:
                d = json.load(f)
            self.positions, self.closed = d.get("positions", {}), d.get("closed", [])
        except (OSError, ValueError):
            pass

    def _save(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = self._state_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"positions": self.positions, "closed": self.closed[-500:]}, f, indent=1)
        os.replace(tmp, self._state_path())

    def _event(self, kind, text, **extra):
        e = {"t": time.time(), "kind": kind, "text": text, **extra}
        self.events = (self.events + [e])[-200:]
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "journal.jsonl"), "a") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    # ---------- holders ----------
    def get_holders(self, mint, force=False):
        cached = self.holders.get(mint)
        if cached and not force and time.time() - cached[0] < HOLDERS_TTL:
            return cached[1]
        h = W.analyze_holders(self.rpc, mint)
        h["reasons"] = S.holders_check(h, self.cfg)
        h["checked_at"] = time.time()
        with self.lock:
            self.holders[mint] = (time.time(), h)
        return h

    # ---------- one scan ----------
    def scan_once(self):
        cfg = self.cfg
        now = time.time()
        dexes = ["pumpswap"] + (["pump-fun"] if cfg.include_bonding_curve else []) if cfg.pump_only else []
        raw = self.gecko.universe(cfg.universe_pages, dexes)
        pools = S.dedupe_by_token([S.normalize_pool(p, now) for p in raw])
        for p in pools:
            p["filter_reasons"] = S.pool_filter(p, cfg)
        passed = [p for p in pools if not p["filter_reasons"]]
        ranked = sorted(passed, key=S.prerank_score, reverse=True)[:cfg.trades_fetch_top_n]

        by_mint = {p["mint"]: p for p in pools}
        analyze = {p["mint"]: p for p in ranked}
        for mint, pos in list(self.positions.items()):
            p = by_mint.get(mint)
            if p is None:
                try:
                    p = S.normalize_pool(self.gecko.pool(pos["pool"]), now)
                    p["filter_reasons"] = []
                except (SourceError, KeyError, TypeError):
                    continue
            analyze[mint] = p

        for p in analyze.values():
            try:
                p["flow"] = S.flow_metrics(self.gecko.trades(p["pool"]), time.time())
                p["entry_missing"] = S.entry_check(p["flow"], cfg)
            except SourceError as e:
                p["flow"], p["entry_missing"] = None, [str(e)]

        # Entry signals -> holder check -> paper entry
        for p in analyze.values():
            p["signal"] = "watch"
            if p.get("flow") and not p["entry_missing"] and not p["filter_reasons"]:
                p["signal"] = "entry"
                if p["mint"] in self.positions:
                    p["signal"] = "holding"
                    continue
                h = self.get_holders(p["mint"])
                p["holder_reasons"] = h["reasons"]
                if h["reasons"]:
                    p["signal"] = "rejected"
                elif cfg.auto_paper_trade and len(self.positions) < cfg.max_open_positions:
                    p["signal"] = self._try_open(p, h)
            elif p["mint"] in self.positions:
                p["signal"] = "holding"

        self._manage_positions(analyze)

        rows = [p for p in analyze.values() if p.get("flow")]
        rows.sort(key=lambda p: (p["mint"] not in self.positions, -p["flow"]["15m"]["ratio"]))
        watch = {p["mint"]: p["pool"] for p in rows[:10]}
        watch.update({m: pos["pool"] for m, pos in self.positions.items()})
        watch.update({m: x["p"]["pool"] for m, x in self.pending.items()})
        self.ticker.set_watch(watch)
        with self.lock:
            self.rows = rows[:10]
            self.universe_stats = {"pools": len(raw), "tokens": len(pools), "passed_filter": len(passed),
                                   "analyzed": len(analyze)}
            self.last_scan = time.time()
            self.last_error = None
        self._save()

    # ---------- momentum gate + live price ----------
    @staticmethod
    def momentum(vel):
        """True = rising on 15s and 30s, False = not rising, None = not enough live data yet."""
        if not vel or vel.get("v15") is None or vel.get("v30") is None:
            return None
        return vel["v15"] > 0 and vel["v30"] > 0

    def _try_open(self, p, h):
        if not self.cfg.entry_require_momentum:
            self._open(p, h)
            return "holding"
        m = self.momentum(self.ticker.velocity(p["mint"]))
        if m is None:
            self.pending[p["mint"]] = {"p": p, "h": h, "until": time.time() + PENDING_TTL}
            return "pending"
        if m:
            self._open(p, h)
            return "holding"
        p["holder_reasons"] = ["цена сейчас не растёт (15с/30с)"]
        return "entry"

    def on_tick(self):
        """Called by the ticker every second: pending entries + live PnL / stop."""
        now = time.time()
        for mint, x in list(self.pending.items()):
            row = x["p"]  # same object as in self.rows -> UI badge updates immediately
            if mint in self.positions or now > x["until"] or len(self.positions) >= self.cfg.max_open_positions:
                self.pending.pop(mint, None)
                row["signal"] = "holding" if mint in self.positions else "entry"
                continue
            m = self.momentum(self.ticker.velocity(mint, now))
            if m is True:
                self.pending.pop(mint, None)
                self._open(row, x["h"])
                row["signal"] = "holding"
            elif m is False:
                self.pending.pop(mint, None)
                row["signal"] = "entry"
                row["holder_reasons"] = ["цена сейчас не растёт (15с/30с)"]
        for mint, pos in list(self.positions.items()):
            live = self.ticker.price(mint)
            if not live:
                continue
            if not pos.get("entry_tick"):
                pos["entry_tick"] = live
                pos["entry_tick_price_usd"] = pos["price"]
            pos["price"] = pos["entry_tick_price_usd"] * live / pos["entry_tick"]
            pos["pnl_pct"] = S.paper_pnl(pos["entry_price"], pos["price"], self.cfg.cost_per_side_pct)
            reason = S.exit_check(pos, None, pos["price"], None, self.cfg)  # stop only, every second
            if reason:
                self.close(mint, reason)

    def live(self):
        with self.lock:
            pos = {m: {"price": p["price"], "pnl_pct": p["pnl_pct"]} for m, p in self.positions.items()}
        return {"velocity": self.ticker.all(), "positions": pos, "ticker_error": self.ticker.last_error,
                "pending": list(self.pending), "t": time.time()}

    def _open(self, p, h):
        pos = {
            "mint": p["mint"], "pool": p["pool"], "symbol": p["symbol"],
            "entry_price": p["price_usd"], "entry_time": time.time(), "size_usd": self.cfg.position_usd,
            "entry_ratio": p["flow"]["15m"]["ratio"], "creator": h.get("creator"), "price": p["price_usd"],
            "whale_snapshot": W.snapshot(h), "whales": None, "pnl_pct": 0.0,
            "entry_velocity": self.ticker.velocity(p["mint"]),
        }
        live = self.ticker.price(p["mint"])
        if live:
            pos["entry_tick"], pos["entry_tick_price_usd"] = live, p["price_usd"]
        with self.lock:
            self.positions[p["mint"]] = pos
        self._event("open", f"ВХОД {p['symbol']} @ ${p['price_usd']:.8g} (покупки/продажи 15м ×{p['flow']['15m']['ratio']:.1f})",
                    mint=p["mint"])

    def _manage_positions(self, analyze):
        for mint, pos in list(self.positions.items()):
            p = analyze.get(mint)
            if pos.get("entry_tick") and self.ticker.price(mint):
                pass  # live on-chain price already maintained by on_tick
            elif p and p["price_usd"]:
                pos["price"] = p["price_usd"]
            flow = p.get("flow") if p else None
            if flow:
                pos["ratio_15m"] = flow["15m"]["ratio"]
            try:
                pos["whales"] = W.compare_snapshot(self.rpc, pos["whale_snapshot"])
            except (SourceError, KeyError, TypeError) as e:
                pos["whales_error"] = str(e)
            pos["pnl_pct"] = S.paper_pnl(pos["entry_price"], pos["price"], self.cfg.cost_per_side_pct)
            reason = S.exit_check(pos, flow, pos["price"], pos.get("whales"), self.cfg)
            if reason:
                self.close(mint, reason)

    def close(self, mint, reason):
        with self.lock:
            pos = self.positions.pop(mint, None)
            if not pos:
                return None
            pos.update(exit_time=time.time(), exit_price=pos["price"], exit_reason=reason,
                       pnl_pct=S.paper_pnl(pos["entry_price"], pos["price"], self.cfg.cost_per_side_pct))
            pos["pnl_usd"] = pos["size_usd"] * pos["pnl_pct"] / 100
            pos.pop("whale_snapshot", None)
            self.closed.append(pos)
        self._event("close", f"ВЫХОД {pos['symbol']}: {reason}, {pos['pnl_pct']:+.1f}% (${pos['pnl_usd']:+.2f})",
                    mint=mint)
        self._save()
        return pos

    # ---------- loop ----------
    def run_forever(self):
        while True:
            self.scanning = True
            try:
                self.scan_once()
            except Exception as e:  # keep the loop alive; show error in UI
                self.last_error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
            finally:
                self.scanning = False
            self._wake.wait(self.cfg.scan_interval_sec)
            self._wake.clear()

    def trigger(self):
        self._wake.set()

    def state_json(self):
        with self.lock:
            return json.dumps(self.snapshot_state(), default=str)

    def snapshot_state(self):
        with self.lock:
            closed = self.closed
            wins = [c for c in closed if c.get("pnl_usd", 0) > 0]
            return {
                "rows": self.rows,
                "positions": list(self.positions.values()),
                "closed": closed[-50:][::-1],
                "stats": {
                    **self.universe_stats,
                    "trades": len(closed),
                    "winrate": len(wins) / len(closed) if closed else None,
                    "pnl_usd": round(sum(c.get("pnl_usd", 0) for c in closed), 2),
                },
                "events": self.events[-40:][::-1],
                "last_scan": self.last_scan,
                "last_error": self.last_error,
                "scanning": self.scanning,
                "mode": "PAPER",
            }
