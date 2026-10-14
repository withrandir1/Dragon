"""Thin HTTP clients: GeckoTerminal (pools, trades) and Solana JSON-RPC (holders).

Stdlib only, so `python3 server.py` works without installing anything.
"""
import json
import threading
import time
import urllib.request
import urllib.error

UA = {"User-Agent": "memescan/0.1", "Accept": "application/json"}


class SourceError(Exception):
    pass


def _http(url, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(UA)
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SourceError(f"HTTP {e.code} {url.split('?')[0]}") from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise SourceError(f"{type(e).__name__}: {e} ({url.split('?')[0]})") from e


class Gecko:
    """GeckoTerminal public API (free, ~30 req/min)."""

    BASE = "https://api.geckoterminal.com/api/v2/networks/solana"

    def __init__(self, min_interval=2.2, http=_http):
        self.min_interval = min_interval
        self._http = http
        self._lock = threading.Lock()
        self._last = 0.0

    def _get(self, path):
        with self._lock:
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
        for attempt in range(3):
            try:
                return self._http(self.BASE + path)
            except SourceError as e:
                if "HTTP 429" in str(e) and attempt < 2:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise

    def universe(self, pages=3):
        """Trending pools + top pools by 24h volume, deduplicated by pool address."""
        pools = {}
        paths = ["/trending_pools?include=base_token&page=1"]
        paths += [f"/pools?include=base_token&sort=h24_volume_usd_desc&page={p}" for p in range(1, pages + 1)]
        for path in paths:
            for p in self._get(path).get("data", []):
                pools[p["attributes"]["address"]] = p
        return list(pools.values())

    def trades(self, pool_address):
        """Last ~300 trades of a pool: kind buy/sell, volume_in_usd, tx_from_address, timestamp."""
        return self._get(f"/pools/{pool_address}/trades").get("data", [])

    def pool(self, pool_address):
        return self._get(f"/pools/{pool_address}").get("data")


class SolanaRPC:
    SYSTEM_PROGRAM = "11111111111111111111111111111111"

    def __init__(self, url, http=_http, min_interval=0.25):
        self.url = url
        self._http = http
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0
        self._id = 0

    def call(self, method, params):
        with self._lock:
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self._id += 1
            rid = self._id
        for attempt in range(3):
            try:
                res = self._http(self.url, {"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            except SourceError as e:
                if "HTTP 429" in str(e) and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise
            if "error" in res:
                raise SourceError(f"RPC {method}: {res['error'].get('message')}")
            return res["result"]

    def largest_accounts(self, mint):
        return self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])["value"]

    def token_supply(self, mint):
        return float(self.call("getTokenSupply", [mint])["value"]["uiAmount"] or 0)

    def mint_info(self, mint):
        v = self.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])["value"]
        return (v or {}).get("data", {}).get("parsed", {}).get("info", {})

    def multiple_accounts(self, addresses):
        out = []
        for i in range(0, len(addresses), 100):
            out += self.call("getMultipleAccounts", [addresses[i:i + 100], {"encoding": "jsonParsed"}])["value"]
        return out

    def oldest_signature(self, address, limit=1000):
        """Oldest signature within the last `limit`. Returns (sig, complete) — complete=False if wallet has more history."""
        sigs = self.call("getSignaturesForAddress", [address, {"limit": limit}])
        if not sigs:
            return None, True
        return sigs[-1]["signature"], len(sigs) < limit

    def transaction(self, sig):
        return self.call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
