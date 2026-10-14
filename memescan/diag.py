"""Live self-check: python3 -m memescan.diag
Verifies on real data what the offline tests can't: GeckoTerminal pumpswap pools, PumpSwap pool
layout (vault offsets), on-chain price vs GeckoTerminal price, creator lookup, holders, live flow.
Prints a short report to paste back."""
import sys
import time

from .config import Config
from .sources import Gecko, SolanaRPC, DexScreener, SourceError
from .strategy import normalize_pool, is_pump_mint
from .ticker import PriceTicker, PUMPSWAP_PROGRAM, pumpswap_vaults
from . import whales as W


def ok(flag):
    return "OK " if flag else "FAIL"


def main():
    cfg = Config.load()
    gecko, rpc = Gecko(cfg.gecko_min_interval_sec), SolanaRPC(cfg.solana_rpc_url)
    print(f"RPC: {cfg.solana_rpc_url.split('?')[0]}")
    # 1. GeckoTerminal pumpswap pools
    try:
        raw = gecko._get("/dexes/pumpswap/pools?include=base_token&sort=h24_volume_usd_desc&page=1").get("data", [])
        print(f"[{ok(bool(raw))}] GeckoTerminal /dexes/pumpswap: {len(raw)} пулов")
    except SourceError as e:
        print(f"[FAIL] GeckoTerminal /dexes/pumpswap: {e}")
        raw = gecko._get("/pools?include=base_token&sort=h24_volume_usd_desc&page=1").get("data", [])
    pools = [(normalize_pool(p), p["attributes"]) for p in raw]
    pools = [(p, a) for p, a in pools if is_pump_mint(p["mint"]) and p["liquidity_usd"] > 20_000][:3]
    if not pools:
        print("[FAIL] нет pump-пулов с ликвидностью > $20K для проверки")
        return 1
    dexes = {p["dex"] for p, _ in pools}
    print(f"       dex id у пулов: {', '.join(sorted(dexes))}")

    # 2. PumpSwap layout + price check
    raws = rpc.multiple_accounts_raw([p["pool"] for p, _ in pools])
    for (p, a), acc in zip(pools, raws):
        tag = f"{p['symbol'][:10]:10} {p['pool'][:6]}…"
        if not acc:
            print(f"[FAIL] {tag} аккаунт пула не найден")
            continue
        owner, data = acc
        if owner != PUMPSWAP_PROGRAM:
            print(f"[info] {tag} не PumpSwap (owner {owner[:6]}…), len={len(data)} -> будет DexScreener")
            continue
        v = pumpswap_vaults(data, p["mint"])
        if not v:
            print(f"[FAIL] {tag} PumpSwap, но mint не на смещении 43 (len={len(data)}) — раскладка другая")
            continue
        accs = rpc.multiple_accounts(list(v))
        b = float(accs[0]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
        q = float(accs[1]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
        mine = q / b if b else 0
        gk = float(a.get("base_token_price_quote_token") or 0)
        diff = abs(mine / gk - 1) * 100 if gk else None
        print(f"[{ok(diff is not None and diff < 3)}] {tag} хранилища найдены; цена on-chain {mine:.3e} SOL "
              f"vs GeckoTerminal {gk:.3e} (расхождение {diff:.2f}%)" if diff is not None else
              f"[info] {tag} цена on-chain {mine:.3e}, у GeckoTerminal нет цены в SOL")

    # 3. creator + holders (first coin)
    p0 = pools[0][0]
    c, done = W.find_creator(rpc, p0["mint"])
    print(f"[{ok(bool(c))}] создатель {p0['symbol']}: {c or 'не найден (старая монета?)'}; curve complete={done}")
    h = W.analyze_holders(rpc, p0["mint"], with_clusters=False)
    if h.get("error"):
        print(f"[FAIL] холдеры: {h['error']}")
    else:
        print(f"[OK ] холдеры: топ-1 {h['top1_pct']:.1f}%, топ-10 {h['top10_pct']:.1f}%, создатель {h['creator_pct']:.2f}%")

    # 4. live ticker + on-chain flow for 30s
    t = PriceTicker(rpc, 1.0, DexScreener())
    t.set_watch({p["mint"]: p["pool"] for p, _ in pools})
    print("       тикер 30 сек…", flush=True)
    for _ in range(30):
        t.tick()
        time.sleep(1)
    for p, _ in pools:
        v = t.velocity(p["mint"]) or {}
        f = t.live_flow(p["mint"]) or {}
        m1 = f.get("1m", {})
        print(f"[{ok('v15' in v and v.get('v15') is not None)}] {p['symbol'][:10]:10} источник={v.get('src') or v.get('status')} "
              f"v15={v.get('v15')} v30={v.get('v30')} | on-chain 1м: buy {m1.get('buy_q', 0):.3f} / sell {m1.get('sell_q', 0):.3f} SOL")
    if t.last_error:
        print(f"[FAIL] ошибка тикера: {t.last_error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
