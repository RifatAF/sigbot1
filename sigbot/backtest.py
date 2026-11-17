"""Walk forward бэктест с комиссиями и фандингом, сравнение с buy and hold и кэшем."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import engine, features, ledger, risk, scanner
from . import indicators as ind
from .trade import TradeState, pnl_pct, step

log = logging.getLogger(__name__)
HOUR = 3_600_000
DAY = 86_400_000


@dataclass
class BtTrade:
    symbol: str
    side: str
    entry_ts: int
    exit_ts: int
    entry: float
    stop: float
    tp1: float
    tp2: float
    strength: float
    risk_pct: float
    leverage: float
    pnl_pct: float
    r: float
    fees_pct: float
    funding_pct: float
    exit_event: str
    exits: list
    divergences: list
    trend_4h: int
    fear_greed: int | None


def _ms(d: str) -> int:
    return int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp() * 1000)


def load(conn, cfg, symbols, start_ms, end_ms) -> dict[str, pd.DataFrame]:
    warm = (cfg["indicators"]["zscore_window"] + 300) * HOUR
    out = {}
    for s in symbols:
        df = features.build(conn, s, start_ms - warm, end_ms + 10 * DAY)
        if len(df) < cfg["indicators"]["zscore_window"] + 50:
            log.warning("%s: not enough history (%d bars), skipped", s, len(df))
            continue
        out[s] = engine.prepare(df.reset_index(drop=True), cfg)
    return out


def candidates(prep: dict[str, pd.DataFrame], cfg, min_strength: float) -> dict[int, list]:
    """Все кандидаты по всем символам, сгруппированные по ts бара."""
    by_ts: dict[int, list] = {}
    floor = cfg["engine"]["min_divergence_score"]
    for sym, p in prep.items():
        idx = np.flatnonzero((p.bull.to_numpy() >= floor) | (p.bear.to_numpy() >= floor))
        for i in idx:
            c = engine.decide(p, int(i), sym, cfg, min_strength=min_strength)
            if c is None:
                continue
            lo = max(0, i - 720)
            if "oi" in p and p.oi.iloc[lo:i + 1].notna().sum() > 24:
                lm = ind.liquidation_map(p.close.iloc[lo:i + 1], p.oi.iloc[lo:i + 1].ffill().fillna(0),
                                         cfg["liq_map"]["leverages"], cfg["liq_map"]["bin_pct"])
                c2 = engine.decide(p, int(i), sym, cfg, min_strength=min_strength,
                                   liq_clusters=ind.nearest_clusters(lm, c.entry))
                c = c2 or c
            by_ts.setdefault(c.ts, []).append(c)
    return by_ts


def simulate(prep, cands_by_ts, cfg, start_ms, end_ms, deposit=1000.0) -> list[BtTrade]:
    rc, sc, tc = cfg["risk"], cfg["scanner"], cfg["trade"]
    bars = {s: p.set_index("ts") for s, p in prep.items()}
    closes = {s: b.close for s, b in bars.items()}
    open_: dict[str, tuple] = {}     # symbol -> (TradeState, Candidate, sizing, equity_at_entry, funding_paid)
    done: list[BtTrade] = []
    equity = deposit
    per_day: dict[int, int] = {}
    t = start_ms
    corr_cache = (None, -1)
    last_ts = max(int(b.index.max()) for b in bars.values())
    while t <= last_ts:
        # 1. ведение открытых сделок
        for sym in list(open_):
            st, c, sz, eq0, fpaid = open_[sym]
            b = bars[sym]
            if t not in b.index:
                continue
            row = b.loc[t]
            if (t // HOUR) % 8 == 0 and "funding" in b and np.isfinite(row.funding):
                fpaid += st.sgn * row.funding * 100 * sz.leverage * st.remaining
            step(st, t, row.high, row.low, row.close, cfg)
            if st.status == "closed":
                fee = sz.leverage * tc["fee_taker_pct"] * 2
                p = pnl_pct(st, sz.leverage, tc["fee_taker_pct"], fpaid)
                equity += eq0 * p / 100
                done.append(BtTrade(sym, c.side, c.ts, t, c.entry, c.stop, c.tp1, c.tp2, c.strength,
                                    sz.risk_pct, sz.leverage, round(p, 4), round(p / sz.risk_pct, 3),
                                    round(fee, 4), round(fpaid, 4), st.exits[-1][1],
                                    [list(map(lambda x: x if not isinstance(x, np.generic) else x.item(), e))
                                     for e in st.exits],
                                    c.features["divergences"], c.features["trend_4h"], c.features["fear_greed"]))
                del open_[sym]
            else:
                open_[sym] = (st, c, sz, eq0, fpaid)

        # 2. новые входы (только внутри окна)
        if t <= end_ms and t in cands_by_ts:
            day = t // DAY
            if corr_cache[1] != day:
                win = sc["correlation_window_hours"]
                cm = scanner.correlation_matrix({s: c.loc[:t].tail(win + 1) for s, c in closes.items()}, win)
                corr_cache = (cm, day)
            chosen = scanner.select(cands_by_ts[t], [(s, v[1].side) for s, v in open_.items()],
                                    per_day.get(day, 0), sc["max_signals_per_day"], corr_cache[0],
                                    sc["correlation_threshold"])
            for c in chosen:
                sz = risk.size(c.entry, c.stop, c.side, c.strength, equity, rc["risk_min_pct"],
                               rc["risk_max_pct"], rc["public_leverage_cap"], rc["maintenance_margin_pct"])
                st = TradeState(c.side, c.entry, c.stop, c.invalidation, c.tp1, c.tp2, c.ts)
                open_[c.symbol] = (st, c, sz, equity, 0.0)
                per_day[day] = per_day.get(day, 0) + 1
        if t > end_ms and not open_:
            break
        t += HOUR
    return done


def stats(trades: list[BtTrade], start_ms: int, end_ms: int, deposit: float = 1000.0) -> dict:
    eq = deposit
    curve = [(start_ms, eq)]
    for tr in sorted(trades, key=lambda x: x.exit_ts):
        eq *= 1 + tr.pnl_pct / 100
        curve.append((tr.exit_ts, eq))
    vals = np.array([v for _, v in curve])
    peak = np.maximum.accumulate(vals)
    dd = float(((vals - peak) / peak).min() * 100) if len(vals) else 0.0
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    gp, gl = sum(t.pnl_pct for t in wins), -sum(t.pnl_pct for t in losses)
    return {
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "return_pct": round((eq / deposit - 1) * 100, 2),
        "max_drawdown_pct": round(dd, 2),
        "avg_r": round(float(np.mean([t.r for t in trades])), 3) if trades else 0.0,
        "sum_r": round(float(np.sum([t.r for t in trades])), 2),
        "profit_factor": round(gp / gl, 2) if gl > 0 else None,
        "fees_pct": round(sum(t.fees_pct for t in trades), 2),
        "funding_pct": round(sum(t.funding_pct for t in trades), 2),
        "equity_curve": [(int(a), round(b, 2)) for a, b in curve],
    }


def buy_and_hold(prep, start_ms, end_ms, fee_pct) -> dict:
    rets = {}
    for s, p in prep.items():
        w = p[(p.ts >= start_ms) & (p.ts <= end_ms)]
        if len(w) > 1:
            rets[s] = (w.close.iloc[-1] / w.close.iloc[0] - 1) * 100 - 2 * fee_pct
    return {
        "equal_weight_pct": round(float(np.mean(list(rets.values()))), 2) if rets else 0.0,
        "btc_pct": round(rets.get("BTCUSDT", float("nan")), 2),
        "cash_pct": 0.0,
    }


def walk_forward(conn, cfg, symbols: list[str]) -> dict:
    bc = cfg["backtest"]
    start, end = _ms(bc["start"]), _ms(bc["end"])
    prep = load(conn, cfg, symbols, start, end)
    if not prep:
        raise RuntimeError("no data: run `python -m sigbot backfill` first")
    grid = bc["min_strength_grid"]
    cand_grid = {ms: candidates(prep, cfg, ms) for ms in grid}

    folds, test_trades = [], []
    t0 = start + bc["train_days"] * DAY
    while t0 < end:
        t1 = min(t0 + bc["test_days"] * DAY, end)
        tr_start = t0 - bc["train_days"] * DAY
        best, best_r = cfg["scanner"]["min_strength"], -1e9
        train_scores = {}
        for ms in grid:
            tr = simulate(prep, cand_grid[ms], cfg, tr_start, t0 - HOUR)
            tr = [x for x in tr if x.entry_ts < t0]
            score = sum(x.r for x in tr)
            train_scores[ms] = {"trades": len(tr), "sum_r": round(score, 2)}
            if len(tr) >= bc["min_trades_train"] and score > best_r:
                best, best_r = ms, score
        tt = simulate(prep, cand_grid[best], cfg, t0, t1)
        test_trades += tt
        s = stats(tt, t0, t1)
        folds.append({"test_start": t0, "test_end": t1, "min_strength": best, "train": train_scores,
                      "trades": s["trades"], "return_pct": s["return_pct"], "sum_r": s["sum_r"],
                      "bh": buy_and_hold(prep, t0, t1, cfg["trade"]["fee_taker_pct"])})
        t0 = t1

    oos_start = start + bc["train_days"] * DAY
    total = stats(test_trades, oos_start, end)
    bh = buy_and_hold(prep, oos_start, end, cfg["trade"]["fee_taker_pct"])
    per_coin = {}
    for s in prep:
        ts_ = [t for t in test_trades if t.symbol == s]
        st = stats(ts_, oos_start, end)
        per_coin[s] = {k: st[k] for k in ("trades", "win_rate_pct", "return_pct", "sum_r", "max_drawdown_pct")}
        per_coin[s]["bh_pct"] = buy_and_hold({s: prep[s]}, oos_start, end, cfg["trade"]["fee_taker_pct"])["equal_weight_pct"]
    exits = pd.Series([t.exit_event for t in test_trades]).value_counts().to_dict() if test_trades else {}
    beats = total["return_pct"] > bh["equal_weight_pct"] and total["return_pct"] > 0
    return {
        "params_version": cfg["params_version"],
        "generated": int(time.time() * 1000),
        "period": {"start": bc["start"], "end": bc["end"], "out_of_sample_start": oos_start},
        "symbols": list(prep),
        "total": total,
        "benchmarks": bh,
        "beats_buy_and_hold_and_cash": bool(beats),
        "exit_events": exits,
        "per_coin": per_coin,
        "folds": folds,
        "trades": [asdict(t) for t in test_trades],
    }


def save_report(conn, cfg, report: dict, out_dir: str = "reports") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    base = out / f"backtest_{report['params_version']}_{stamp}"
    base.with_suffix(".json").write_text(json.dumps(report, default=float))
    base.with_suffix(".md").write_text(to_markdown(report))
    params = {k: cfg[k] for k in ("indicators", "engine", "trade", "scanner", "risk", "liq_map", "backtest")}
    ledger.register_params(conn, cfg["params_version"], report["generated"], params, str(base.with_suffix(".json")))
    return base.with_suffix(".md")


def _d(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def to_markdown(r: dict) -> str:
    t, b = r["total"], r["benchmarks"]
    lines = [
        f"# Бэктест {r['params_version']}",
        "",
        f"Период вне выборки: {_d(r['period']['out_of_sample_start'])} .. {r['period']['end']}, "
        f"монет: {len(r['symbols'])}.",
        "",
        "| Показатель | Стратегия | Buy and hold (равные веса) | BTC | Кэш |",
        "|---|---|---|---|---|",
        f"| Доходность, % | {t['return_pct']} | {b['equal_weight_pct']} | {b['btc_pct']} | 0 |",
        f"| Макс. просадка, % | {t['max_drawdown_pct']} | | | 0 |",
        "",
        f"Сделок: {t['trades']}, прибыльных: {t['win_rate_pct']}%, средний R: {t['avg_r']}, сумма R: {t['sum_r']}, "
        f"profit factor: {t['profit_factor']}.",
        f"Комиссии: {t['fees_pct']}% депозита, фандинг: {t['funding_pct']}% депозита.",
        "",
        f"**Вердикт:** {'обгоняет' if r['beats_buy_and_hold_and_cash'] else 'НЕ обгоняет'} buy and hold и кэш.",
        "",
        "## Выходы", "",
        *[f"- {k}: {v}" for k, v in r["exit_events"].items()],
        "", "## По монетам", "",
        "| Монета | Сделок | Win % | Доходность % | Сумма R | B&H % |", "|---|---|---|---|---|---|",
        *[f"| {s} | {c['trades']} | {c['win_rate_pct']} | {c['return_pct']} | {c['sum_r']} | {c['bh_pct']} |"
          for s, c in sorted(r["per_coin"].items(), key=lambda x: -x[1]["sum_r"])],
        "", "## Окна walk forward", "",
        "| Тест с | по | min_strength | Сделок | Доходность % | B&H % |", "|---|---|---|---|---|---|",
        *[f"| {_d(f['test_start'])} | {_d(f['test_end'])} | {f['min_strength']} | {f['trades']} | "
          f"{f['return_pct']} | {f['bh']['equal_weight_pct']} |" for f in r["folds"]],
    ]
    return "\n".join(lines) + "\n"
