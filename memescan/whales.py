"""On-chain holder analysis: top wallets, linked-wallet clusters, whale movement.

Cluster heuristic (fast, ~20 RPC calls): for each top wallet find who first funded it
with SOL. Wallets sharing a funder (or funding each other) = one cluster.
"""
from .solana_util import bonding_curve_address, parse_bonding_curve, is_pump_mint
from .sources import SolanaRPC, SourceError

# Exchange hot wallets fund thousands of unrelated users -> never cluster by them.
EXCHANGE_WALLETS = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "Binance",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S": "Binance",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Binance",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "Coinbase",
    "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE": "Coinbase",
    "5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD": "OKX",
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": "Bybit",
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": "Kraken",
}


def short(a):
    return f"{a[:4]}…{a[-4:]}" if a and len(a) > 10 else a


def find_creator(rpc: SolanaRPC, mint):
    """pump.fun creator from the on-chain bonding curve account. Returns (creator, curve_complete)."""
    if not is_pump_mint(mint):
        return None, None
    try:
        data = rpc.account_bytes(bonding_curve_address(mint))
    except (SourceError, ValueError):
        return None, None
    bc = parse_bonding_curve(data) if data else None
    return (bc["creator"], bc["complete"]) if bc else (None, None)


def find_funder(rpc: SolanaRPC, wallet):
    """Return (funder, status). status: ok | partial (old/active wallet) | none."""
    sig, complete = rpc.oldest_signature(wallet)
    if not sig:
        return None, "none"
    tx = rpc.transaction(sig)
    if not tx:
        return None, "none"
    msg = tx["transaction"]["message"]
    instrs = list(msg.get("instructions", []))
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        instrs += inner.get("instructions", [])
    for ix in instrs:
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict):
            continue
        info = parsed.get("info", {})
        if parsed.get("type") in ("transfer", "transferChecked", "createAccount") and \
                (info.get("destination") == wallet or info.get("newAccount") == wallet):
            src = info.get("source")
            if src and src != wallet:
                return src, "ok" if complete else "partial"
    keys = msg.get("accountKeys", [])
    payer = keys[0]["pubkey"] if keys and isinstance(keys[0], dict) else (keys[0] if keys else None)
    if payer and payer != wallet:
        return payer, "ok" if complete else "partial"
    return None, "partial" if not complete else "none"


def build_clusters(wallets, funders):
    """wallets: list of {owner, pct}; funders: {owner: funder}. Union-find by shared funder / direct funding."""
    parent = {w["owner"]: w["owner"] for w in wallets}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    by_funder = {}
    for w in wallets:
        f = funders.get(w["owner"])
        if not f or f in EXCHANGE_WALLETS:
            continue
        if f in parent:          # one top wallet funded another
            union(w["owner"], f)
        by_funder.setdefault(f, []).append(w["owner"])
    for owners in by_funder.values():
        for o in owners[1:]:
            union(owners[0], o)

    groups = {}
    for w in wallets:
        groups.setdefault(find(w["owner"]), []).append(w)
    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        common = {funders.get(m["owner"]) for m in members} - {None}
        clusters.append({
            "wallets": [m["owner"] for m in members],
            "pct": round(sum(m["pct"] for m in members), 2),
            "funder": next(iter(common)) if len(common) == 1 else None,
        })
    clusters.sort(key=lambda c: -c["pct"])
    for i, c in enumerate(clusters):
        c["label"] = f"кластер {chr(65 + i)}"
    return clusters


def analyze_holders(rpc: SolanaRPC, mint, with_clusters=True, top_n=10):
    try:
        info = rpc.mint_info(mint)
        supply = rpc.token_supply(mint)
        largest = rpc.largest_accounts(mint)
        if not supply or not largest:
            return {"error": "нет данных по supply/холдерам"}
        tok_accounts = [a["address"] for a in largest]
        parsed = rpc.multiple_accounts(tok_accounts)
        owners_of = {}
        for addr, acc in zip(tok_accounts, parsed):
            if acc:
                owners_of[addr] = acc["data"]["parsed"]["info"]["owner"]
        uniq_owners = sorted(set(owners_of.values()))
        owner_accs = rpc.multiple_accounts(uniq_owners)
        is_wallet = {o: bool(a) and a.get("owner") == SolanaRPC.SYSTEM_PROGRAM
                     for o, a in zip(uniq_owners, owner_accs)}

        by_owner = {}
        for a in largest:
            owner = owners_of.get(a["address"])
            if not owner:
                continue
            e = by_owner.setdefault(owner, {"owner": owner, "amount": 0.0, "token_accounts": [],
                                            "kind": "wallet" if is_wallet.get(owner) else "program/LP"})
            e["amount"] += float(a.get("uiAmount") or 0)
            e["token_accounts"].append(a["address"])
        holders = sorted(by_owner.values(), key=lambda h: -h["amount"])
        for h in holders:
            h["pct"] = round(h["amount"] / supply * 100, 3)
            h["label"] = EXCHANGE_WALLETS.get(h["owner"], short(h["owner"]))
            if h["owner"] in EXCHANGE_WALLETS:
                h["kind"] = "exchange"
        wallets = [h for h in holders if h["kind"] == "wallet"][:top_n]

        funders = {}
        if with_clusters:
            for w in wallets:
                try:
                    f, status = find_funder(rpc, w["owner"])
                except SourceError:
                    f, status = None, "error"
                w["funder"], w["funder_status"] = f, status
                if f:
                    funders[w["owner"]] = f
        clusters = build_clusters(wallets, funders) if with_clusters else []
        cluster_of = {o: c["label"] for c in clusters for o in c["wallets"]}
        for w in wallets:
            w["cluster"] = cluster_of.get(w["owner"])

        creator, curve_complete = find_creator(rpc, mint)
        creator_accounts, creator_amount = [], 0.0
        if creator:
            for addr, amt in rpc.token_accounts_by_owner(creator, mint):
                creator_accounts.append(addr)
                creator_amount += amt
            for h in holders:
                if h["owner"] == creator:
                    h["is_creator"] = True
                    h["label"] = "создатель " + h["label"]
        for i, w in enumerate(wallets):
            w["role"] = "creator" if w["owner"] == creator else ("top3" if i < 3 else "other")

        return {
            "mint": mint,
            "supply": supply,
            "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
            "holders": holders,
            "top1_pct": wallets[0]["pct"] if wallets else 0.0,
            "top10_pct": round(sum(w["pct"] for w in wallets), 2),
            "clusters": clusters,
            "creator": creator,
            "creator_amount": creator_amount,
            "creator_pct": round(creator_amount / supply * 100, 3) if creator else 0.0,
            "creator_token_accounts": creator_accounts,
            "curve_complete": curve_complete,
            "error": None,
        }
    except SourceError as e:
        return {"error": str(e)}
    except (KeyError, TypeError, IndexError) as e:
        return {"error": f"неожиданный ответ RPC: {e}"}


def snapshot(h):
    """What we watch after entry: creator + top-10 wallets (top-3 flagged) + cluster membership."""
    wallets = [x for x in h.get("holders", []) if x["kind"] == "wallet"][:10]
    rows = [{"owner": w["owner"], "label": w["label"], "cluster": w.get("cluster"),
             "role": "creator" if w["owner"] == h.get("creator") else ("top3" if i < 3 else "other"),
             "token_accounts": w["token_accounts"], "amount": w["amount"]} for i, w in enumerate(wallets)]
    if h.get("creator") and not any(r["role"] == "creator" for r in rows):
        rows.append({"owner": h["creator"], "label": "создатель " + short(h["creator"]), "cluster": None,
                     "role": "creator", "token_accounts": h.get("creator_token_accounts", []),
                     "amount": h.get("creator_amount", 0.0)})
    return {"wallets": rows}


def snapshot_accounts(snap):
    return [ta for w in snap["wallets"] for ta in w["token_accounts"]]


def compare_snapshot(rpc: SolanaRPC, snap):
    """Fetch current balances, then compare_balances."""
    accs = snapshot_accounts(snap)
    cur = {}
    for addr, acc in zip(accs, rpc.multiple_accounts(accs) if accs else []):
        cur[addr] = float(acc["data"]["parsed"]["info"]["tokenAmount"].get("uiAmount") or 0) if acc else 0.0
    return compare_balances(snap, cur)


def compare_balances(snap, cur):
    """Entry snapshot vs current {token_account: amount} -> per-wallet (with role) / per-cluster / top-10 drop %.
    A closed / missing token account counts as fully sold."""
    rows, clusters = [], {}
    total0 = total1 = 0.0
    for i, w in enumerate(snap["wallets"]):
        now = sum(cur.get(ta) or 0.0 for ta in w["token_accounts"])
        if i < 10:  # an off-list creator is appended after the top-10
            total0 += w["amount"]
            total1 += now
        rows.append({"label": w["label"], "owner": w["owner"], "role": w.get("role", "other"),
                     "was": w["amount"], "now": now, "drop_pct": _drop(w["amount"], now)})
        if w.get("cluster"):
            c = clusters.setdefault(w["cluster"], [0.0, 0.0])
            c[0] += w["amount"]
            c[1] += now
    for label, (was, now) in clusters.items():
        rows.append({"label": label, "owner": None, "role": "cluster", "was": was, "now": now,
                     "drop_pct": _drop(was, now)})
    return {"wallets": rows, "top10_drop_pct": _drop(total0, total1)}


def _drop(was, now):
    return round(max(0.0, (was - now) / was * 100), 2) if was > 0 else 0.0
