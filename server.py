#!/usr/bin/env python3
"""Run:  python3 server.py   ->  open http://localhost:8787

Paper trading only. No keys, no wallets, no real orders.
"""
import json
import os
import re
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from memescan.engine import Engine

ROOT = os.path.dirname(os.path.abspath(__file__))


def make_engine():
    if "--demo" not in sys.argv:
        return Engine()
    # Offline demo on synthetic data: shows the UI without network access.
    import memescan.config as mc, memescan.engine as me
    from memescan.config import Config
    from tests.fakes import FakeGecko, FakeRPC, pool, trades
    mc.DATA_DIR = me.DATA_DIR = os.path.join(ROOT, "data", "demo")
    g = FakeGecko()
    names = ["WIF", "POPCAT", "MEW", "GOAT", "PNUT", "MOODENG", "FWOG", "MICHI", "CHILLGUY", "FARTCOIN", "NEWRUG"]
    for i, n in enumerate(names):
        mint = (n.replace("I", "i").replace("O", "o") + "1" * 40)[:40] + "pump"  # base58-valid
        g.pools.append(pool(f"pool{n}", mint, n, price=0.01 * (i + 1), age_h=3 if n == "NEWRUG" else 24 * (5 + i)))
        g.trades_by_pool[f"pool{n}"] = trades(900, 45 - 3 * i, 600, 10 + 2 * i)
    from memescan.solana_util import b58encode
    rpc = FakeRPC(holders=[("LPvault", 300_000_000, False, None)] +
                  [(f"wallet{i}abcdefghijk", 20_000_000 - i * 1_000_000, True, "devfunder" if i < 2 else f"f{i}")
                   for i in range(12)], creator=b58encode(b"\x07" * 32), creator_amount=4_000_000)
    import random
    for i, n in enumerate(names):
        mint = g.pools[i]["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
        rpc.add_pool(f"pool{n}", mint, base=1_000_000_000, quote=500 * (i + 1))

    def walk():  # synthetic price moves: early coins trend up, later ones chop
        while True:
            for i, n in enumerate(names):
                k = f"ta_vq_pool{n}"
                rpc.balances[k] *= 1 + random.gauss(0.0006 - 0.0001 * i, 0.0015)
            time.sleep(0.5)
    threading.Thread(target=walk, daemon=True).start()
    return Engine(Config(scan_interval_sec=15), gecko=g, rpc=rpc)


ENGINE = make_engine()


def masked_config():
    d = ENGINE.cfg.public()
    d["solana_rpc_url"] = re.sub(r"(api[-_]?key=)[^&]+", r"\1***", d["solana_rpc_url"])
    return d


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str, ensure_ascii=False))

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(ROOT, "web", "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html")
        if u.path == "/api/state":
            return self._send(200, ENGINE.state_json())
        if u.path == "/api/config":
            return self._json(masked_config())
        if u.path == "/api/live":
            return self._json(ENGINE.live())
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/scan":
            ENGINE.trigger()
            return self._json({"ok": True})
        if u.path == "/api/holders":
            mint = q.get("mint", "")
            if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint):
                return self._json({"error": "bad mint"}, 400)
            return self._json(ENGINE.get_holders(mint, force=q.get("force") == "1"))
        if u.path == "/api/close":
            pos = ENGINE.close(q.get("mint", ""), "закрыто вручную")
            return self._json({"ok": bool(pos)})
        if u.path == "/api/config":
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            body.pop("solana_rpc_url", None) if "***" in str(body.get("solana_rpc_url", "")) else None
            ENGINE.cfg.update(body)
            ENGINE.cfg.save()
            ENGINE.rpc.url = ENGINE.cfg.solana_rpc_url
            ENGINE.ticker.interval = ENGINE.cfg.tick_interval_sec
            return self._json(masked_config())
        self._json({"error": "not found"}, 404)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = int(os.environ.get("PORT", args[0] if args else 8787))
    threading.Thread(target=ENGINE.run_forever, daemon=True).start()
    threading.Thread(target=ENGINE.ticker.run_forever, daemon=True).start()
    print(f"memescan (PAPER{' DEMO' if '--demo' in sys.argv else ''}) -> http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
