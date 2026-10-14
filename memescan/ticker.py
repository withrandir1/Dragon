"""1-second price ticker -> growth speed over 1s / 15s / 30s ("inertia").

Source per pool, best first:
  chain       PumpSwap: vault addresses read from the pool account (verified by the mint
              stored in it); price = quote vault / base vault. One RPC call/sec for all pools.
  chain       other constant-product pools whose vaults are owned by the pool account.
  dexscreener anything else (Raydium, Meteora, ...): priceNative, one request per ~2s for all.
Price is in quote units (SOL); % speed doesn't need USD.
"""
import math
import threading
import time
from collections import deque

from .solana_util import b58decode, b58encode
from .sources import SourceError, DexScreener

PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
DEX_INTERVAL = 2.0
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


def pumpswap_vaults(data: bytes, mint: str):
    """PumpSwap Pool: 8 disc, u8 bump, u16 index, creator, base_mint, quote_mint, lp_mint,
    pool_base_token_account, pool_quote_token_account. Returns vaults only if base_mint == mint."""
    if len(data) < 203 or data[43:75] != b58decode(mint):
        return None
    return b58encode(data[139:171]), b58encode(data[171:203])


class PriceTicker:
    def __init__(self, rpc, interval=1.0, dex=None):
        self.rpc = rpc
        self.dex = dex or DexScreener()
        self.interval = interval
        self.lock = threading.Lock()
        self.watch = {}       # mint -> pool
        self.route = {}       # pool -> ("chain", base_vault, quote_vault) | ("dexscreener",)
        self.status = {}      # mint -> human-readable state shown in the UI
        self.samples = {}     # mint -> deque[(t, price)]
        self.last_error = None
        self._last_dex = 0.0
        self.on_tick = None   # callback after each tick

    def set_watch(self, mapping):
        with self.lock:
            self.watch = dict(mapping)
            for m in list(self.samples):
                if m not in self.watch:
                    del self.samples[m]
            for m in mapping:
                self.status.setdefault(m, "ищу пул…")

    def _discover(self, items):
        """items: [(mint, pool)] without a route yet. One batched RPC call + per-pool fallback."""
        try:
            raw = self.rpc.multiple_accounts_raw([p for _, p in items])
        except SourceError as e:
            self.last_error = f"RPC: {e}"
            for m, _ in items:
                self.status[m] = f"ошибка RPC: {e}"
            return
        for (mint, pool), acc in zip(items, raw):
            if acc and acc[0] == PUMPSWAP_PROGRAM:
                v = pumpswap_vaults(acc[1], mint)
                if v:
                    self.route[pool] = ("chain",) + v
                    continue
            try:
                base = self.rpc.token_accounts_by_owner(pool, mint)
                quote = self.rpc.token_accounts_by_owner(pool, WSOL) or self.rpc.token_accounts_by_owner(pool, USDC)
            except SourceError:
                base = quote = None
            if base and quote:
                self.route[pool] = ("chain", max(base, key=lambda x: x[1])[0], max(quote, key=lambda x: x[1])[0])
            else:
                self.route[pool] = ("dexscreener",)

    def _add(self, mint, now, price, src):
        dq = self.samples.setdefault(mint, deque())
        dq.append((now, price))
        while dq and now - dq[0][0] > HISTORY_SEC:
            dq.popleft()
        self.status[mint] = src

    def tick(self, now=None):
        with self.lock:
            watch = dict(self.watch)
        new = [(m, p) for m, p in watch.items() if p not in self.route]
        if new:
            self._discover(new)
        now = now or time.time()
        chain = [(m, self.route[p]) for m, p in watch.items() if self.route.get(p, ("",))[0] == "chain"]
        dex = [(m, p) for m, p in watch.items() if self.route.get(p, ("",))[0] == "dexscreener"]
        errors = []
        if chain:
            addrs = [a for _, r in chain for a in r[1:]]
            try:
                accs = self.rpc.multiple_accounts(addrs)
                bal = {}
                for a, acc in zip(addrs, accs):
                    try:
                        bal[a] = float(acc["data"]["parsed"]["info"]["tokenAmount"].get("uiAmount") or 0)
                    except (TypeError, KeyError):
                        bal[a] = 0.0
                with self.lock:
                    for mint, (_, bv, qv) in chain:
                        if bal.get(bv):
                            self._add(mint, now, bal[qv] / bal[bv], "chain")
                        else:
                            self.status[mint] = "пустое хранилище пула"
            except SourceError as e:
                errors.append(f"RPC: {e}")
                for m, _ in chain:
                    self.status[m] = f"ошибка RPC: {e}"
        if dex and now - self._last_dex >= DEX_INTERVAL:
            self._last_dex = now
            try:
                prices = self.dex.prices([p for _, p in dex])
                with self.lock:
                    for mint, pool in dex:
                        if prices.get(pool):
                            self._add(mint, now, prices[pool], "dexscreener")
                        else:
                            self.status[mint] = "DexScreener не знает пул"
            except SourceError as e:
                errors.append(f"DexScreener: {e}")
                for m, _ in dex:
                    self.status[m] = f"ошибка DexScreener: {e}"
        self.last_error = "; ".join(errors) or None

    def velocity(self, mint, now=None):
        with self.lock:
            s = list(self.samples.get(mint, ()))
            st = self.status.get(mint)
        v = velocity(s, now or time.time())
        if v is None:
            return {"status": st or "нет данных"} if st else None
        v["src"] = st if st in ("chain", "dexscreener") else None
        return v

    def price(self, mint):
        with self.lock:
            s = self.samples.get(mint)
            return s[-1][1] if s else None

    def all(self):
        now = time.time()
        with self.lock:
            mints = set(self.samples) | set(self.watch)
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
