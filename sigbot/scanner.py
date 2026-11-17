"""Отбор: ранжирование кандидатов, дневной лимит, фильтр корреляции."""
from __future__ import annotations

import pandas as pd

from .engine import Candidate


def correlation_matrix(closes: dict[str, pd.Series], window: int) -> pd.DataFrame:
    rets = pd.DataFrame({k: v.pct_change() for k, v in closes.items()}).tail(window)
    return rets.corr().fillna(0)


def select(cands: list[Candidate], open_trades: list[tuple[str, str]], taken_today: int,
           max_per_day: int, corr: pd.DataFrame | None, corr_threshold: float) -> list[Candidate]:
    """open_trades: [(symbol, side)]. Одновременные сделки в одну сторону по коррелированным
    монетам считаются одной ставкой: вторая отбрасывается."""
    chosen: list[Candidate] = []
    busy = list(open_trades)
    for c in sorted(cands, key=lambda c: c.strength, reverse=True):
        if taken_today + len(chosen) >= max_per_day:
            break
        if any(sym == c.symbol for sym, _ in busy):
            continue
        same_bet = False
        for sym, side in busy:
            if side != c.side:
                continue
            rho = 0.0
            if corr is not None and c.symbol in corr.index and sym in corr.columns:
                rho = float(corr.loc[c.symbol, sym])
            if rho >= corr_threshold:
                same_bet = True
                break
        if same_bet:
            continue
        chosen.append(c)
        busy.append((c.symbol, c.side))
    return chosen
