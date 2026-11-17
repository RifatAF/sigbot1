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
