from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Sizing:
    risk_pct: float          # риск в % депозита
    position_usd: float
    leverage: float          # эффективное плечо = позиция / депозит
    capped: bool             # плечо урезано потолком
    liq_price: float         # примерная цена ликвидации при изолированной марже


def risk_for_strength(strength: float, rmin: float, rmax: float) -> float:
    s = min(max(strength, 0.0), 1.0)
    return round(rmin + (rmax - rmin) * s, 2)


def size(entry: float, stop: float, side: str, strength: float, deposit: float,
         rmin: float, rmax: float, lev_cap: float, mm_pct: float = 0.5) -> Sizing:
    if side not in ("long", "short"):
        raise ValueError(side)
    dist = (entry - stop) / entry if side == "long" else (stop - entry) / entry
    if dist <= 0:
        raise ValueError("stop is on the wrong side of entry")

    risk_pct = risk_for_strength(strength, rmin, rmax)
    position = deposit * risk_pct / 100 / dist
    lev = position / deposit
    capped = False
    if lev > lev_cap:
        # потолок плеча режет позицию, а значит и фактический риск, сигнал остаётся
        capped = True
        lev = lev_cap
        position = deposit * lev
        risk_pct = round(position * dist / deposit * 100, 2)

    # ликвидация при марже = позиция/плечо: движение против ≈ 1/lev − mm
    liq_move = max(1 / lev - mm_pct / 100, 0) if lev > 0 else 1.0
    liq = entry * (1 - liq_move) if side == "long" else entry * (1 + liq_move)
    return Sizing(risk_pct, round(position, 2), round(lev, 2), capped, liq)


def stop_before_liquidation(s: Sizing, stop: float, side: str) -> bool:
    return stop > s.liq_price if side == "long" else stop < s.liq_price


@dataclass
class Guard:
    allow: bool
    multiplier: float
    reason: str


def guard(rc: dict, results: list, open_risk_pct: list[float], now: int | None = None) -> Guard:
    """Портфельный предохранитель перед новым входом.

    results: закрытые сделки по порядку, доходность в % депозита или пары (ts закрытия, %);
    open_risk_pct: риск открытых сделок. Просадка от пика и серия убытков уменьшают риск.
    Глубокая просадка ставит входы на паузу на dd_pause_days, после паузы пик считается заново."""
    if len(open_risk_pct) >= rc.get("max_open_trades", 99):
        return Guard(False, 0.0, "max open trades")
    pairs = [r if isinstance(r, (tuple, list)) else (None, r) for r in results]
    cool = rc.get("dd_pause_days", 7) * 86_400_000
    eq, peak = 1.0, 1.0
    for ts, r in pairs:
        eq *= 1 + r / 100
        peak = max(peak, eq)
        if (1 - eq / peak) * 100 >= rc.get("dd_pause_pct", 100):
            if ts is None or now is None or now - ts < cool:
                return Guard(False, 0.0, f"drawdown {(1 - eq / peak) * 100:.1f}% pause")
            peak = eq   # пауза отбыта: отсчёт просадки заново
    dd = (1 - eq / peak) * 100
    mult = 1.0
    reason = []
    if dd >= rc.get("dd_half_pct", 100):
        mult *= 0.5
        reason.append(f"drawdown {dd:.1f}%")
    streak = 0
    for _, r in reversed(pairs):
        if r > 0:
            break
        streak += 1
    if streak >= rc.get("loss_streak", 99):
        mult *= 0.5
        reason.append(f"{streak} losses in a row")
    room = rc.get("max_open_risk_pct", 100) - sum(open_risk_pct)
    if room < rc["risk_min_pct"] * 0.5:
        return Guard(False, 0.0, "max open risk")
    return Guard(True, mult, ", ".join(reason))


def size_guarded(entry, stop, side, strength, deposit, rc, g: Guard, open_risk_pct: list[float],
                 lev_cap: float | None = None) -> Sizing | None:
    """Размер с учётом предохранителя, остатка лимита открытого риска и минимальной ширины стопа."""
    dist = abs(entry - stop) / entry * 100
    if dist < rc.get("min_stop_pct", 0):
        return None   # стоп так близко, что комиссия съедает заметную часть R
    room = rc.get("max_open_risk_pct", 100) - sum(open_risk_pct)
    rmax = min(rc["risk_max_pct"] * g.multiplier, room)
    rmin = min(rc["risk_min_pct"] * g.multiplier, rmax)
    if not g.allow or rmax <= 0:
        return None
    return size(entry, stop, side, strength, deposit, rmin, rmax,
                lev_cap or rc["public_leverage_cap"], rc["maintenance_margin_pct"])
