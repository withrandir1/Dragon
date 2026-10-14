"""Streamlit version of Memescan (paper trading only).

Local:   pip install -r requirements.txt && streamlit run streamlit_app.py
Cloud:   share.streamlit.io -> New app -> this repo/branch, main file streamlit_app.py.
Secrets (optional, app settings -> Secrets):  SOLANA_RPC_URL = "https://mainnet.helius-rpc.com/?api-key=..."
                                              MEMESCAN_DEMO = "1"   # synthetic data, no network
"""
import contextlib
import io
import os
import threading
import time

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Memescan · pump.fun", page_icon="📈", layout="wide")

if st.secrets.load_if_toml_exists():  # secrets are optional; silent when there is no file
    for key in ("SOLANA_RPC_URL", "MEMESCAN_DEMO"):
        if key in st.secrets:
            os.environ[key] = str(st.secrets[key])


@st.cache_resource
def get_engine():
    """One engine + ticker per server process, shared by all browser tabs."""
    if os.environ.get("MEMESCAN_DEMO") == "1":
        from memescan.demo import build_demo_engine
        eng = build_demo_engine()
    else:
        from memescan.engine import Engine
        eng = Engine()
    threading.Thread(target=eng.run_forever, daemon=True).start()
    threading.Thread(target=eng.ticker.run_forever, daemon=True).start()
    return eng


E = get_engine()
cfg = E.cfg


def usd(v):
    if v is None:
        return "—"
    v = float(v)
    return f"${v / 1e6:.2f}M" if v >= 1e6 else f"${v / 1e3:.1f}K" if v >= 1e3 else f"${v:.0f}"


def rx(f):
    if not f:
        return "—"
    r = f.get("ratio", 0)
    return ("⚡" if f.get("src") == "live" else "") + ("×∞" if r >= 99 else f"×{r:.1f}")


def pct(v, d=1):
    return "—" if v is None else f"{v:+.{d}f}%"


SIG = {"entry": "ВХОД", "holding": "В позиции", "rejected": "Отсев", "watch": "наблюдаем", "pending": "ждём скорость"}
VERD = {"ok": "выгодно", "no_stats": "копим статистику", "weak": "рост не покроет издержки", "costly": "дорогой вход"}

st.title("Memescan · pump.fun")
st.caption("🟡 PAPER — без реальных сделок")
st.markdown(
    "**1. Безопасные монеты:** pump.fun, мигрировавшие, с ликвидностью; без концентрации у создателя и связанных кошельков.  \n"
    "**2. Вход в спрос:** покупают в 2+ раза больше, чем продают (по $), цена растёт, ожидаемый рост ≥ 2× издержек.  \n"
    "**3. Выход с информированными:** продаёт создатель, топ-3 или связанная группа — выходим в ту же секунду. "
    "Иначе держим, пока перевес ≥ ×2; стоп −15%.")


@st.fragment(run_every=2)
def live_panel():
    s = E.snapshot_state()
    live = E.live()
    vel = live["velocity"]
    st_ = s["stats"]
    c = st.columns(6)
    c[0].metric("Пулов", st_.get("pools", "—"))
    c[1].metric("Прошли фильтр", st_.get("passed_filter", "—"))
    c[2].metric("Сделок", st_["trades"])
    c[3].metric("Winrate", "—" if st_["winrate"] is None else f"{st_['winrate']:.0%}")
    c[4].metric("PnL", f"${st_['pnl_usd']:+.2f}")
    c[5].metric("Скан", "идёт…" if s["scanning"] else
                (f"{time.time() - s['last_scan']:.0f}с назад" if s["last_scan"] else "первый скан…"))
    for err in (s["last_error"], live["ticker_error"] and "Тикер: " + live["ticker_error"]):
        if err:
            st.error(err)

    st.subheader("Топ-10 по перевесу покупок")
    rows = []
    for p in s["rows"]:
        f, v, e = p.get("flow") or {}, vel.get(p["mint"]) or {}, p.get("econ") or {}
        why = (p.get("entry_missing") or []) + (p.get("holder_reasons") or [])
        rows.append({
            "Монета": p["symbol"], "Ликвидность": usd(p["liquidity_usd"]),
            "Покуп/Прод 5м": rx(f.get("5m")), "Покуп/Прод 15м": rx(f.get("15m")),
            "1с": pct(v.get("v1"), 2), "15с": pct(v.get("v15")), "30с": pct(v.get("v30")),
            "Инерция %/мин": v.get("status") if "v1" not in v else pct(v.get("inertia")),
            "Buy $ 15м": usd((f.get("15m") or {}).get("buy_usd")),
            "Размер": usd(e.get("size_usd")) if e else "—",
            "Издержки": f"{e['cost']['total']:.1f}%" if e else "—",
            "Нужно / ист.": f"≥{e['need_pct']:.1f}% / {pct(e.get('expected_pct'))} (n={e['samples']})" if e else "—",
            "Экономика": VERD.get(e.get("verdict"), "—") if e else "—",
            "Сигнал": SIG.get(p.get("signal"), p.get("signal")),
            "Почему нет": why[0] if why and p.get("signal") != "holding" else "",
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    else:
        st.info("Первый скан идёт ~30–60 сек…")

    st.subheader("Открытые позиции")
    if not s["positions"]:
        st.caption("нет")
    for pos in s["positions"]:
        lp = live["positions"].get(pos["mint"], {})
        ws = (lp.get("whales") or pos.get("whales") or {}).get("wallets", [])
        cr = next((w for w in ws if w["role"] == "creator"), None)
        t3 = [w for w in ws if w["role"] == "top3"]
        drop = lambda w: f"−{w['drop_pct']:.0f}%" if w["drop_pct"] > 0 else "держит"
        v = vel.get(pos["mint"]) or {}
        cols = st.columns([3, 1])
        cols[0].markdown(
            f"**{pos['symbol']}** · PnL **{pct(lp.get('pnl_pct', pos['pnl_pct']))}** · ${pos['size_usd']:.0f} · "
            f"издержки {pos['cost_pct']:.1f}% · {(time.time() - pos['entry_time']) / 60:.0f} мин  \n"
            f"перевес 15м: {('×%.1f' % pos['ratio_15m']) if pos.get('ratio_15m') else '—'} · "
            f"скорость 15с/30с: {pct(v.get('v15'))} / {pct(v.get('v30'))}  \n"
            f"создатель: {(drop(cr) if cr['was'] > 0 else 'вышел до входа') if cr else 'не определён'} · "
            f"топ-3: {', '.join(w['label'] + ' ' + drop(w) for w in t3) or '—'}")
        if cols[1].button("Закрыть", key="close_" + pos["mint"]):
            E.close(pos["mint"], "закрыто вручную")

    st.subheader("Проверка гипотезы: растут ли монеты с перевесом покупок?")
    r = s["research"]
    cell = lambda x, h: (f"{x[f'm{h}']:+.1f}% · {x[f'w{h}']:.0%} росли · n={x[f'n{h}']}" if x[f"n{h}"] else "—")
    st.dataframe(pd.DataFrame([{"Покуп/Прод 15м": x["bucket"], "через 5м": cell(x, 5), "через 15м": cell(x, 15),
                                "через 30м": cell(x, 30)} for x in r["rows"]]), hide_index=True, use_container_width=True)
    st.caption(f"Среднее без 10% крайних · доля выросших · число замеров. Ожидают результата: {r['pending']}.")

    st.subheader("Закрытые сделки")
    if s["closed"]:
        st.dataframe(pd.DataFrame([{
            "Монета": c["symbol"], "Размер": usd(c["size_usd"]), "PnL": pct(c["pnl_pct"]),
            "$": f"{c['pnl_usd']:+.2f}", "Держали, мин": round((c["exit_time"] - c["entry_time"]) / 60),
            "Причина выхода": c["exit_reason"]} for c in s["closed"]]), hide_index=True, use_container_width=True)
    else:
        st.caption("пока нет")
    with st.expander("Журнал"):
        for ev in s["events"]:
            st.text(time.strftime("%H:%M:%S", time.localtime(ev["t"])) + "  " + ev["text"])


live_panel()

with st.expander("Холдеры монеты (on-chain)"):
    opts = {p["symbol"]: p["mint"] for p in E.rows}
    if opts:
        sym = st.selectbox("Монета", list(opts))
        if st.button("Проверить холдеров"):
            with st.spinner("топ-кошельки и кто их фондировал, 10–30 сек…"):
                h = E.get_holders(opts[sym], force=True)
            if h.get("error"):
                st.error(h["error"])
            else:
                st.write(("✅ проходит" if not h["reasons"] else "❌ " + "; ".join(h["reasons"])) +
                         f" · топ-1 {h['top1_pct']:.1f}% · топ-10 {h['top10_pct']:.1f}% · создатель "
                         f"{h['creator'] or 'не найден'} ({h['creator_pct']:.2f}%)")
                for c in h["clusters"]:
                    st.write(f"{c['label']}: {len(c['wallets'])} кошельков, {c['pct']}%")
                st.dataframe(pd.DataFrame([{"Кошелёк": x["label"], "Тип": "создатель" if x.get("is_creator") else x["kind"],
                                            "% supply": round(x["pct"], 2), "Кластер": x.get("cluster") or ""}
                                           for x in h["holders"][:20]]), hide_index=True)
    else:
        st.caption("дождись первого скана")

with st.expander("Самопроверка на живых данных (~45 сек)"):
    st.caption("Проверяет PumpSwap-пулы, чтение хранилищ, цену on-chain vs GeckoTerminal, создателя, холдеров, поток. "
               "Скопируй вывод и пришли в чат.")
    if st.button("Запустить самопроверку"):
        from memescan import diag
        buf = io.StringIO()
        with st.spinner("проверяю…"), contextlib.redirect_stdout(buf):
            try:
                diag.main()
            except Exception as ex:  # report, don't crash the app
                print(f"[FAIL] {type(ex).__name__}: {ex}")
        st.code(buf.getvalue())

with st.expander("Настройки"):
    with st.form("cfg"):
        keys = [("entry_ratio_15m", "Вход: покуп/прод 15м ≥ ×"), ("entry_ratio_5m", "Вход: покуп/прод 5м ≥ ×"),
                ("exit_ratio_15m", "Держим пока 15м ≥ ×"), ("creator_exit_drop_pct", "Выход: создатель продал ≥ %"),
                ("top3_exit_drop_pct", "Выход: топ-3 продал ≥ %"), ("stop_loss_pct", "Стоп %"),
                ("min_liquidity_usd", "Мин. ликвидность $"), ("min_age_hours", "Мин. возраст, ч"),
                ("bankroll_usd", "Банк $"), ("risk_per_trade_pct", "Риск на сделку % банка"),
                ("fee_per_side_pct", "Комиссия %/сторона"), ("min_edge_multiple", "Ожидание ≥ × издержек")]
        vals = {k: st.number_input(label, value=float(getattr(cfg, k))) for k, label in keys}
        if st.form_submit_button("Сохранить"):
            cfg.update(vals)
            cfg.save()
            st.success("сохранено")
