"""Post-mortem of a real trade: would the "exit with the informed" signal have saved it?

    python3 -m memescan.replay <buy_tx_signature> [--hours 6] [--rps 4]

Rebuilds the token's on-chain history from creation to entry + N hours (pump.fun bonding
curve and PumpSwap alike; prices are implied by each trade's SOL / token change), finds who
the informed wallets were at the moment of entry (creator, top-3 holders, early buyers),
then replays every later transaction and reports when the exit signal would have fired and
what exiting 0-10 seconds after it would have returned, vs. the peak, your actual exit
and holding. Faster with your own RPC: SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=...
"""
import argparse
import csv
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .config import Config, DATA_DIR
from .solana_util import b58decode, is_on_curve
from .sources import SolanaRPC, SourceError
from . import whales as W

WSOL = "So11111111111111111111111111111111111111112"
LATENCIES = (0, 1, 2, 5, 10)
MIN_SOL_FOR_PRICE = 0.01


# ---------- pure part (unit-tested) ----------
def parse_tx(tx, mint):
    """Raw getTransaction(jsonParsed) -> {t, slot, signer, post: {owner: balance}, delta: {owner: d}, dsol, price}."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None
    keys = tx["transaction"]["message"]["accountKeys"]
    signer = keys[0]["pubkey"] if isinstance(keys[0], dict) else keys[0]
    pre, post = {}, {}
    for side, out in (("preTokenBalances", pre), ("postTokenBalances", post)):
        for b in meta.get(side) or []:
            if b.get("mint") == mint and b.get("owner"):
                out[b["owner"]] = out.get(b["owner"], 0.0) + float(b["uiTokenAmount"].get("uiAmount") or 0)
    owners = set(pre) | set(post)
    if not owners:
        return None
    delta = {o: post.get(o, 0.0) - pre.get(o, 0.0) for o in owners}
    dsol = (meta["postBalances"][0] - meta["preBalances"][0]) / 1e9
    dtok = delta.get(signer, 0.0)
    price = abs(dsol) / abs(dtok) if dtok and abs(dsol) >= MIN_SOL_FOR_PRICE else None
    return {"t": tx.get("blockTime") or 0, "slot": tx.get("slot", 0), "signer": signer,
            "post": {o: post.get(o, 0.0) for o in owners}, "delta": delta, "dsol": dsol,
            "dtok": dtok, "price": price}


def is_wallet(owner):
    """PDAs (bonding curve, AMM pools) are off the ed25519 curve; real wallets are on it."""
    try:
        key = b58decode(owner)
        return len(key) == 32 and is_on_curve(key)
    except (ValueError, IndexError):
        return False


def replay(events, entry_idx, user, creator, creator_pct=10, top3_pct=20, early_pct=20, n_early=10):
    """events: parsed txs in chronological order; entry_idx: index of the user's buy.
    Returns the informed set at entry, the first exit signal, exits with latency, peak, user's exit."""
    bal = {}
    early = []
    for e in events[:entry_idx + 1]:
        bal.update(e["post"])
        if e["dtok"] > 0 and e["signer"] not in (user, creator) and e["signer"] not in early:
            early.append(e["signer"])
    early = early[:n_early]
    entry = events[entry_idx]
    wallets = sorted((o for o, b in bal.items() if b > 0 and is_wallet(o) and o != user), key=lambda o: -bal[o])
    top3 = [o for o in wallets if o != creator][:3]
    tracked = {"creator": creator, "top3": top3, "early": early}
    at_entry = {o: bal.get(o, 0.0) for o in set(top3) | set(early) | ({creator} if creator else set())}
    early0 = sum(at_entry[o] for o in early)

    def last_price(i):
        for e in reversed(events[:i + 1]):
            if e["price"]:
                return e["price"]
        return None

    signal = None
    sells = []
    peak, peak_t = entry["price"] or last_price(entry_idx), entry["t"]
    user_exit = None
    for i in range(entry_idx + 1, len(events)):
        e = events[i]
        bal.update(e["post"])
        if e["price"] and e["price"] > (peak or 0):
            peak, peak_t = e["price"], e["t"]
        if e["signer"] == user and e["dtok"] < 0 and user_exit is None:
            user_exit = {"t": e["t"], "price": e["price"] or last_price(i)}
        for o, d in e["delta"].items():
            if d < 0 and o in at_entry and at_entry[o] > 0:
                role = "создатель" if o == creator else ("топ-3" if o in top3 else "ранний покупатель")
                sells.append({"t": e["t"], "who": role, "owner": o, "sold_pct": -d / at_entry[o] * 100,
                              "left_pct": bal.get(o, 0.0) / at_entry[o] * 100, "price": last_price(i)})
        if signal:
            continue
        reason = None
        if creator and at_entry.get(creator, 0) > 0 and bal.get(creator, 0) <= at_entry[creator] * (1 - creator_pct / 100):
            reason = f"создатель продал {100 - bal.get(creator, 0) / at_entry[creator] * 100:.0f}%"
        for o in top3:
            if not reason and at_entry[o] > 0 and bal.get(o, 0) <= at_entry[o] * (1 - top3_pct / 100):
                reason = f"топ-3 {o[:4]}… продал {100 - bal.get(o, 0) / at_entry[o] * 100:.0f}%"
        if not reason and early0 > 0:
            now_e = sum(bal.get(o, 0.0) for o in early)
            if now_e <= early0 * (1 - early_pct / 100):
                reason = f"ранние покупатели продали {100 - now_e / early0 * 100:.0f}%"
        if reason:
            signal = {"i": i, "t": e["t"], "reason": reason, "price": last_price(i)}

    exits = {}
    if signal:
        for lat in LATENCIES:
            j = next((k for k in range(signal["i"] + 1, len(events))
                      if events[k]["t"] >= signal["t"] + lat and events[k]["price"]), None)
            exits[lat] = events[j]["price"] if j is not None else last_price(len(events) - 1)
    return {"tracked": tracked, "at_entry": at_entry, "signal": signal, "exits": exits, "sells": sells,
            "peak": peak, "peak_t": peak_t, "user_exit": user_exit, "final": last_price(len(events) - 1)}


# ---------- network part ----------
def signatures(rpc, address, before=None, until=None, max_pages=50):
    out, cursor = [], before
    for _ in range(max_pages):
        opts = {"limit": 1000}
        if cursor:
            opts["before"] = cursor
        if until:
            opts["until"] = until
        page = rpc.call("getSignaturesForAddress", [address, opts])
        out += page
        if len(page) < 1000:
            return out, True
        cursor = page[-1]["signature"]
    return out, False


def fetch_txs(rpc, sigs, workers=4):
    def one(s):
        for attempt in range(3):
            try:
                return rpc.transaction(s)
            except SourceError:
                time.sleep(1 + attempt)
        return None
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, sigs))


def fmt_t(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "—"


def pct(a, b):
    return f"{(a / b - 1) * 100:+.1f}%" if a and b else "—"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("signature")
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--rps", type=float, default=4, help="RPC requests/sec (public ~4, Helius free ~10)")
    ap.add_argument("--max-pages", type=int, default=50)
    a = ap.parse_args(argv)
    cfg = Config.load()
    rpc = SolanaRPC(cfg.solana_rpc_url, min_interval=1 / a.rps)

    print("1/4 читаю твою транзакцию…", flush=True)
    tx = rpc.transaction(a.signature)
    if not tx:
        print("транзакция не найдена")
        return 1
    meta = tx["meta"]
    keys = tx["transaction"]["message"]["accountKeys"]
    user = keys[0]["pubkey"] if isinstance(keys[0], dict) else keys[0]
    deltas = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") == user and b.get("mint") != WSOL:
                deltas[b["mint"]] = deltas.get(b["mint"], 0.0) + sign * float(b["uiTokenAmount"].get("uiAmount") or 0)
    if not deltas:
        print("в транзакции нет изменения токенов у подписанта")
        return 1
    mint = max(deltas, key=lambda m: abs(deltas[m]))
    print(f"   токен {mint}, кошелёк {user}")

    print("2/4 история подписей токена…", flush=True)
    pre, pre_complete = signatures(rpc, mint, before=a.signature, max_pages=a.max_pages)
    post, _ = signatures(rpc, mint, until=a.signature, max_pages=a.max_pages)
    t_entry = tx.get("blockTime") or 0
    post_in = [s for s in post if s.get("blockTime") and s["blockTime"] <= t_entry + a.hours * 3600]
    if post and not post_in and min(s.get("blockTime") or 0 for s in post) > t_entry + a.hours * 3600:
        print(f"   токен слишком активный: за {a.max_pages}k подписей не дошли до окна, увеличь --max-pages")
    sigs = [s["signature"] for s in reversed(pre) if not s.get("err")] + [a.signature] + \
           [s["signature"] for s in reversed(post_in) if not s.get("err")]
    print(f"   до входа {len(pre)} (с самого создания: {'да' if pre_complete else 'нет'}), после входа в окне {len(post_in)}")
    eta = len(sigs) / a.rps
    print(f"3/4 загружаю {len(sigs)} транзакций (~{eta / 60:.0f} мин)…", flush=True)
    raw = fetch_txs(rpc, sigs)
    events = []
    entry_idx = None
    for s, r in zip(sigs, raw):
        e = parse_tx(r, mint) if r else None
        if e:
            if s == a.signature:
                entry_idx = len(events)
            events.append(e)
    if entry_idx is None:
        print("не удалось разобрать транзакцию входа")
        return 1

    print("4/4 разбор…\n", flush=True)
    creator, _ = W.find_creator(rpc, mint)
    if not creator and pre_complete and events:
        creator = events[0]["signer"]
    r = replay(events, entry_idx, user, creator)
    entry = events[entry_idx]
    ep = entry["price"]
    print(f"Токен {mint}")
    print(f"Создатель {creator or '—'}; ранних покупателей до тебя: {len(r['tracked']['early'])}")
    print(f"Вход   {fmt_t(entry['t'])}  цена {ep:.3e} SOL  потрачено {abs(entry['dsol']):.3f} SOL")
    print(f"Пик    {fmt_t(r['peak_t'])}  {pct(r['peak'], ep)}  (через {(r['peak_t'] - entry['t']) / 60:.0f} мин)")
    if r["user_exit"]:
        print(f"Твой выход {fmt_t(r['user_exit']['t'])}  {pct(r['user_exit']['price'], ep)}")
    s = r["signal"]
    if s:
        print(f"\nСИГНАЛ {fmt_t(s['t'])} (через {(s['t'] - entry['t']) / 60:.1f} мин после входа): {s['reason']}")
        print(f"   цена в момент сигнала {pct(s['price'], ep)}")
        print("   выход по сигналу с задержкой: " + " · ".join(f"{k}с {pct(v, ep)}" for k, v in r["exits"].items()))
    else:
        print("\nСигнал выхода в окне не сработал")
    print(f"Держать до конца окна ({a.hours:g}ч): {pct(r['final'], ep)}")
    print("\nПервые продажи информированных после входа:")
    for x in r["sells"][:12]:
        print(f"   {fmt_t(x['t'])} {x['who']:17} {x['owner'][:4]}…  продал {x['sold_pct']:.0f}% (осталось {x['left_pct']:.0f}%)"
              f"  цена {pct(x['price'], ep)}")
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"replay_{mint[:8]}.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "signer", "side", "sol", "tokens", "price_sol"])
        for e in events:
            w.writerow([e["t"], e["signer"], "buy" if e["dtok"] > 0 else "sell", round(e["dsol"], 6),
                        round(e["dtok"], 2), e["price"] or ""])
    print(f"\nХронология всех сделок: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
