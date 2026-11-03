"""Тонкая прослойка между dragon_core.step() и бэктестом/paper. Логику ядра не трогает — только вызывает и читает.

Всё, что приходится предполагать о форме ответа step(), собрано здесь. Если ядро отвечает иначе —
normalize() падает с понятной ошибкой, правка — в одном месте.
"""
import inspect

import dragon_core

LONG = {"LONG", "BUY", "ENTER_LONG", "OPEN_LONG", "GO_LONG"}
SHORT = {"SHORT", "SELL", "ENTER_SHORT", "OPEN_SHORT", "GO_SHORT"}
FLAT = {"FLAT", "EXIT", "CLOSE", "CLOSE_ALL", "EXIT_LONG", "EXIT_SHORT", "CLOSE_LONG", "CLOSE_SHORT"}
KEEP = {"HOLD", "KEEP", "WAIT", "NONE", "NO_TRADE", "NOTRADE", "SKIP", "NEUTRAL", "STAY", ""}

FEATURES = ("liq_long_z", "liq_short_z", "cvd_chg")


class CoreShapeError(ValueError):
    pass


def _nparams():
    try:
        ps = [p for p in inspect.signature(dragon_core.step).parameters.values()
              if p.default is inspect.Parameter.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        return len(ps)
    except (TypeError, ValueError):
        return 1


_N = None


def call_step(window, state=None):
    """→ (raw_result, new_state). step(df) или step(df, state); ответ (result, state) тоже понимаем."""
    global _N
    if _N is None:
        _N = _nparams()
    raw = dragon_core.step(window) if _N <= 1 else dragon_core.step(window, state)
    if isinstance(raw, tuple) and len(raw) == 2 and _N >= 2:
        return raw[0], raw[1]
    return raw, state


def _get(obj, *keys):
    for k in keys:
        if isinstance(obj, dict) and k in obj:
            return obj[k]
        if not isinstance(obj, dict) and hasattr(obj, k):
            return getattr(obj, k)
    return None


def normalize(raw, current_pos):
    """→ dict(target=-1|0|1, decision=str, score, reasons, plan, features)."""
    if raw is None:
        raise CoreShapeError("step() вернул None")
    if isinstance(raw, (str, int, float)):
        raw = {"decision": raw}
    dec = _get(raw, "decision", "action", "signal", "side", "direction")
    if dec is None:
        raise CoreShapeError(f"в ответе step() нет decision/action/signal: {type(raw).__name__} "
                             f"{list(raw.keys()) if isinstance(raw, dict) else dir(raw)[:20]}")
    if isinstance(dec, (int, float)) and not isinstance(dec, bool):
        target = (dec > 0) - (dec < 0)
        name = {1: "LONG", -1: "SHORT", 0: "FLAT"}[target]
    else:
        name = str(getattr(dec, "name", dec)).strip().upper().replace(" ", "_").replace("-", "_")
        if name in LONG:
            target = 1
        elif name in SHORT:
            target = -1
        elif name in FLAT:
            target = 0
        elif name in KEEP:
            target = current_pos
        else:
            raise CoreShapeError(f"неизвестное решение step(): {dec!r}. Добавь его в core_adapter.LONG/SHORT/FLAT/KEEP")
    reasons = _get(raw, "reasons", "reason", "why")
    if isinstance(reasons, (list, tuple)):
        reasons = "; ".join(map(str, reasons))
    feats = {}
    for src in (raw, _get(raw, "features"), _get(raw, "metrics"), _get(raw, "signals"), _get(raw, "blocks")):
        if src is None:
            continue
        for f in FEATURES:
            v = _get(src, f)
            if v is not None and f not in feats:
                try:
                    feats[f] = float(v)
                except (TypeError, ValueError):
                    pass
    return {"target": target, "decision": name, "score": _get(raw, "score", "confidence"),
            "reasons": reasons, "plan": _get(raw, "plan", "trade_plan"), "features": feats}


def plan_levels(plan, side):
    """Стоп/тейк из плана, если ядро их даёт числами. → (stop, take) или (None, None)."""
    if not isinstance(plan, dict):
        return None, None

    def num(*ks):
        v = _get(plan, *ks)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return num("stop", "stop_loss", "sl", "stop_price"), num("take", "take_profit", "tp", "target", "target_price")


def feature_frame(df):
    """Признаки ядра (liq_long_z, liq_short_z, cvd_chg), если ядро даёт функцию для их расчёта. Иначе None."""
    for name in ("features", "compute_features", "build_features", "add_features", "enrich", "prepare"):
        fn = getattr(dragon_core, name, None)
        if callable(fn):
            try:
                out = fn(df.copy())
            except Exception:
                continue
            if hasattr(out, "columns") and all(f in out.columns for f in FEATURES):
                return out[list(FEATURES)], f"dragon_core.{name}()"
    return None, None
