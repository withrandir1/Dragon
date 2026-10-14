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

    def universe(self, pages=3, dexes=()):
        """Pools from given DEXes (e.g. pumpswap) + trending + top by 24h volume, deduped by address.

        A failing DEX endpoint (unknown id, 404) is skipped so the scan still runs.
        """
        pools, errors = {}, []
        paths = [f"/dexes/{d}/pools?include=base_token&sort=h24_volume_usd_desc&page={p}"
                 for d in dexes for p in range(1, pages + 1)]
        paths += ["/trending_pools?include=base_token&page=1"]
        paths += [f"/pools?include=base_token&sort=h24_volume_usd_desc&page={p}" for p in range(1, pages + 1)]
        for path in paths:
            try:
                data = self._get(path).get("data", [])
            except SourceError as e:
                errors.append(str(e))
                continue
            for p in data:
                pools[p["attributes"]["address"]] = p
        if not pools and errors:
            raise SourceError("; ".join(errors[:3]))
        return list(pools.values())

    def trades(self, pool_address):
        """Last ~300 trades of a pool: kind buy/sell, volume_in_usd, tx_from_address, timestamp."""
        return self._get(f"/pools/{pool_address}/trades").get("data", [])

    def pool(self, pool_address):
        return self._get(f"/pools/{pool_address}").get("data")


class DexScreener:
    """Fallback live price for pools we can't read on-chain (Raydium, Meteora, ...). ~300 req/min."""

    URL = "https://api.dexscreener.com/latest/dex/pairs/solana/"

    def __init__(self, http=_http):
        self._http = http

    def prices(self, pools):
        """{pool: price_native} for up to 30 pools per request."""
        out = {}
        for i in range(0, len(pools), 30):
            res = self._http(self.URL + ",".join(pools[i:i + 30]))
            for pr in res.get("pairs") or []:
                try:
                    out[pr["pairAddress"]] = float(pr["priceNative"])
                except (KeyError, TypeError, ValueError):
                    pass
        return out


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

    def account_bytes(self, address):
        import base64
        v = self.call("getAccountInfo", [address, {"encoding": "base64"}])["value"]
        return base64.b64decode(v["data"][0]) if v else None

    def token_accounts_by_owner(self, owner, mint):
        """[(token_account, ui_amount)] of `owner` for `mint`."""
        res = self.call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])["value"]
        return [(a["pubkey"], float(a["account"]["data"]["parsed"]["info"]["tokenAmount"].get("uiAmount") or 0))
                for a in res]

    def multiple_accounts_raw(self, addresses):
        """[(owner_program, bytes) | None] for each address (base64)."""
        import base64
        out = []
        for i in range(0, len(addresses), 100):
            for v in self.call("getMultipleAccounts", [addresses[i:i + 100], {"encoding": "base64"}])["value"]:
                out.append((v["owner"], base64.b64decode(v["data"][0])) if v else None)
        return out

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
