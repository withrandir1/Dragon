"""1-second on-chain price ticker -> growth speed over 1s / 15s / 30s ("inertia").

Price = quote vault balance / base vault balance of the pool (PumpSwap vaults are token
accounts owned by the pool account). One getMultipleAccounts call per second covers every
watched pool. Price is in quote units (SOL); % speed doesn't need USD.
"""
import math
import threading
import time
from collections import deque

from .sources import SourceError

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WINDOWS = (1, 15, 30)
WEIGHTS = {1: 0.2, 15: 0.4, 30: 0.4}
HISTORY_SEC = 65
TREND_EPS = 0.002     # %/s dead-band so noise doesn't flip accel/decel


def velocity(samples, now=None):
    """samples: [(t, price)] ascending. Returns % change over 1/15/30s, combined inertia (%/min), trend."""
    if not samples:
        return None
    now = now or samples[-1][0]
    t_last, p_last = samples[-1]
    out = {"price": p_last, "t": t_last}
    rates = {}
    for w in WINDOWS:
        target = t_last - w
        ref = None
        for t, p in reversed(samples[:-1]):
            if t <= target + 0.25:           # allow ±0.25s polling jitter
                ref = (t, p)
                break
        if ref is None or ref[1] <= 0 or t_last - ref[0] > w + 3:
            out[f"v{w}"] = None
            continue
        change = (p_last / ref[1] - 1) * 100
        out[f"v{w}"] = round(change, 3)
        rates[w] = math.log(p_last / ref[1]) * 100 / (t_last - ref[0])  # log-%/s: steady growth = equal rates
    if rates:
        wsum = sum(WEIGHTS[w] for w in rates)
        out["inertia"] = round(60 * sum(WEIGHTS[w] * r for w, r in rates.items()) / wsum, 3)  # %/min
    else:
        out["inertia"] = None
    if 15 in rates and 30 in rates:
        d = rates[15] - rates[30]
        out["trend"] = "accel" if d > TREND_EPS else ("decel" if d < -TREND_EPS else "flat")
    else:
        out["trend"] = None
    out["stale"] = now - t_last > 5
    return out


class PriceTicker:
    def __init__(self, rpc, interval=1.0):
        self.rpc = rpc
        self.interval = interval
        self.lock = threading.Lock()
        self.watch = {}       # mint -> pool
        self.vaults = {}      # pool -> (base_vault, quote_vault) | None (not supported)
        self.samples = {}     # mint -> deque[(t, price)]
        self.last_error = None
        self.on_tick = None   # callback(engine) after each tick

    def set_watch(self, mapping):
        with self.lock:
            self.watch = dict(mapping)
            for m in list(self.samples):
                if m not in self.watch:
                    del self.samples[m]

    def _discover(self, pool, mint):
        try:
            base = self.rpc.token_accounts_by_owner(pool, mint)
            quote = self.rpc.token_accounts_by_owner(pool, WSOL) or self.rpc.token_accounts_by_owner(pool, USDC)
        except SourceError as e:
            self.last_error = str(e)
            return None
        if not base or not quote:
            return False      # AMM whose vaults aren't owned by the pool account -> no live price
        return max(base, key=lambda x: x[1])[0], max(quote, key=lambda x: x[1])[0]

    def tick(self, now=None):
        with self.lock:
            watch = dict(self.watch)
        for mint, pool in watch.items():
            if pool not in self.vaults:
                v = self._discover(pool, mint)
                if v is None:
                    continue  # transient error, retry next tick
                self.vaults[pool] = v or None
        pairs = [(m, self.vaults[p]) for m, p in watch.items() if self.vaults.get(p)]
        if not pairs:
            return
        addrs = [a for _, v in pairs for a in v]
        try:
            accs = self.rpc.multiple_accounts(addrs)
        except SourceError as e:
            self.last_error = str(e)
            return
        now = now or time.time()
        bal = {}
        for a, acc in zip(addrs, accs):
            try:
                bal[a] = float(acc["data"]["parsed"]["info"]["tokenAmount"].get("uiAmount") or 0)
            except (TypeError, KeyError):
                bal[a] = 0.0
        with self.lock:
            for mint, (bv, qv) in pairs:
                if bal.get(bv):
                    dq = self.samples.setdefault(mint, deque())
                    dq.append((now, bal[qv] / bal[bv]))
                    while dq and now - dq[0][0] > HISTORY_SEC:
                        dq.popleft()
            self.last_error = None

    def velocity(self, mint, now=None):
        with self.lock:
            s = list(self.samples.get(mint, ()))
        return velocity(s, now or time.time())

    def price(self, mint):
        with self.lock:
            s = self.samples.get(mint)
            return s[-1][1] if s else None

    def all(self):
        now = time.time()
        with self.lock:
            mints = list(self.samples)
        return {m: self.velocity(m, now) for m in mints}

    def run_forever(self):
        while True:
            t0 = time.time()
            try:
                self.tick()
                if self.on_tick:
                    self.on_tick()
            except Exception as e:  # never kill the ticker
                self.last_error = f"{type(e).__name__}: {e}"
            time.sleep(max(0.05, self.interval - (time.time() - t0)))
