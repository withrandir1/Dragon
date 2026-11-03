"""Сборщик Hyperliquid для dragon_core: BTC и ETH → SQLite dragon.db.

Запуск:
    python3 hl_collector.py               # бесконечно: история один раз, потом снимок каждые 5 минут + лента сделок
    python3 hl_collector.py --once        # одна итерация снимков (без ленты), для проверки
    python3 hl_collector.py --status      # с какой даты какие колонки полные

Источники (только чтение, без ключей биржи, ничего не отправляет на биржу):
    candleSnapshot 1h       — история: максимум, что отдаёт API (последние ~5000 свечей ≈ 208 дней)
    fundingHistory          — история с листинга монеты, ежечасно
    metaAndAssetCtxs        — OI (снимок, истории в info API нет), текущий funding, mark
    ws trades               — лента сделок → CVD по агрессору (side "B" = покупатель-тейкер)
    leaderboard + clearinghouseState — позиции топ-50 → top_net = (L−S)/(L+S) по нотионалу
    Coinalyze (необязательно, COINALYZE_API_KEY) — ликвидации и история OI по Hyperliquid.
        В info API Hyperliquid публичной ленты ликвидаций нет. Без ключа liq_* = NaN.

CVD, top_net, ликвидации (без Coinalyze) копятся с момента запуска; прошлое = NaN.
Час CVD считается валидным, только если лента была подключена ≥ 58 из 60 минут.

load_hourly(symbol) → DataFrame строго по dragon_core.REQUIRED_COLUMNS.
"""
import argparse
import base64
import json
import logging
import math
import os
import random
import socket
import sqlite3
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DRAGON_DB", os.path.join(HERE, "dragon.db"))
INFO_URL = "https://api.hyperliquid.xyz/info"
WS_HOST = "api.hyperliquid.xyz"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
COINALYZE_URL = "https://api.coinalyze.net/v1"
SYMBOLS = ("BTC", "ETH")
SNAPSHOT_EVERY_S = 300
TOP_N = 50
LEADERBOARD_WINDOW = os.environ.get("DRAGON_LB_WINDOW", "month")  # ранжирование топ-50 по PnL окна
WS_MIN_COVERAGE = 58  # минут из 60
HOUR_MS = 3_600_000

log = logging.getLogger("hl_collector")

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (coin TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, n INTEGER,
    PRIMARY KEY (coin, t));
CREATE TABLE IF NOT EXISTS funding (coin TEXT, t INTEGER, rate REAL, premium REAL, PRIMARY KEY (coin, t));
CREATE TABLE IF NOT EXISTS oi_snap (coin TEXT, ts INTEGER, oi_coin REAL, oi_usd REAL, mark REAL, funding REAL,
    PRIMARY KEY (coin, ts));
CREATE TABLE IF NOT EXISTS oi_hist (coin TEXT, t INTEGER, oi_usd REAL, source TEXT, PRIMARY KEY (coin, t));
CREATE TABLE IF NOT EXISTS liq (coin TEXT, t INTEGER, long_usd REAL, short_usd REAL, source TEXT, PRIMARY KEY (coin, t));
CREATE TABLE IF NOT EXISTS flow (coin TEXT, minute INTEGER, buy_usd REAL, sell_usd REAL, n INTEGER,
    PRIMARY KEY (coin, minute));
CREATE TABLE IF NOT EXISTS ws_cover (coin TEXT, minute INTEGER, PRIMARY KEY (coin, minute));
CREATE TABLE IF NOT EXISTS top_snap (coin TEXT, ts INTEGER, long_usd REAL, short_usd REAL, wallets INTEGER,
    top_net REAL, PRIMARY KEY (coin, ts));
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


# ---------------------------------------------------------------- storage

def connect(path=None):
    db = sqlite3.connect(path or DB_PATH, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def meta_get(db, k, default=None):
    r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(db, k, v):
    db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, str(v)))


# ---------------------------------------------------------------- http

def _http(url, body=None, headers=None, tries=5, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json", "User-Agent": "dragon-collector/1"}
    hdr.update(headers or {})
    delay = 2.0
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdr, method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or i == tries - 1:
                raise
            wait = float(e.headers.get("Retry-After") or delay)
        except (urllib.error.URLError, socket.timeout, ConnectionError):
            if i == tries - 1:
                raise
            wait = delay
        time.sleep(wait + random.random())
        delay *= 2


def info(body):
    return _http(INFO_URL, body)


# ---------------------------------------------------------------- parsers (чистые, под тесты)

def parse_candles(coin, rows):
    return [(coin, int(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"]),
             int(r.get("n", 0))) for r in rows]


def parse_funding(coin, rows):
    # time приходит с хвостом в миллисекундах после часа — нормализуем к началу часа
    return [(coin, int(r["time"]) // HOUR_MS * HOUR_MS, float(r["fundingRate"]), float(r.get("premium") or 0))
            for r in rows]


def parse_asset_ctxs(resp, coins, ts):
    meta, ctxs = resp[0], resp[1]
    out = []
    for asset, ctx in zip(meta["universe"], ctxs):
        if asset["name"] in coins:
            oi = float(ctx["openInterest"])
            mark = float(ctx.get("markPx") or ctx.get("oraclePx"))
            out.append((asset["name"], ts, oi, oi * mark, mark, float(ctx["funding"])))
    return out


def top_wallets(board, n=TOP_N, window=LEADERBOARD_WINDOW):
    rows = board.get("leaderboardRows", [])

    def pnl(r):
        for name, perf in r.get("windowPerformances", []):
            if name == window:
                return float(perf.get("pnl") or 0)
        return float("-inf")

    return [r["ethAddress"] for r in sorted(rows, key=pnl, reverse=True)[:n]]


def net_positions(states, coins):
    """states: список ответов clearinghouseState. → {coin: (long_usd, short_usd, wallets_with_pos)}"""
    acc = {c: [0.0, 0.0, 0] for c in coins}
    for st in states:
        for ap in st.get("assetPositions", []):
            p = ap.get("position", {})
            c = p.get("coin")
            if c not in acc:
                continue
            szi = float(p.get("szi") or 0)
            val = abs(float(p.get("positionValue") or 0))
            if szi > 0:
                acc[c][0] += val
            elif szi < 0:
                acc[c][1] += val
            if szi:
                acc[c][2] += 1
    return {c: tuple(v) for c, v in acc.items()}


def top_net_value(long_usd, short_usd):
    tot = long_usd + short_usd
    return (long_usd - short_usd) / tot if tot > 0 else 0.0


def trades_to_flow(trades):
    """ws trades → {(coin, minute_ms): [buy_usd, sell_usd, n]}. side 'B' — агрессор покупатель."""
    out = {}
    for t in trades:
        k = (t["coin"], int(t["time"]) // 60_000 * 60_000)
        b = out.setdefault(k, [0.0, 0.0, 0])
        ntl = float(t["px"]) * float(t["sz"])
        if t["side"] == "B":
            b[0] += ntl
        else:
            b[1] += ntl
        b[2] += 1
    return out


# ---------------------------------------------------------------- history + snapshots

def backfill_candles(db, coin, now_ms=None):
    """Тянем 1h свечи назад, пока API отдаёт новые (у Hyperliquid лимит ~5000 последних)."""
    now_ms = now_ms or int(time.time() * 1000)
    end, total = now_ms, 0
    while True:
        start = end - 5000 * HOUR_MS
        rows = info({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": start,
                                                        "endTime": end}})
        if not rows:
            break
        parsed = parse_candles(coin, rows)
        db.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)", parsed)
        total += len(parsed)
        first = min(r[1] for r in parsed)
        if first >= end or len(parsed) < 10:
            break
        end = first - 1
        time.sleep(0.3)
    db.commit()
    return total


def update_candles(db, coin, hours=6):
    now = int(time.time() * 1000)
    rows = info({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h",
                                                    "startTime": now - hours * HOUR_MS, "endTime": now}})
    db.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)", parse_candles(coin, rows or []))


def update_funding(db, coin, start_ms=None):
    last = db.execute("SELECT MAX(t) FROM funding WHERE coin=?", (coin,)).fetchone()[0]
    start = (last + 1) if last else (start_ms or 0)
    total = 0
    while True:
        rows = info({"type": "fundingHistory", "coin": coin, "startTime": start})
        if not rows:
            break
        db.executemany("INSERT OR REPLACE INTO funding VALUES (?,?,?,?)", parse_funding(coin, rows))
        total += len(rows)
        nxt = max(int(r["time"]) for r in rows) + 1
        if nxt <= start or len(rows) < 500:
            break
        start = nxt
        time.sleep(0.3)
    db.commit()
    return total


def snapshot_ctx(db, coins=SYMBOLS):
    ts = int(time.time() * 1000)
    rows = parse_asset_ctxs(info({"type": "metaAndAssetCtxs"}), coins, ts)
    db.executemany("INSERT OR REPLACE INTO oi_snap VALUES (?,?,?,?,?,?)", rows)
    return rows


def snapshot_top(db, coins=SYMBOLS):
    ts = int(time.time() * 1000)
    addrs = top_wallets(_http(LEADERBOARD_URL))
    states = []
    for a in addrs:
        try:
            states.append(info({"type": "clearinghouseState", "user": a}))
        except Exception as e:  # один кошелёк не валит снимок
            log.warning("clearinghouseState %s: %s", a, e)
        time.sleep(0.1)
    if len(states) < len(addrs) * 0.9:
        log.warning("top_net: ответили %d из %d кошельков — снимок не пишем", len(states), len(addrs))
        return None
    res = net_positions(states, coins)
    rows = [(c, ts, l, s, w, top_net_value(l, s)) for c, (l, s, w) in res.items()]
    db.executemany("INSERT OR REPLACE INTO top_snap VALUES (?,?,?,?,?,?)", rows)
    return rows


# ---------------------------------------------------------------- Coinalyze (ликвидации, история OI)

class Coinalyze:
    def __init__(self, key):
        self.key = key
        self.symbols = None

    def get(self, path, **params):
        q = urllib.parse.urlencode(params)
        return _http(f"{COINALYZE_URL}/{path}?{q}", headers={"api_key": self.key})

    def resolve(self, coins):
        if self.symbols is None:
            ex = {e["code"]: e["name"] for e in self.get("exchanges")}
            out = {}
            for m in self.get("future-markets"):
                if "hyperliquid" in ex.get(m.get("exchange"), "").lower() and m.get("is_perpetual") \
                        and m.get("base_asset") in coins:
                    out.setdefault(m["base_asset"], m["symbol"])
            self.symbols = out
            log.info("Coinalyze символы Hyperliquid: %s", out)
        return self.symbols

    def history(self, path, coins, frm, to, **extra):
        syms = self.resolve(coins)
        if not syms:
            return {}
        rev = {v: k for k, v in syms.items()}
        resp = self.get(path, symbols=",".join(syms.values()), interval="1hour", **{"from": frm, "to": to}, **extra)
        return {rev[r["symbol"]]: r["history"] for r in resp if r["symbol"] in rev}


def update_coinalyze(db, cz, coins=SYMBOLS):
    to = int(time.time())
    for kind in ("liq", "oi"):
        last = db.execute(f"SELECT MAX(t) FROM {'liq' if kind == 'liq' else 'oi_hist'} WHERE source='coinalyze'"
                          ).fetchone()[0]
        frm = (last // 1000 - 3 * 3600) if last else to - 365 * 86400  # Coinalyze сам обрежет до своей глубины
        if kind == "liq":
            h = cz.history("liquidation-history", coins, frm, to, convert_to_usd="true")
            rows = [(c, int(x["t"]) * 1000, float(x["l"]), float(x["s"]), "coinalyze") for c, xs in h.items() for x in xs]
            db.executemany("INSERT OR REPLACE INTO liq VALUES (?,?,?,?,?)", rows)
        else:
            h = cz.history("open-interest-history", coins, frm, to, convert_to_usd="true")
            rows = [(c, int(x["t"]) * 1000, float(x["c"]), "coinalyze") for c, xs in h.items() for x in xs]
            db.executemany("INSERT OR REPLACE INTO oi_hist VALUES (?,?,?,?)", rows)
    db.commit()


# ---------------------------------------------------------------- websocket (stdlib, без зависимостей)

class MiniWS:
    """Минимальный клиент RFC 6455: текстовые кадры, ping/pong, close. Хватает для ленты Hyperliquid."""

    def __init__(self, host, path="/ws", timeout=60):
        raw = socket.create_connection((host, 443), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("ws handshake: соединение закрыто")
            head += chunk
        status, _, self.buf = head.partition(b"\r\n\r\n")
        if b" 101 " not in status.split(b"\r\n")[0]:
            raise ConnectionError(f"ws handshake: {status[:80]!r}")
        self.lock = threading.Lock()

    def _frame(self):
        """Разбор одного кадра из буфера без потребления, пока кадр не пришёл целиком (таймаут не рвёт кадр)."""
        b = self.buf
        if len(b) < 2:
            return None
        fin, op, n, i = b[0] & 0x80, b[0] & 0x0F, b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, i = struct.unpack(">H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, i = struct.unpack(">Q", b[2:10])[0], 10
        mask = None
        if b[1] & 0x80:
            if len(b) < i + 4:
                return None
            mask, i = b[i:i + 4], i + 4
        if len(b) < i + n:
            return None
        data, self.buf = b[i:i + n], b[i + n:]
        if mask:
            data = bytes(x ^ mask[k % 4] for k, x in enumerate(data))
        return fin, op, data

    def send(self, text, opcode=0x1):
        payload = text.encode() if isinstance(text, str) else text
        mask = os.urandom(4)
        n = len(payload)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.lock:
            self.sock.sendall(hdr + mask + masked)

    def recv(self):
        parts = []
        while True:
            fr = self._frame()
            if fr is None:
                chunk = self.sock.recv(65536)  # socket.timeout пробрасывается, буфер цел
                if not chunk:
                    raise ConnectionError("ws: соединение закрыто")
                self.buf += chunk
                continue
            fin, op, data = fr
            if op == 0x8:
                raise ConnectionError("ws: сервер закрыл соединение")
            if op == 0x9:
                self.send(data, opcode=0xA)
                continue
            if op == 0xA:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts).decode()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class TradeTape(threading.Thread):
    """Лента сделок BTC/ETH → поминутные покупки/продажи по агрессору + покрытие (минуты, когда лента была жива)."""

    def __init__(self, coins=SYMBOLS, db_path=None):
        super().__init__(daemon=True)
        self.coins, self.db_path = coins, db_path
        self.pending, self.covered = {}, set()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.connected_since = None

    def run(self):
        backoff = 2
        while not self.stop.is_set():
            ws = None
            try:
                ws = MiniWS(WS_HOST)
                for c in self.coins:
                    ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": c}}))
                log.info("лента сделок подключена")
                backoff = 2
                self.connected_since = None
                last_ping = last_msg = time.time()
                ws.sock.settimeout(30)
                while not self.stop.is_set():
                    if time.time() - last_ping > 30:
                        ws.send(json.dumps({"method": "ping"}))
                        last_ping = time.time()
                    try:
                        msg = json.loads(ws.recv())
                    except socket.timeout:
                        msg = {}
                    now = time.time()
                    now_min = int(now * 1000) // 60_000 * 60_000
                    if now - last_msg > 90:
                        # тишина > 90с: лента могла зависнуть — эти минуты не считаем покрытыми
                        self.connected_since = None
                    if msg:
                        last_msg = now
                    else:
                        continue
                    if self.connected_since is None:
                        # первая неполная минута не считается покрытой
                        self.connected_since = now_min + 60_000
                    if msg.get("channel") == "trades":
                        flow = trades_to_flow(msg["data"])
                        with self.lock:
                            for k, v in flow.items():
                                p = self.pending.setdefault(k, [0.0, 0.0, 0])
                                p[0] += v[0]; p[1] += v[1]; p[2] += v[2]
                    with self.lock:
                        # минута покрыта, если лента была жива всю минуту: от connected_since до текущей
                        for m in range(self.connected_since, now_min, 60_000):
                            for c in self.coins:
                                self.covered.add((c, m))
                        self.connected_since = max(self.connected_since, now_min)
            except Exception as e:
                log.warning("лента сделок: %s — переподключение через %ss", e, backoff)
            finally:
                if ws:
                    ws.close()
            self.connected_since = None
            self.stop.wait(backoff)
            backoff = min(backoff * 2, 60)

    def flush(self, db):
        with self.lock:
            pending, covered = self.pending, self.covered
            self.pending, self.covered = {}, set()
        for (c, m), (b, s, n) in pending.items():
            db.execute("INSERT INTO flow VALUES (?,?,?,?,?) ON CONFLICT(coin, minute) DO UPDATE SET "
                       "buy_usd=buy_usd+excluded.buy_usd, sell_usd=sell_usd+excluded.sell_usd, n=n+excluded.n",
                       (c, m, b, s, n))
        db.executemany("INSERT OR IGNORE INTO ws_cover VALUES (?,?)", list(covered))
        db.commit()
        return len(pending), len(covered)


# ---------------------------------------------------------------- loop

def history_once(db):
    for c in SYMBOLS:
        n = backfill_candles(db, c)
        f = update_funding(db, c)
        log.info("%s: история свечей 1h — %d, funding — %d", c, n, f)
    if not meta_get(db, "collector_started_ms"):
        meta_set(db, "collector_started_ms", int(time.time() * 1000))
    db.commit()


def snapshot_once(db, cz=None):
    for name, fn in (("candles", lambda: [update_candles(db, c) for c in SYMBOLS]),
                     ("funding", lambda: [update_funding(db, c) for c in SYMBOLS]),
                     ("oi", lambda: snapshot_ctx(db)),
                     ("top_net", lambda: snapshot_top(db)),
                     ("coinalyze", lambda: cz and update_coinalyze(db, cz))):
        try:
            fn()
        except Exception as e:  # один источник упал — остальные пишем, час потом будет NaN по этой колонке
            log.warning("снимок %s: %s", name, e)
    meta_set(db, "last_snapshot_ms", int(time.time() * 1000))
    db.commit()


def run(once=False):
    db = connect()
    key = os.environ.get("COINALYZE_API_KEY")
    cz = Coinalyze(key) if key else None
    if not cz:
        log.warning("COINALYZE_API_KEY не задан: ликвидаций в публичном API Hyperliquid нет → liq_* = NaN, "
                    "OI только со снимков с момента запуска")
    history_once(db)
    tape = None
    if not once:
        tape = TradeTape()
        tape.start()
    while True:
        t0 = time.time()
        snapshot_once(db, cz)
        if tape:
            tape.flush(db)
        log_status(db)
        if once:
            return
        time.sleep(max(5.0, SNAPSHOT_EVERY_S - (time.time() - t0)))


# ---------------------------------------------------------------- hourly view + contract

def _hourly_frame(db, coin, include_open_bar=False):
    import numpy as np
    import pandas as pd

    def q(sql, *args):
        return pd.read_sql_query(sql, db, params=args)

    c = q("SELECT t, o AS open, h AS high, l AS low, c AS close, v AS volume, n AS trades FROM candles "
          "WHERE coin=? ORDER BY t", coin)
    if c.empty:
        return pd.DataFrame()
    df = c.set_index("t")
    df["volume_usd"] = df["volume"] * df["close"]

    f = q("SELECT t, rate AS funding FROM funding WHERE coin=?", coin).groupby("t").last()
    df = df.join(f)

    # OI: снимки за час → последний в часе; поверх — история Coinalyze там, где снимков нет
    oi = q("SELECT ts, oi_usd FROM oi_snap WHERE coin=? ORDER BY ts", coin)
    oi["t"] = oi["ts"] // HOUR_MS * HOUR_MS
    oi_h = oi.groupby("t")["oi_usd"].last().rename("oi")
    oh = q("SELECT t, oi_usd FROM oi_hist WHERE coin=?", coin).groupby("t")["oi_usd"].last().rename("oi")
    df = df.join(oi_h.combine_first(oh) if not oh.empty else oi_h)

    lq = q("SELECT t, long_usd AS liq_long, short_usd AS liq_short FROM liq WHERE coin=?", coin).groupby("t").last()
    df = df.join(lq)

    fl = q("SELECT minute, buy_usd, sell_usd FROM flow WHERE coin=?", coin)
    cov = q("SELECT minute FROM ws_cover WHERE coin=?", coin)
    if not cov.empty:
        cov["t"] = cov["minute"] // HOUR_MS * HOUR_MS
        ok = cov.groupby("t").size()
        ok = ok[ok >= WS_MIN_COVERAGE].index
        fl["t"] = fl["minute"] // HOUR_MS * HOUR_MS
        agg = fl.groupby("t")[["buy_usd", "sell_usd"]].sum().reindex(ok, fill_value=0.0)
        agg = agg.rename(columns={"buy_usd": "taker_buy_usd", "sell_usd": "taker_sell_usd"})
        agg["cvd_delta"] = agg["taker_buy_usd"] - agg["taker_sell_usd"]
        df = df.join(agg)
    else:
        for col in ("taker_buy_usd", "taker_sell_usd", "cvd_delta"):
            df[col] = np.nan
    # CVD-уровень: накопленная сумма от первого валидного часа; на дырах NaN (уровень после дыры не восстановить)
    df["cvd"] = df["cvd_delta"].cumsum().where(df["cvd_delta"].notna())

    ts = q("SELECT ts, top_net, long_usd, short_usd FROM top_snap WHERE coin=? ORDER BY ts", coin)
    ts["t"] = ts["ts"] // HOUR_MS * HOUR_MS
    df = df.join(ts.groupby("t")[["top_net"]].last())

    if not include_open_bar:
        now = int(time.time() * 1000)
        df = df[df.index + HOUR_MS <= now]
    df.index = pd.to_datetime(df.index, unit="ms", utc=True)
    df.index.name = "time"
    df["symbol"] = coin
    return df


# Синонимы: если dragon_core называет колонку иначе, достаточно дописать сюда.
ALIASES = {
    "timestamp": "time", "ts": "time", "datetime": "time", "date": "time",
    "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume",
    "open_interest": "oi", "oi_usd": "oi",
    "funding_rate": "funding", "fundingRate": "funding",
    "liq_long_usd": "liq_long", "liq_longs": "liq_long", "long_liq": "liq_long", "liquidations_long": "liq_long",
    "liq_short_usd": "liq_short", "liq_shorts": "liq_short", "short_liq": "liq_short",
    "liquidations_short": "liq_short",
    "cvd_change": "cvd_delta", "delta": "cvd_delta",
    "buy_volume": "taker_buy_usd", "sell_volume": "taker_sell_usd",
    "top_net_ratio": "top_net", "coin": "symbol",
}


class ContractError(ValueError):
    pass


def apply_contract(df, required):
    """Ровно REQUIRED_COLUMNS, в их порядке. Колонка, которую сборщик не умеет давать, — ошибка, а не тихий NaN."""
    df = df.reset_index()
    out, missing = {}, []
    for col in required:
        src = col if col in df.columns else ALIASES.get(col)
        if src is None or src not in df.columns:
            missing.append(col)
        else:
            out[col] = df[src]
    if missing:
        raise ContractError(f"сборщик не даёт колонки контракта: {missing}. Есть: {sorted(df.columns)}. "
                            f"Добавь синоним в hl_collector.ALIASES или источник.")
    import pandas as pd
    res = pd.DataFrame(out)
    res.index = pd.DatetimeIndex(df["time"], name="time")
    return res


def required_columns():
    import dragon_core
    return list(dragon_core.REQUIRED_COLUMNS)


def load_hourly(symbol, db_path=None, required=None, include_open_bar=False):
    """Часовые закрытые бары symbol (BTC/ETH) строго по контракту dragon_core.REQUIRED_COLUMNS.
    Индекс — начало часа UTC. Нет данных за час — NaN (никаких подстановок)."""
    db = connect(db_path)
    try:
        df = _hourly_frame(db, symbol.upper(), include_open_bar)
    finally:
        db.close()
    return apply_contract(df, required if required is not None else required_columns())


def completeness(db_path=None, symbols=SYMBOLS):
    """{symbol: {column: first_full_hour | None, '__all__': first hour from which every column is non-null}}"""
    db = connect(db_path)
    out = {}
    try:
        for s in symbols:
            df = _hourly_frame(db, s)
            cols = [c for c in ("open", "high", "low", "close", "volume", "oi", "funding", "liq_long", "liq_short",
                                "cvd_delta", "top_net") if c in df.columns]
            r = {}
            for c in cols:
                nn = df[c].notna()
                r[c] = df.index[nn.values].min() if nn.any() else None
            if not df.empty:
                full = df[cols].notna().all(axis=1)
                # первый час, начиная с которого колонки полные без дыр до конца
                tail_start = None
                for ts, ok in zip(reversed(df.index), reversed(full.values)):
                    if not ok:
                        break
                    tail_start = ts
                r["__all__"] = tail_start
                r["__rows_full__"] = int(full.sum())
            out[s] = r
    finally:
        db.close()
    return out


def log_status(db=None):
    st = completeness()
    for s, r in st.items():
        allf = r.get("__all__")
        msg = ", ".join(f"{k}≥{v:%Y-%m-%d %H:00}" if v is not None else f"{k}=нет"
                        for k, v in r.items() if not k.startswith("__"))
        log.info("%s: полные данные (все колонки) с %s; часов полных %s | %s", s,
                 f"{allf:%Y-%m-%d %H:00} UTC" if allf is not None else "— ещё нет",
                 r.get("__rows_full__", 0), msg)
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(HERE, "hl_collector.log"))])
    if a.status:
        log_status()
        return
    while True:
        try:
            run(once=a.once)
            return
        except KeyboardInterrupt:
            return
        except Exception as e:
            log.exception("сборщик упал: %s — перезапуск через 60с", e)
            time.sleep(60)


if __name__ == "__main__":
    main()
