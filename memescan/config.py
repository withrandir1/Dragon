"""All strategy thresholds in one place. Edit here or via data/config.json."""
import json
import os
from dataclasses import dataclass, asdict, fields

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


@dataclass
class Config:
    # --- 0. Universe: pump.fun only ---
    pump_only: bool = True                  # mint ends with "pump" or pool on pump.fun / PumpSwap
    include_bonding_curve: bool = False     # coins not yet graduated (thin, easy to kill with 1 tx)

    # --- 1. Anti-scam filter (pool level) ---
    min_liquidity_usd: float = 50_000       # graduated PumpSwap pool starts ~$25-30K -> this one grew
    min_age_hours: float = 24               # "not new" coins
    min_volume_24h_usd: float = 200_000
    min_fdv_usd: float = 300_000
    min_liq_to_fdv: float = 0.03            # thin liquidity vs cap = one sell can crash it
    max_drop_h24_pct: float = -60           # skip coins already collapsing

    # --- 2. Anti-scam filter (holders, checked on-chain for candidates) ---
    max_top1_holder_pct: float = 10         # biggest wallet (LP/curve excluded)
    max_top10_holders_pct: float = 35       # top-10 wallets combined
    max_cluster_pct: float = 15             # wallets funded by the same source
    max_creator_pct: float = 10             # creator's own bag
    require_no_mint_authority: bool = True
    require_no_freeze_authority: bool = True

    # --- 3. Entry / hold: buy $ vs sell $ ---
    entry_ratio_15m: float = 2.0            # buy$ >= 2x sell$ over 15 min
    entry_ratio_5m: float = 2.0             # and still >= 2x right now
    min_buy_usd_15m: float = 10_000
    min_unique_buyers_15m: int = 25         # not a single wallet painting the tape
    max_top_buyer_share: float = 0.30       # biggest buyer's share of buy volume
    entry_require_momentum: bool = True     # live price rising over both 15s and 30s at entry

    # --- 4. Exit (whatever comes first) ---
    exit_ratio_15m: float = 2.0             # hold while buy$ >= 2x sell$; below -> out
    creator_exit_drop_pct: float = 10       # creator sold >= 10% of his bag
    top3_exit_drop_pct: float = 20          # any of top-3 holders sold >= 20% of his bag
    cluster_exit_drop_pct: float = 20       # linked-wallet cluster sold >= 20%
    stop_loss_pct: float = -15              # hard stop, capital preservation

    # --- 5. Money: size + is the move worth the costs (see economics.py) ---
    bankroll_usd: float = 1000              # paper bankroll
    risk_per_trade_pct: float = 1.0         # a stop-out loses at most 1% of bankroll
    max_position_usd: float = 250
    max_impact_per_side_pct: float = 0.5    # our own order moves price at most 0.5%
    fee_per_side_pct: float = 1.0           # PumpSwap pool+protocol+creator fee (tiered by mcap; verify)
    fixed_cost_per_tx_usd: float = 0.15     # priority fee / tip per transaction
    max_round_trip_cost_pct: float = 4.0    # skip coins where the round trip alone costs more
    min_edge_multiple: float = 2.0          # expected move must be >= 2x round-trip cost
    ev_horizon_min: int = 15                # horizon of the expected move (research table)
    min_samples: int = 30                   # signals needed before the expectation is trusted
    trade_without_stats: bool = True        # paper only: trade while stats accumulate
    max_open_positions: int = 3

    # --- Runtime ---
    scan_interval_sec: int = 90
    tick_interval_sec: float = 1.0          # on-chain price ticker (growth speed 1s/15s/30s)
    trades_fetch_top_n: int = 10            # pools that get trade-level analysis per scan
    universe_pages: int = 3
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    gecko_min_interval_sec: float = 2.2     # free GeckoTerminal limit ~30 req/min
    auto_paper_trade: bool = True

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        path = os.path.join(DATA_DIR, "config.json")
        if os.path.exists(path):
            with open(path) as f:
                cfg.update(json.load(f))
        if os.environ.get("SOLANA_RPC_URL"):
            cfg.solana_rpc_url = os.environ["SOLANA_RPC_URL"]
        return cfg

    def update(self, values: dict) -> None:
        known = {f.name: f.type for f in fields(self)}
        for k, v in values.items():
            if k in known:
                cur = getattr(self, k)
                setattr(self, k, type(cur)(v) if not isinstance(cur, bool) else bool(v))

    def save(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "config.json"), "w") as f:
            json.dump(self.public(), f, indent=2)

    def public(self) -> dict:
        d = asdict(self)
        return d
