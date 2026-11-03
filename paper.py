"""Бумажная торговля dragon_core. НИЧЕГО НЕ ОТПРАВЛЯЕТ НА БИРЖУ — ни ключей, ни ордеров в этом файле нет.

    python3 paper.py              # каждый закрытый час: step() на свежих данных → paper_log.csv, paper_summary.md
    python3 paper.py --summary    # только пересобрать paper_summary.md из журнала

Нужен работающий hl_collector.py (он пишет dragon.db). Вместе на ночь: ./run_night.sh
Исполнение то же, что в бэктесте: решение по закрытому бару → виртуальная сделка по close следующего бара,
комиссия тейкера + проскальзывание + funding. Если последний час неполный (NaN в колонках контракта) —
решения нет, в журнал пишется SKIP с причиной.
"""
import argparse
import csv
import json
import logging
import os
import time
from datetime import datetime, timezone

import pandas as pd

import backtest as bt
import core_adapter as ca
import hl_collector as hc

LOG = os.path.join(hc.HERE, "paper_log.csv")
SUMMARY = os.path.join(hc.HERE, "paper_summary.md")
STATE = os.path.join(hc.HERE, "paper_state.json")
FIELDS = ["time", "symbol", "decision", "score", "reasons", "price", "plan", "position", "event", "event_px",
          "trade_net_ret"]
GRACE_S = 150  # ждём после закрытия часа, пока сборщик допишет снимок и поток

log = logging.getLogger("paper")


def _now():
    return datetime.now(timezone.utc)


def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def save_state(st):
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"), default=str)
    os.replace(tmp, STATE)


def append(row):
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in FIELDS})


def tick(symbol, st, fee=bt.TAKER_FEE, slip=bt.SLIPPAGE, db_path=None):
    """Один закрытый час для symbol. st — состояние позиции этого символа (мутируется)."""
    raw = bt.market(symbol, db_path)
    if raw.empty:
        return None
    ts = raw.index[-1]  # последний закрытый час
    if st.get("last_bar") == str(ts):
        return None
    px = float(raw["close"].iloc[-1])
    row = {"time": ts.isoformat(), "symbol": symbol, "price": px, "position": st.get("pos", 0)}

    # 0) funding часа ts, если позиция была открыта на его начале (как в бэктесте)
    if st.get("pos") and st.get("entry") and st["entry"]["entry_time"] < ts.isoformat():
        fr = raw["funding"].iloc[-1]
        if pd.notna(fr):
            st["entry"]["funding"] += st["pos"] * float(fr)
    # 1) исполняем решение прошлого часа по close этого бара
    pend = st.pop("pending", None)
    if pend is not None:
        pos, tgt = st.get("pos", 0), pend["target"]
        if pos != 0 and tgt != pos:
            e = st.pop("entry")
            fill = px * (1 - pos * slip)
            net = pos * (fill / e["px_fill"] - 1) - 2 * fee - e["funding"]
            row.update(event=f"CLOSE_{'LONG' if pos > 0 else 'SHORT'}:{pend['why']}", event_px=fill,
                       trade_net_ret=round(net, 6))
            st.setdefault("closed", []).append({**e, "exit_time": ts.isoformat(), "exit_px": fill, "net_ret": net,
                                                "exit_reason": pend["why"]})
            st["pos"] = pos = 0
        if tgt != 0 and pos == 0:
            fill = px * (1 + tgt * slip)
            st["entry"] = {"side": tgt, "entry_time": ts.isoformat(), "px_fill": fill, "funding": 0.0,
                           "levels": pend.get("levels"), "score": pend.get("score")}
            st["pos"] = tgt
            row["event"] = (row.get("event", "") + " " if row.get("event") else "") + \
                f"OPEN_{'LONG' if tgt > 0 else 'SHORT'}"
            row["event_px"] = fill

    # 2) решение по закрытому часу
    data = hc.apply_contract(raw, hc.required_columns())
    if data.iloc[-1].isna().any():
        miss = [c for c in data.columns if pd.isna(data.iloc[-1][c])]
        row.update(decision="SKIP", reasons=f"неполные данные: {', '.join(miss)}")
    else:
        full = data.dropna()
        core_state = st.get("core_state")
        rawres, core_state = ca.call_step(full, core_state)
        try:
            json.dumps(core_state, default=str)
            st["core_state"] = core_state
        except (TypeError, ValueError):
            st["core_state"] = None
        n = ca.normalize(rawres, st.get("pos", 0))
        tgt, why = n["target"], "signal"
        pos = st.get("pos", 0)
        lv = (st.get("entry") or {}).get("levels") or [None, None]
        if pos and lv[0] is not None and (px - lv[0]) * pos <= 0:
            tgt, why = 0, "stop"
        elif pos and lv[1] is not None and (px - lv[1]) * pos >= 0:
            tgt, why = 0, "take"
        row.update(decision=n["decision"], score=n["score"], reasons=n["reasons"],
                   plan=json.dumps(n["plan"], default=str, ensure_ascii=False) if n["plan"] is not None else "")
        if tgt != pos:
            st["pending"] = {"target": tgt, "why": why, "score": n["score"],
                             "levels": list(ca.plan_levels(n["plan"], tgt)) if tgt else None}
    row["position"] = st.get("pos", 0)
    st["last_bar"] = str(ts)
    append(row)
    return row


def write_summary(st=None, path=SUMMARY):
    st = st if st is not None else load_state()
    try:
        j = pd.read_csv(LOG)
    except (OSError, pd.errors.EmptyDataError):
        j = pd.DataFrame(columns=FIELDS)
    L = ["# Paper trading — сводка", "", f"Тип: **paper** (виртуальные сделки, на биржу ничего не отправлялось). "
         f"Обновлено {_now():%Y-%m-%d %H:%M} UTC.", ""]
    if j.empty:
        L.append("Журнал пуст.")
    else:
        L += [f"Часов в журнале: {len(j)} ({j['time'].min()} → {j['time'].max()}).", "",
              "| символ | часов | решений | SKIP | LONG | SHORT | позиция сейчас |", "|---|---|---|---|---|---|---|"]
        for s, g in j.groupby("symbol"):
            d = g["decision"].fillna("")
            L.append(f"| {s} | {len(g)} | {(d != 'SKIP').sum()} | {(d == 'SKIP').sum()} | {(d == 'LONG').sum()} | "
                     f"{(d == 'SHORT').sum()} | {st.get(s, {}).get('pos', 0)} |")
        skips = j[j["decision"] == "SKIP"]["reasons"].value_counts().head(5)
        if len(skips):
            L += ["", "Почему пропускали часы:"] + [f"- {k}: {v}" for k, v in skips.items()]
    closed = [dict(c, symbol=s) for s, v in st.items() if isinstance(v, dict) for c in v.get("closed", [])]
    L += ["", "## Закрытые виртуальные сделки (реализованный результат, после издержек)", ""]
    if not closed:
        L.append("Закрытых сделок нет.")
    else:
        t = pd.DataFrame(closed)
        r = t["net_ret"]
        L += [f"Сделок: {len(t)}, винрейт {(r > 0).mean() * 100:.0f}%, сумма {r.sum() * 100:.2f}% от нотионала, "
              f"средняя {r.mean() * 100:.2f}%."]
        if len(t) < bt.MIN_TRADES:
            L.append(f"**Сделок меньше {bt.MIN_TRADES} — выводов о качестве нет.**")
    opn = [(s, v["entry"]) for s, v in st.items() if isinstance(v, dict) and v.get("entry")]
    L += ["", "## Открытые позиции (нереализованный результат — отдельно)", ""]
    L += [f"- {s}: {'лонг' if e['side'] > 0 else 'шорт'} с {e['entry_time']} по {e['px_fill']:.2f}" for s, e in opn] \
        or ["Нет."]
    open(path, "w").write("\n".join(L) + "\n")


def loop():
    st = load_state()
    log.info("paper запущен; журнал %s", LOG)
    while True:
        for s in hc.SYMBOLS:
            try:
                r = tick(s, st.setdefault(s, {}))
                if r:
                    log.info("%s %s %s score=%s %s", s, r["time"], r.get("decision"), r.get("score"),
                             r.get("event", ""))
            except Exception as e:
                log.exception("%s: %s", s, e)
        save_state(st)
        write_summary(st)
        now = time.time()
        time.sleep(3600 - now % 3600 + GRACE_S)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(hc.HERE, "paper.log"))])
    if a.summary:
        write_summary()
    else:
        loop()


if __name__ == "__main__":
    main()
