"""Бэктест dragon_core.step() по часовым барам из dragon.db.

    python3 backtest.py                 # BTC и ETH → report.md, cycles.md, backtest_trades.csv

Модель исполнения:
- решение по закрытому бару t (step видит только бары ≤ t) → исполнение по close бара t+1;
- комиссия тейкера Hyperliquid (по умолчанию 0.045% — базовый уровень) + проскальзывание 0.05% на каждую сторону;
- funding Hyperliquid начисляется ежечасно, пока позиция открыта (лонг платит при положительной ставке);
- стоп/тейк из plan (если ядро даёт числа) проверяются по close бара, выход — по close следующего;
- размер — 1× нотионал, без плеча и без реинвеста: доходность сделки в % от нотионала.
Прогоняются только часы, где все колонки REQUIRED_COLUMNS непустые.
"""
import argparse
import os

import numpy as np
import pandas as pd

import core_adapter as ca
import hl_collector as hc

TAKER_FEE = float(os.environ.get("HL_TAKER_FEE", "0.00045"))
SLIPPAGE = 0.0005
HOUR = pd.Timedelta(hours=1)
MIN_TRADES = 30
SPIKE_Z = 2.0
Z_WINDOW = 168  # часов — для запасного расчёта z, если ядро не отдаёт признаки


def market(symbol, db_path=None):
    db = hc.connect(db_path)
    try:
        raw = hc._hourly_frame(db, symbol)
    finally:
        db.close()
    return raw


def simulate(symbol, data, prices, funding, fee=TAKER_FEE, slip=SLIPPAGE, on_step=None):
    """data — кадр по контракту (может содержать NaN); prices/funding — Series по тем же часам.
    → (trades DataFrame, equity Series, decisions list)"""
    req = list(data.columns)
    full = data.dropna(subset=req)
    if full.empty:
        return pd.DataFrame(), pd.Series(dtype=float), []
    hours = prices.loc[full.index[0]:].index
    full_set = set(full.index)
    pos, entry, state, pending, plan = 0, None, None, None, None
    trades, equity, decisions = [], {}, []
    realized = 0.0
    full_pos = {ts: i for i, ts in enumerate(full.index)}

    def close_trade(ts, px, reason):
        nonlocal realized
        e = entry
        side = e["side"]
        fill = px * (1 - side * slip)
        gross = side * (px / e["px_mid"] - 1)
        net = side * (fill / e["px_fill"] - 1) - 2 * fee - e["funding"]
        realized += net
        trades.append({**{k: v for k, v in e.items() if k not in ("px_mid",)}, "symbol": symbol,
                       "exit_time": ts, "exit_px": fill, "exit_reason": reason,
                       "gross_ret": gross, "net_ret": net, "hold_h": (ts - e["entry_time"]) / HOUR})

    for i, ts in enumerate(hours):
        px = float(prices.loc[ts])
        # 1) исполняем решение, принятое на прошлом баре, по close этого бара
        if pending is not None:
            tgt, why, meta = pending
            pending = None
            if pos != 0 and tgt != pos:
                close_trade(ts + HOUR, px, why)
                pos, entry = 0, None
            if tgt != 0 and pos == 0:
                side = tgt
                entry = {"side": side, "entry_time": ts + HOUR, "entry_hour_utc": (ts + HOUR).hour,
                         "entry_weekday": (ts + HOUR).dayofweek, "px_mid": px, "px_fill": px * (1 + side * slip),
                         "funding": 0.0, **meta}
                pos = side
                plan = meta.get("_levels", (None, None))
        # 2) funding за час, начинающийся после этого close
        if pos != 0 and i + 1 < len(hours):
            fr = funding.get(hours[i + 1], np.nan)
            if pd.notna(fr):
                entry["funding"] += pos * float(fr)
        # 3) решение по закрытому бару ts
        if ts in full_set:
            window = full.iloc[: full_pos[ts] + 1]
            raw, state = ca.call_step(window, state)
            n = ca.normalize(raw, pos)
            rec = {"time": ts, "decision": n["decision"], "target": n["target"], "score": n["score"],
                   "reasons": n["reasons"], "plan": n["plan"], "price": px, **n["features"]}
            decisions.append(rec)
            if on_step:
                on_step(rec)
            tgt, why = n["target"], "signal"
            stop, take = plan if (pos != 0 and plan) else (None, None)
            if pos != 0 and stop is not None and (px - stop) * pos <= 0:
                tgt, why = 0, "stop"
            elif pos != 0 and take is not None and (px - take) * pos >= 0:
                tgt, why = 0, "take"
            if tgt != pos and i + 1 < len(hours):
                meta = {"score": n["score"], "reasons": n["reasons"],
                        "_levels": ca.plan_levels(n["plan"], tgt) if tgt else (None, None)}
                pending = (tgt, why, meta)
        mtm = 0.0
        if pos != 0:
            mtm = pos * (px * (1 - pos * slip) / entry["px_fill"] - 1) - 2 * fee - entry["funding"]
        equity[ts] = realized + mtm
    if pos != 0:  # открытая на конце — закрываем по последнему close, помечаем
        close_trade(hours[-1] + HOUR, float(prices.iloc[-1]), "end_of_data")
    t = pd.DataFrame(trades)
    if not t.empty:
        t = t.drop(columns=["_levels"], errors="ignore")
    return t, pd.Series(equity, dtype=float), decisions


def stats(t, equity=None):
    if t is None or t.empty:
        return {"trades": 0}
    r = t["net_ret"]
    wins, losses = r[r > 0], r[r <= 0]
    eq = equity if equity is not None and len(equity) else r.cumsum()
    dd = (eq - eq.cummax().clip(lower=0)).min()
    return {"trades": len(r), "winrate": len(wins) / len(r), "avg_win": wins.mean() if len(wins) else np.nan,
            "avg_loss": losses.mean() if len(losses) else np.nan, "avg": r.mean(), "total": r.sum(),
            "max_dd": dd, "pf": wins.sum() / -losses.sum() if losses.sum() < 0 else np.inf,
            "avg_hold_h": t["hold_h"].mean()}


def pct(x, d=2):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.{d}f}%"


def stats_row(name, s):
    if not s.get("trades"):
        return f"| {name} | 0 | — | — | — | — | — | — | — |"
    pf = "∞" if np.isinf(s["pf"]) else f"{s['pf']:.2f}"
    return (f"| {name} | {s['trades']} | {pct(s['winrate'], 1)} | {pct(s['avg'])} | {pct(s['avg_win'])} | "
            f"{pct(s['avg_loss'])} | {pct(s['max_dd'])} | {pf} | {pct(s['total'])} |")


HDR = ("| | сделок | винрейт | средняя | ср. прибыль | ср. убыток | макс. просадка | профит-фактор | сумма |\n"
       "|---|---|---|---|---|---|---|---|---|")


def write_report(path, trades, per_symbol, windows, fee, slip):
    n = len(trades)
    L = ["# Бэктест dragon_core — отчёт", "",
         "Тип: **бэктест** (не paper, не live). Исполнение по close следующего бара; "
         f"комиссия тейкера {fee * 100:.3f}% + проскальзывание {slip * 100:.2f}% на сторону; funding ежечасно; "
         "1× нотионал, доходности в % от нотионала, без реинвеста.", ""]
    for s, (a, b, rows) in windows.items():
        L.append(f"- {s}: окно с полными данными {a:%Y-%m-%d %H:00} → {b:%Y-%m-%d %H:00} UTC, {rows} ч"
                 if a is not None else f"- {s}: часов со всеми колонками нет — прогона не было")
    L.append("")
    if n < MIN_TRADES:
        L += [f"**Сделок {n} — меньше {MIN_TRADES}. Выводов о качестве стратегии нет: выборка мала.**", ""]
    L += ["## Итог", "", HDR, stats_row("все", stats(trades))]
    for s, (t, eq) in per_symbol.items():
        L.append(stats_row(s, stats(t, eq)))
    if n:
        L += ["", "Макс. просадка по символу — по почасовой переоценке; для «все» — по закрытым сделкам.", "",
              "## По направлению", "", HDR]
        for side, name in ((1, "лонг"), (-1, "шорт")):
            L.append(stats_row(name, stats(trades[trades["side"] == side])))
        L += ["", "## По часу входа (UTC)", "", HDR]
        for h in range(24):
            sub = trades[trades["entry_hour_utc"] == h]
            if len(sub):
                L.append(stats_row(f"{h:02d}:00", stats(sub)))
        L += ["", "## Причины выхода", ""]
        for k, v in trades["exit_reason"].value_counts().items():
            L.append(f"- {k}: {v}")
        top5 = trades["net_ret"].nlargest(5).sum()
        tot = trades.loc[trades["net_ret"] > 0, "net_ret"].sum()
        if tot > 0:
            L += ["", f"Доля прибыли от топ-5 сделок: {top5 / tot * 100:.0f}% (высокая доля — результат держится на единицах)."]
    L += ["", "Прошлый результат не гарантирует будущий. Out-of-sample проверки в этом отчёте нет."]
    open(path, "w").write("\n".join(L) + "\n")


def spike_features(data, decisions):
    """liq_long_z, liq_short_z, cvd_chg: сначала из ответов step(), потом из функции ядра, иначе — свой расчёт."""
    d = pd.DataFrame(decisions).set_index("time") if decisions else pd.DataFrame()
    if not d.empty and all(f in d.columns for f in ca.FEATURES) and d[list(ca.FEATURES)].notna().any().all():
        return d[list(ca.FEATURES)], "ответы dragon_core.step()"
    f, src = ca.feature_frame(data)
    if f is not None:
        return f, src
    raw = data
    out = pd.DataFrame(index=raw.index)

    def z(s):
        m = s.rolling(Z_WINDOW, min_periods=48).mean()
        sd = s.rolling(Z_WINDOW, min_periods=48).std()
        return (s - m) / sd.replace(0, np.nan)

    for col, name in (("liq_long", "liq_long_z"), ("liq_short", "liq_short_z")):
        out[name] = z(raw[col]) if col in raw else np.nan
    out["cvd_chg"] = z(raw["cvd_delta"]) if "cvd_delta" in raw else np.nan
    return out, f"запасной расчёт бэктеста (z-score {Z_WINDOW}ч; cvd_chg = z почасовой дельты CVD) — не ядро"


def write_cycles(path, feats_by_symbol):
    days = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    L = ["# Циклы всплесков", "", f"Всплеск: z ≥ {SPIKE_Z} (для cvd_chg — |z| ≥ {SPIKE_Z}). "
         "Доля = всплесков / часов с данными в этой ячейке; lift = доля ÷ средняя доля (1.0 = равномерно).", ""]
    for sym, (f, src) in feats_by_symbol.items():
        L += [f"## {sym}", "", f"Источник признаков: {src}.", ""]
        f = f.dropna(how="all")
        if f.empty:
            L += ["Данных нет.", ""]
            continue
        flags = pd.DataFrame({"liq_long_z": f["liq_long_z"] >= SPIKE_Z, "liq_short_z": f["liq_short_z"] >= SPIKE_Z,
                              "cvd_chg": f["cvd_chg"].abs() >= SPIKE_Z}, index=f.index)
        valid = f.notna()
        idx = pd.DatetimeIndex(f.index)
        L.append(f"Часов: {len(f)}; всплесков: " + ", ".join(f"{c} {int(flags[c].sum())}" for c in flags) +
                 ("  \n**Мало всплесков (<20 на признак) — распределение шумное, выводов нет.**"
                  if flags.sum().min() < 20 else ""))
        for key, labels, title in ((idx.hour, [f"{h:02d}" for h in range(24)], "Час UTC"),
                                   (idx.dayofweek, days, "День недели")):
            L += ["", f"### {title}", "", "| " + title + " | " + " | ".join(
                f"{c} доля | {c} lift" for c in flags) + " |", "|---" * (1 + 2 * len(flags)) + "|"]
            for k, lab in enumerate(labels):
                m = key == k
                cells = []
                for c in flags:
                    n = valid[c][m].sum()
                    rate = flags[c][m].sum() / n if n else np.nan
                    base = flags[c][valid[c]].mean()
                    cells += [pct(rate, 1), f"{rate / base:.2f}" if base and n else "—"]
                L.append(f"| {lab} | " + " | ".join(cells) + " |")
        L.append("")
    open(path, "w").write("\n".join(L) + "\n")


def run(symbols=hc.SYMBOLS, out_dir=hc.HERE, db_path=None, fee=TAKER_FEE, slip=SLIPPAGE):
    req = hc.required_columns()
    all_t, per, windows, feats = [], {}, {}, {}
    for s in symbols:
        raw = market(s, db_path)
        if raw.empty:
            windows[s] = (None, None, 0)
            continue
        data = hc.apply_contract(raw, req)
        full = data.dropna()
        windows[s] = (full.index.min(), full.index.max(), len(full)) if len(full) else (None, None, 0)
        t, eq, dec = simulate(s, data, raw["close"], raw["funding"], fee, slip)
        per[s] = (t, eq)
        if not t.empty:
            all_t.append(t)
        feats[s] = spike_features(raw.loc[full.index] if len(full) else raw, dec)
    trades = pd.concat(all_t, ignore_index=True) if all_t else pd.DataFrame(
        columns=["side", "entry_hour_utc", "net_ret", "exit_reason", "hold_h"])
    write_report(os.path.join(out_dir, "report.md"), trades, per, windows, fee, slip)
    write_cycles(os.path.join(out_dir, "cycles.md"), feats)
    trades.to_csv(os.path.join(out_dir, "backtest_trades.csv"), index=False)
    return trades, per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fee", type=float, default=TAKER_FEE)
    ap.add_argument("--slip", type=float, default=SLIPPAGE)
    a = ap.parse_args()
    t, _ = run(fee=a.fee, slip=a.slip)
    print(f"сделок: {len(t)} → report.md, cycles.md, backtest_trades.csv")


if __name__ == "__main__":
    main()
