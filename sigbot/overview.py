"""Сводный бэктест всех блоков для панели: кривые капитала, годы, месяцы, сделки, маркеры позиций.
`python -m sigbot report` пишет reports/overview.json, панель рисует его на /overview."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import listings_bt, portfolio

DAY = 86_400_000


def _ms(d: str) -> int:
    return int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _curve(p: pd.Series) -> list:
    eq = (1 + p.fillna(0)).cumprod()
    return [[int(t // 1000), round(float(v), 5)] for t, v in eq.items()]


def build(conn, cfg, start="2020-07-01", end="2026-11-01", refresh=False) -> dict:
    s, e = _ms(start), _ms(end)
    pc = cfg["portfolio"]
    C, V, FR = portfolio.daily_frames(conn, None, s - 400 * DAY, e)
    W = portfolio.weights(C, V, pc, FR=FR)
    d = portfolio.simulate(C, FR, W, cfg["trade"]["fee_taker_pct"])
    tp = d.pnl[(d.index >= s) & (d.index < e)]
    D = listings_bt.load(refresh)
    from . import settings
    kinds = settings.get(conn, "perp_kinds") or {}           # акции и сырьё на Binance: не шортим как листинги
    lp, trades = listings_bt.backtest(D, cfg["listings"], cfg["trade"]["fee_taker_pct"], s, e,
                                      exclude={k for k, v in kinds.items() if v != "COIN"})
    lp = lp.reindex(tp.index).fillna(0)
    liq = V.rolling(30, min_periods=20).mean()
    uni = (liq.rank(axis=1, ascending=False) <= pc["top_n"]).shift(1).fillna(False)
    bh = C.pct_change().where(uni).mean(1).fillna(0).reindex(tp.index).fillna(0)
    btc = C["BTCUSDT"].pct_change().fillna(0).reindex(tp.index).fillna(0)
    series = {"Итог: портфель + листинги": tp + lp, "Трендовый портфель": tp, "Шорт листингов": lp,
              "Buy and hold топ 30": bh, "BTC": btc}
    stats = {k: portfolio.stats(pd.DataFrame({"pnl": v, "fees": 0.0, "funding": 0.0, "gross": 0.0})) for k, v in series.items()}
    comb = series["Итог: портфель + листинги"]
    months = (1 + comb).groupby(pd.to_datetime(comb.index, unit="ms").to_period("M")).prod() - 1
    # маркеры позиций портфеля: вход, разворот, выход по каждой монете
    marks = {}
    side = np.sign(W.where(W.abs() >= 0.001, 0)).loc[(W.index >= s) & (W.index < e)]
    for sym in side.columns:
        x = side[sym]
        ch = x[x.diff().fillna(x) != 0]
        if len(ch):
            marks[sym] = [[int(t // 1000) + 86400, int(v)] for t, v in ch.items()]      # исполнение на следующий день
    return {"generated": int(time.time()), "period": [start, end], "params_version": cfg["params_version"],
            "curves": {k: _curve(v) for k, v in series.items()}, "stats": stats,
            "months": {str(k): round(float(v) * 100, 2) for k, v in months.items()},
            "listing_trades": sorted(trades, key=lambda x: -x["entry_t"]),
            "portfolio_marks": marks}


def save(rep: dict, out_dir="reports") -> Path:
    p = Path(out_dir)
    p.mkdir(exist_ok=True)
    f = p / "overview.json"
    f.write_text(json.dumps(rep))
    return f


def to_text(rep: dict) -> str:
    st = rep["stats"]
    lines = [f"Сводный бэктест {rep['period'][0]} .. {rep['period'][1]}, версия {rep['params_version']}", "",
             f"{'':28}{'годовая':>9}{'просадка':>10}{'Sharpe':>8}{'мес. ср':>9}"]
    for k, v in st.items():
        lines.append(f"{k:28}{v['cagr_pct']:>8}%{v['max_drawdown_pct']:>9}%{v['sharpe']:>8}{v['avg_month_pct']:>8}%")
    lines += ["", "По годам, %: " + ", ".join(f"{y}: {v}" for y, v in st["Итог: портфель + листинги"]["years"].items()),
              f"Сделок шорта листингов: {len(rep['listing_trades'])}"]
    return "\n".join(lines)
