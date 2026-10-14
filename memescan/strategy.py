"""Pure strategy logic: no network, fully unit-testable.

Rule set (the whole strategy):
  1. Filter:  liquid, not new, not collapsing, sane liquidity/FDV.
  2. Entry:   buy$ share over 15m >= 62% AND 5m >= 60%, enough $ and many distinct buyers.
  3. Exit:    buy$ share 15m <= 52% (equilibrium)  OR  whales/cluster selling  OR  -15% stop.
"""
import math
import time
from datetime import datetime

# Non-meme majors / stables / wrapped assets on Solana: never treat as memecoins.
EXCLUDED_MINTS = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",   # JUP
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # jitoSOL
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj",  # stSOL
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",   # bSOL
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",  # RAY
    "27G8MtK7VtTcCHkpASjSDdkWWYfoqT6ggEuKidVJidD4",  # JLP
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",   # cbBTC
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",  # WBTC
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # WETH
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",  # PYTH
    "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",   # JTO
}


def _f(x, default=0.0):
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def _ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def normalize_pool(p, now=None):
    """GeckoTerminal pool JSON -> flat dict we use everywhere."""
    now = now or time.time()
    a = p["attributes"]
    base_id = p.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
    mint = base_id.split("_", 1)[1] if "_" in base_id else base_id
    created = _ts(a.get("pool_created_at"))
    tx = a.get("transactions") or {}
    vol = a.get("volume_usd") or {}
    chg = a.get("price_change_percentage") or {}
    return {
        "pool": a["address"],
        "mint": mint,
        "name": a.get("name", ""),
        "symbol": (a.get("name", "") or "").split(" / ")[0],
        "dex": p.get("relationships", {}).get("dex", {}).get("data", {}).get("id", ""),
        "price_usd": _f(a.get("base_token_price_usd")),
        "liquidity_usd": _f(a.get("reserve_in_usd")),
        "fdv_usd": _f(a.get("fdv_usd")),
        "mcap_usd": _f(a.get("market_cap_usd")) or _f(a.get("fdv_usd")),
        "volume_24h": _f(vol.get("h24")),
        "volume_1h": _f(vol.get("h1")),
        "change_h1": _f(chg.get("h1")),
        "change_h24": _f(chg.get("h24")),
        "age_hours": (now - created) / 3600 if created else None,
        "tx_m15": tx.get("m15") or {},
        "tx_h1": tx.get("h1") or {},
    }


def dedupe_by_token(pools):
    """Keep the most liquid pool per base token."""
    best = {}
    for p in pools:
        cur = best.get(p["mint"])
        if cur is None or p["liquidity_usd"] > cur["liquidity_usd"]:
            best[p["mint"]] = p
    return list(best.values())


def pool_filter(p, cfg):
    """Return list of rejection reasons (empty = passes)."""
    r = []
    if p["mint"] in EXCLUDED_MINTS:
        r.append("не мемкоин (major/stable)")
    if p["liquidity_usd"] < cfg.min_liquidity_usd:
        r.append(f"ликвидность ${p['liquidity_usd']:,.0f} < ${cfg.min_liquidity_usd:,.0f}")
    if p["age_hours"] is None or p["age_hours"] < cfg.min_age_hours:
        r.append(f"новая монета ({(p['age_hours'] or 0):.0f}ч < {cfg.min_age_hours:.0f}ч)")
    if p["volume_24h"] < cfg.min_volume_24h_usd:
        r.append(f"объём 24ч ${p['volume_24h']:,.0f} мал")
    if p["fdv_usd"] < cfg.min_fdv_usd:
        r.append(f"FDV ${p['fdv_usd']:,.0f} мал")
    if p["fdv_usd"] > 0 and p["liquidity_usd"] / p["fdv_usd"] < cfg.min_liq_to_fdv:
        r.append(f"ликвидность/FDV {p['liquidity_usd'] / p['fdv_usd']:.1%} < {cfg.min_liq_to_fdv:.0%}")
    if p["change_h24"] < cfg.max_drop_h24_pct:
        r.append(f"падение 24ч {p['change_h24']:.0f}%")
    return r


def prerank_score(p):
    """Cheap pre-rank from pool-level tx counts, to pick which pools get trade-level analysis."""
    t = p["tx_m15"]
    buys, sells = _f(t.get("buys")), _f(t.get("sells"))
    buyers, sellers = _f(t.get("buyers")), _f(t.get("sellers"))
    total = buys + sells
    if total < 10:
        return 0.0
    share = (buyers + 1) / (buyers + sellers + 2) if (buyers + sellers) else (buys + 1) / (total + 2)
    return share * math.log10(max(p["volume_1h"], 10))


def flow_metrics(trades, now=None):
    """Buy/sell $ flow over 5m and 15m from raw GeckoTerminal trades."""
    now = now or time.time()
    out = {}
    for label, window in (("5m", 300), ("15m", 900)):
        buy_usd = sell_usd = 0.0
        buyers, sellers, per_buyer = set(), set(), {}
        for t in trades:
            a = t["attributes"]
            ts = _ts(a.get("block_timestamp"))
            if ts is None or now - ts > window:
                continue
            usd = _f(a.get("volume_in_usd"))
            who = a.get("tx_from_address", "")
            if a.get("kind") == "buy":
                buy_usd += usd
                buyers.add(who)
                per_buyer[who] = per_buyer.get(who, 0) + usd
            else:
                sell_usd += usd
                sellers.add(who)
        total = buy_usd + sell_usd
        out[label] = {
            "buy_usd": round(buy_usd, 2),
            "sell_usd": round(sell_usd, 2),
            "buy_share": buy_usd / total if total else 0.5,
            "buyers": len(buyers),
            "sellers": len(sellers),
            "top_buyer_share": (max(per_buyer.values()) / buy_usd) if buy_usd else 0.0,
        }
    # If the 300-trade window doesn't reach 15 min back, the 15m numbers are partial.
    stamps = [s for s in (_ts(t["attributes"].get("block_timestamp")) for t in trades) if s]
    out["coverage_sec"] = (now - min(stamps)) if stamps else 0
    return out


def entry_check(flow, cfg):
    """Return list of unmet entry conditions (empty = ENTRY signal)."""
    f15, f5 = flow["15m"], flow["5m"]
    r = []
    if f15["buy_share"] < cfg.entry_buy_share_15m:
        r.append(f"покупки 15м {f15['buy_share']:.0%} < {cfg.entry_buy_share_15m:.0%}")
    if f5["buy_share"] < cfg.entry_buy_share_5m:
        r.append(f"покупки 5м {f5['buy_share']:.0%} < {cfg.entry_buy_share_5m:.0%}")
    if f15["buy_usd"] < cfg.min_buy_usd_15m:
        r.append(f"покупки 15м ${f15['buy_usd']:,.0f} < ${cfg.min_buy_usd_15m:,.0f}")
    if f15["buyers"] < cfg.min_unique_buyers_15m:
        r.append(f"покупателей {f15['buyers']} < {cfg.min_unique_buyers_15m}")
    if f15["top_buyer_share"] > cfg.max_top_buyer_share:
        r.append(f"один покупатель = {f15['top_buyer_share']:.0%} объёма")
    return r


def holders_check(h, cfg):
    """h = result of whales.analyze_holders. Return rejection reasons."""
    if h.get("error"):
        return [f"холдеры: {h['error']}"]
    r = []
    if cfg.require_no_mint_authority and h.get("mint_authority"):
        r.append("mint authority активен (можно допечатать)")
    if cfg.require_no_freeze_authority and h.get("freeze_authority"):
        r.append("freeze authority активен (могут заморозить)")
    if h["top1_pct"] > cfg.max_top1_holder_pct:
        r.append(f"крупнейший кошелёк {h['top1_pct']:.1f}% > {cfg.max_top1_holder_pct:.0f}%")
    if h["top10_pct"] > cfg.max_top10_holders_pct:
        r.append(f"топ-10 кошельков {h['top10_pct']:.1f}% > {cfg.max_top10_holders_pct:.0f}%")
    for c in h.get("clusters", []):
        if c["pct"] > cfg.max_cluster_pct:
            r.append(f"кластер {len(c['wallets'])} кошельков = {c['pct']:.1f}%")
            break
    return r


def exit_check(position, flow, price, whale_moves, cfg):
    """Return exit reason or None. whale_moves = whales.compare_snapshot output."""
    pnl = (price / position["entry_price"] - 1) * 100 if position["entry_price"] else 0
    if pnl <= cfg.stop_loss_pct:
        return f"стоп {pnl:.1f}%"
    if whale_moves:
        for w in whale_moves.get("wallets", []):
            if w["drop_pct"] >= cfg.whale_exit_drop_pct:
                return f"кит {w['label']} продал {w['drop_pct']:.0f}%"
        if whale_moves.get("top10_drop_pct", 0) >= cfg.whale_exit_top10_drop_pct:
            return f"топ-10 продали {whale_moves['top10_drop_pct']:.0f}%"
    if flow and flow["15m"]["buy_share"] <= cfg.exit_buy_share_15m:
        return f"перевес ушёл: покупки 15м {flow['15m']['buy_share']:.0%}"
    return None


def paper_pnl(entry_price, price, cost_pct):
    """Net PnL % after costs on both sides."""
    if not entry_price:
        return 0.0
    gross = price / entry_price
    net = gross * (1 - cost_pct / 100) ** 2
    return (net - 1) * 100
