"""Entry economics: what a round trip costs, how big the position is, is the move worth it.

Round-trip cost (fractions of position):
  fees    = 2 * fee_per_side
  impact  = 2 * size / quote_reserve      (constant-product: slippage ~= size / reserve per side;
                                           quote_reserve ~= liquidity / 2)
  fixed   = 2 * fixed_cost_per_tx / size  (priority / Jito tips, network fee)

Size = min(risk cap, impact cap, hard cap):
  risk cap   = bankroll * risk_per_trade / |stop|   -> a stop-out loses at most risk_per_trade of bankroll
  impact cap = max_impact_per_side * quote_reserve  -> we don't move the price against ourselves
"""


def position_size(liquidity_usd, cfg):
    quote = max(liquidity_usd / 2, 1.0)
    risk_cap = cfg.bankroll_usd * cfg.risk_per_trade_pct / abs(cfg.stop_loss_pct)
    impact_cap = cfg.max_impact_per_side_pct / 100 * quote
    return round(max(0.0, min(risk_cap, impact_cap, cfg.max_position_usd)), 2)


def round_trip_cost(size_usd, liquidity_usd, cfg):
    quote = max(liquidity_usd / 2, 1.0)
    fees = 2 * cfg.fee_per_side_pct
    impact = 2 * size_usd / quote * 100
    fixed = (2 * cfg.fixed_cost_per_tx_usd / size_usd * 100) if size_usd else 100.0
    return {"fees": round(fees, 3), "impact": round(impact, 3), "fixed": round(fixed, 3),
            "total": round(fees + impact + fixed, 3)}


def entry_economics(p, research, cfg):
    """verdict: ok | no_stats | weak (expected < needed) | costly (cost itself too high)."""
    size = position_size(p["liquidity_usd"], cfg)
    cost = round_trip_cost(size, p["liquidity_usd"], cfg)
    need = round(cfg.min_edge_multiple * cost["total"], 2)
    exp, n = research.expected(p["flow"]["15m"]["ratio"], cfg.ev_horizon_min) if research else (None, 0)
    if cost["total"] > cfg.max_round_trip_cost_pct:
        verdict = "costly"
    elif n < cfg.min_samples:
        verdict = "no_stats"
    elif exp < need:
        verdict = "weak"
    else:
        verdict = "ok"
    return {"size_usd": size, "cost": cost, "need_pct": need, "expected_pct": exp, "samples": n,
            "verdict": verdict}


def reason(e, cfg):
    if e["verdict"] == "costly":
        return f"издержки {e['cost']['total']:.1f}% > {cfg.max_round_trip_cost_pct:g}%"
    if e["verdict"] == "weak":
        return f"ожидание {e['expected_pct']:+.1f}% < нужно {e['need_pct']:.1f}%"
    if e["verdict"] == "no_stats":
        return f"мало статистики ({e['samples']}/{cfg.min_samples})"
    return None
