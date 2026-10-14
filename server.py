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
    if "--demo" in sys.argv:
        from memescan.demo import build_demo_engine
        return build_demo_engine()
    return Engine()


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
