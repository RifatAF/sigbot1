"""Ведение сделки по заранее заданным правилам. Одна функция для бэктеста (1ч бары) и живой работы (1м бары).

Порядок проверок внутри бара консервативный: сначала стоп, потом цели.
Усреднения нет. Второго входа в v1 нет.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TradeState:
    side: str
    entry: float
    stop: float
    invalidation: float
    tp1: float
    tp2: float
    opened_ts: int
    remaining: float = 1.0
    be_moved: bool = False
    tp1_done: bool = False
    status: str = "open"           # open | closed
    exits: list = field(default_factory=list)   # [(ts, event, price, fraction)]
    trail: float = 0.0             # трейлинг: расстояние стопа от дневного закрытия (в цене), 0 = нет
    max_hold_h: float | None = None
    tp1_frac: float | None = None  # None = из конфига
    use_breakeven: bool = True
    last_close: float | None = None
    last_day: int | None = None
    trailed: bool = False

    @property
    def sgn(self) -> int:
        return 1 if self.side == "long" else -1


@dataclass
class Event:
    ts: int
    event: str          # breakeven | trail | tp1 | tp2 | stop | breakeven_stop | invalidation | timeout
    price: float
    fraction: float     # доля исходной позиции, закрытая событием


def step(st: TradeState, ts: int, high: float, low: float, close: float, cfg: dict,
         bar_closed: bool = True) -> list[Event]:
    """Обработать один бар. bar_closed=True означает, что close финальный (для правила отмены идеи)."""
    if st.status != "open":
        return []
    tc, ec = cfg["trade"], cfg["engine"]
    s = st.sgn
    ev: list[Event] = []

    def close_part(event, price, frac):
        frac = min(frac, st.remaining)
        st.remaining = round(st.remaining - frac, 10)
        st.exits.append((ts, event, price, frac))
        ev.append(Event(ts, event, price, frac))
        if st.remaining <= 1e-9:
            st.status = "closed"

    adverse = low if s == 1 else high
    favorable = high if s == 1 else low

    # 1. стоп (исходный или безубыток)
    if s * (adverse - st.stop) <= 0:
        name = ("trail_stop" if s * (st.stop - st.entry) > 0 else "breakeven_stop") if (st.be_moved or st.trailed) else "stop"
        close_part(name, st.stop, st.remaining)
        return ev

    # 2. цели
    frac1 = tc["tp1_close_fraction"] if st.tp1_frac is None else st.tp1_frac
    if not st.tp1_done and frac1 > 0 and s * (favorable - st.tp1) >= 0:
        st.tp1_done = True
        close_part("tp1", st.tp1, frac1)
    if st.status == "open" and s * (favorable - st.tp2) >= 0:
        close_part("tp2", st.tp2, st.remaining)
        return ev

    # 3. безубыток после половины пути к tp2
    trigger = st.entry + s * abs(st.tp2 - st.entry) * tc["breakeven_at_fraction_to_tp2"]
    if st.use_breakeven and not st.be_moved and s * (favorable - trigger) >= 0:
        st.be_moved = True
        st.stop = st.entry
        ev.append(Event(ts, "breakeven", st.entry, 0.0))

    # 4. отмена идеи: закрытие бара за уровнем invalidation
    if bar_closed and st.status == "open" and s * (close - st.invalidation) < 0:
        close_part("invalidation", close, st.remaining)
        return ev

    # 5. трейлинг: на первом баре новых суток стоп подтягивается от закрытия прошлых суток
    if bar_closed and st.trail > 0:
        day = ts // 86_400_000
        if st.last_day is not None and day > st.last_day and st.last_close is not None:
            new = st.last_close - s * st.trail
            if s * (new - st.stop) > 0:
                st.stop = new
                st.trailed = True
                ev.append(Event(ts, "trail", new, 0.0))
        st.last_day, st.last_close = day, close

    # 6. время
    hold = ec["max_hold_hours"] if st.max_hold_h is None else st.max_hold_h
    if bar_closed and st.status == "open" and ts - st.opened_ts >= hold * 3_600_000:
        close_part("timeout", close, st.remaining)
    return ev


def state_from(c, ts: int | None = None) -> TradeState:
    """TradeState из кандидата: правила ведения сетапа лежат в c.features["manage"]."""
    m = c.features.get("manage", {})
    return TradeState(c.side, c.entry, c.stop, c.invalidation, c.tp1, c.tp2, c.ts if ts is None else ts,
                      trail=m.get("trail", 0.0), max_hold_h=m.get("max_hold_h"), tp1_frac=m.get("tp1_frac"),
                      use_breakeven=m.get("breakeven", True))


def pnl_pct(st: TradeState, leverage: float, fee_pct: float, funding_paid_pct: float = 0.0) -> float:
    """Результат в % депозита: движение цены * плечо − комиссии − фандинг."""
    s = st.sgn
    gross = sum(frac * s * (price - st.entry) / st.entry for _, _, price, frac in st.exits) * leverage * 100
    closed = sum(frac for *_, frac in st.exits)
    fees = leverage * fee_pct * (1 + closed)   # вход + выходы, % от позиции
    return gross - fees - funding_paid_pct
