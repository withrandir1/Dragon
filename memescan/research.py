"""Hypothesis check: does a buy-$ imbalance actually predict growth?

Every scan we record each analyzed coin (one sample per coin per 5 min to avoid counting
the same move many times), then fill in its price change after 5 / 15 / 30 minutes.
Returns are grouped by the 15m buy/sell ratio at signal time, so you can see whether
x2+ coins really outperform x1-2 coins, and by how much after costs.
"""
import json
import math
import os
import threading

HORIZONS = (5, 15, 30)                     # minutes
BUCKETS = [(0, 1, "<×1"), (1, 2, "×1–2"), (2, 3, "×2–3"), (3, math.inf, "≥×3")]
SAMPLE_GAP_SEC = 300
GIVE_UP_SEC = 600                          # coin vanished from the data -> drop that horizon


def bucket_of(ratio):
    for lo, hi, label in BUCKETS:
        if lo <= ratio < hi:
            return label
    return BUCKETS[-1][2]


def trimmed_mean(xs, cut=0.1):
    """Mean without the extreme 10% on each side: memecoin outliers otherwise dominate."""
    xs = sorted(xs)
    k = int(len(xs) * cut)
    xs = xs[k:len(xs) - k] if len(xs) > 2 * k else xs
    return sum(xs) / len(xs) if xs else None


class Research:
    def __init__(self, path=None):
        self.path = path
        self.lock = threading.Lock()
        self.open = []
        self.done = []
        self.last_sample = {}
        if path and os.path.exists(path):
            with open(path) as f:
                for line in f:
                    try:
                        self.done.append(json.loads(line))
                    except ValueError:
                        pass

    def record(self, p, now):
        if not p.get("flow") or not p.get("price_usd"):
            return
        if now - self.last_sample.get(p["mint"], -math.inf) < SAMPLE_GAP_SEC:
            return
        self.last_sample[p["mint"]] = now
        f = p["flow"]
        with self.lock:
            self.open.append({
                "t": now, "mint": p["mint"], "symbol": p.get("symbol"), "price": p["price_usd"],
                "ratio15": round(f["15m"]["ratio"], 3), "ratio5": round(f["5m"]["ratio"], 3),
                "buy_usd15": f["15m"]["buy_usd"], "liq": p["liquidity_usd"],
                "inertia": (p.get("velocity") or {}).get("inertia"),
            })

    def update(self, prices, now):
        """prices: {mint: current price_usd}."""
        finished = []
        with self.lock:
            for s in self.open:
                for h in HORIZONS:
                    k = f"r{h}"
                    if k in s or now - s["t"] < h * 60:
                        continue
                    if prices.get(s["mint"]):
                        s[k] = round((prices[s["mint"]] / s["price"] - 1) * 100, 3)
                    elif now - s["t"] > h * 60 + GIVE_UP_SEC:
                        s[k] = None
                if all(f"r{h}" in s for h in HORIZONS):
                    finished.append(s)
            self.open = [s for s in self.open if s not in finished]
            self.done.extend(finished)
        if finished and self.path:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a") as f:
                for s in finished:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")

    def _returns(self, label, h):
        k = f"r{h}"
        with self.lock:
            rows = self.done + [s for s in self.open if s.get(k) is not None]
        return [s[k] for s in rows if s.get(k) is not None and bucket_of(s["ratio15"]) == label]

    def expected(self, ratio, horizon):
        xs = self._returns(bucket_of(ratio), horizon)
        return (round(trimmed_mean(xs), 2) if xs else None), len(xs)

    def table(self):
        out = []
        for _, _, label in BUCKETS:
            row = {"bucket": label}
            for h in HORIZONS:
                xs = self._returns(label, h)
                row[f"m{h}"] = round(trimmed_mean(xs), 2) if xs else None
                row[f"w{h}"] = round(sum(x > 0 for x in xs) / len(xs), 3) if xs else None
                row[f"n{h}"] = len(xs)
            out.append(row)
        return {"rows": out, "pending": len(self.open), "horizons": HORIZONS}
