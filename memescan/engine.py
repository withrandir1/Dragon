"""Scan loop + paper positions. One background thread, state exposed to the web UI."""
import json
import os
import threading
import time
import traceback

from . import economics as E
from . import strategy as S
from . import whales as W
from .research import Research
from .config import Config, DATA_DIR
from .sources import Gecko, SolanaRPC, SourceError
from .ticker import PriceTicker

HOLDERS_TTL = 15 * 60
PENDING_TTL = 120   # how long an entry signal waits for 30s of live price data


class Engine:
    def __init__(self, cfg: Config = None, gecko=None, rpc=None, dex=None):
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
        self.ticker = PriceTicker(self.rpc, self.cfg.tick_interval_sec, dex=dex)
        self.ticker.on_tick = self.on_tick
        self.research = Research(os.path.join(DATA_DIR, "research.jsonl"))
        self._wake = threading.Event()
        self._universe = (0.0, [])
        self._holders_busy = set()
        self._load()

    # ---------- persistence ----------
    def _state_path(self):
        return os.path.join(DATA_DIR, "state.json")

    def _load(self):
        try:
            with open(self._state_path()) as f:
                d = json.load(f)
            self.positions, self.closed = d.get("positions", {}), d.get("closed", [])
            for pos in self.positions.values():  # positions saved by older versions
                pos.setdefault("cost_pct", 3.0)
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
        if now - self._universe[0] >= cfg.universe_refresh_sec or not self._universe[1]:
            self._universe = (now, self.gecko.universe(cfg.universe_pages, dexes))
        raw = self._universe[1]
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
                p["flow_gecko"] = S.flow_metrics(self.gecko.trades(p["pool"]), time.time())
                p["flow"] = self.merged_flow(p)
                p["entry_missing"] = S.entry_check(p["flow"], cfg)
            except SourceError as e:
                p["flow"], p["entry_missing"] = None, [str(e)]

        # Hypothesis check: log every analyzed coin, fill forward returns from current prices
        for p in analyze.values():
            p["velocity"] = self.ticker.velocity(p["mint"])
            self.research.record(p, now)
        self.research.update({p["mint"]: p["price_usd"] for p in pools if p["price_usd"]}, now)

        # Entry signals -> economics -> holder check -> paper entry
        for p in analyze.values():
            if p.get("flow"):
                p["econ"] = E.entry_economics(p, self.research, cfg)
            p["signal"] = "watch"
            if p.get("flow") and not p["entry_missing"] and not p["filter_reasons"]:
                p["signal"] = "entry"
                if p["mint"] in self.positions:
                    p["signal"] = "holding"
                    continue
                econ_bad = E.reason(p["econ"], cfg)
                if econ_bad and not (p["econ"]["verdict"] == "no_stats" and cfg.trade_without_stats):
                    p["holder_reasons"] = [econ_bad]
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
        self.ticker.set_extra([a for pos in self.positions.values() for a in W.snapshot_accounts(pos["whale_snapshot"])])
        with self.lock:
            self.rows = rows[:10]
            self.universe_stats = {"pools": len(raw), "tokens": len(pools), "passed_filter": len(passed),
                                   "analyzed": len(analyze)}
            self.last_scan = time.time()
            self.last_error = None
        self._save()

    # ---------- live flow ----------
    def merged_flow(self, p):
        """GeckoTerminal flow, with 5m/15m ratio replaced by on-chain per-second flow once the coin
        has been watched long enough. Buyer counts / top-buyer share stay from GeckoTerminal."""
        g = p.get("flow_gecko")
        if not g:
            return None
        live = self.ticker.live_flow(p["mint"])
        price_q = self.ticker.price(p["mint"])
        q_usd = p["price_usd"] / price_q if price_q and p.get("price_usd") else None
        out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in g.items()}
        for w in ("5m", "15m"):
            out[w]["src"] = "gecko"
            if live and live[w]["covered"]:
                lw = live[w]
                out[w].update(ratio=lw["ratio"], buy_share=lw["buy_share"], src="live")
                if q_usd:
                    out[w].update(buy_usd=round(lw["buy_q"] * q_usd, 2), sell_usd=round(lw["sell_q"] * q_usd, 2))
        if live:
            out["1m_live"] = live["1m"]
        return out

    def _holders_async(self, mint):
        if mint in self._holders_busy:
            return
        self._holders_busy.add(mint)

        def run():
            try:
                self.get_holders(mint)
            finally:
                self._holders_busy.discard(mint)
        threading.Thread(target=run, daemon=True).start()

    def _realtime_entries(self):
        """Every second: a watched coin whose live flow now passes -> enter without waiting for a scan."""
        cfg = self.cfg
        if not cfg.auto_paper_trade:
            return
        for p in list(self.rows):
            m = p["mint"]
            if m in self.positions or m in self.pending or p.get("filter_reasons") or not p.get("flow_gecko"):
                continue
            if len(self.positions) >= cfg.max_open_positions:
                return
            flow = self.merged_flow(p)
            if flow["15m"].get("src") != "live" and flow["5m"].get("src") != "live":
                continue  # nothing new since the scan already judged this coin
            p["flow"] = flow
            if S.entry_check(flow, cfg):
                continue
            p["econ"] = E.entry_economics(p, self.research, cfg)
            bad = E.reason(p["econ"], cfg)
            if bad and not (p["econ"]["verdict"] == "no_stats" and cfg.trade_without_stats):
                continue
            cached = self.holders.get(m)
            if not cached or time.time() - cached[0] > HOLDERS_TTL:
                self._holders_async(m)
                continue
            h = cached[1]
            if h["reasons"]:
                p["signal"], p["holder_reasons"] = "rejected", h["reasons"]
                continue
            if self.momentum(self.ticker.velocity(m)) or not cfg.entry_require_momentum:
                self._open(p, h)
                p["signal"] = "holding"

    def _realtime_flow_exits(self, now=None):
        for mint, pos in list(self.positions.items()):
            live = self.ticker.live_flow(mint, now)
            if live and live["15m"]["covered"]:
                pos["ratio_15m"] = live["15m"]["ratio"]
                if live["15m"]["ratio"] < self.cfg.exit_ratio_15m:
                    self.close(mint, f"перевес ушёл (on-chain): ×{live['15m']['ratio']:.1f}")

    # ---------- momentum gate + live price ----------
    @staticmethod
    def momentum(vel):
        """True = rising on 15s and 30s, False = not rising, None = not enough live data yet."""
        if not vel or "v15" not in vel or vel.get("v15") is None or vel.get("v30") is None:
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
            pos["pnl_pct"] = S.paper_pnl(pos["entry_price"], pos["price"], pos["cost_pct"])
        for mint, pos in list(self.positions.items()):
            accs = W.snapshot_accounts(pos["whale_snapshot"])
            whales = None
            if accs and all(a in self.ticker.token_bal for a in accs):
                whales = W.compare_balances(pos["whale_snapshot"], self.ticker.token_bal)
                pos["whales"] = whales
            # stop + creator / top-3 / cluster selling, every second (flow exit stays per scan)
            reason = S.exit_check(pos, None, pos["price"], whales, self.cfg)
            if reason:
                self.close(mint, reason)
        self._realtime_flow_exits()
        self._realtime_entries()

    def live(self):
        with self.lock:
            pos = {m: {"price": p["price"], "pnl_pct": p["pnl_pct"], "whales": p.get("whales")}
                   for m, p in self.positions.items()}
        return {"velocity": self.ticker.all(), "positions": pos, "ticker_error": self.ticker.last_error,
                "pending": list(self.pending), "t": time.time()}

    def _open(self, p, h):
        econ = p.get("econ") or E.entry_economics(p, self.research, self.cfg)
        pos = {
            "mint": p["mint"], "pool": p["pool"], "symbol": p["symbol"],
            "entry_price": p["price_usd"], "entry_time": time.time(), "size_usd": econ["size_usd"],
            "cost_pct": econ["cost"]["total"], "econ": econ,
            "entry_ratio": p["flow"]["15m"]["ratio"], "creator": h.get("creator"), "price": p["price_usd"],
            "whale_snapshot": W.snapshot(h), "whales": None, "pnl_pct": 0.0,
            "entry_velocity": self.ticker.velocity(p["mint"]),
        }
        live = self.ticker.price(p["mint"])
        if live:
            pos["entry_tick"], pos["entry_tick_price_usd"] = live, p["price_usd"]
        with self.lock:
            self.positions[p["mint"]] = pos
        self._event("open", f"ВХОД {p['symbol']} ${econ['size_usd']:.0f} @ ${p['price_usd']:.8g} "
                            f"(×{p['flow']['15m']['ratio']:.1f}, издержки {econ['cost']['total']:.1f}%)",
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
            pos["pnl_pct"] = S.paper_pnl(pos["entry_price"], pos["price"], pos["cost_pct"])
            reason = S.exit_check(pos, flow, pos["price"], pos.get("whales"), self.cfg)
            if reason:
                self.close(mint, reason)

    def close(self, mint, reason):
        with self.lock:
            pos = self.positions.pop(mint, None)
            if not pos:
                return None
            pos.update(exit_time=time.time(), exit_price=pos["price"], exit_reason=reason,
                       pnl_pct=S.paper_pnl(pos["entry_price"], pos["price"], pos["cost_pct"]))
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
                "research": self.research.table(),
                "bankroll": self.cfg.bankroll_usd,
                "last_scan": self.last_scan,
                "last_error": self.last_error,
                "scanning": self.scanning,
                "mode": "PAPER",
            }
