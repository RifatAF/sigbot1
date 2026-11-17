"""Один процесс: сборщик, сканер, ведение сделок, публикации, админка, панель."""
from __future__ import annotations

import asyncio
import logging
import os
import time

from . import dashboard, postmortem, settings
from .bloggers import Bloggers
from .collector import Collector
from .http import Http
from .live import Live
from .llm import Gemini
from .publisher import Publisher
from .telegram import Telegram

log = logging.getLogger(__name__)
HOUR = 3_600_000
DAY = 86_400_000


async def _guard(name, coro_fn):
    """Перезапуск упавшей задачи: процесс не должен умирать от одной ошибки."""
    while True:
        try:
            await coro_fn()
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("task %s crashed: %s; restart in 10s", name, e)
            await asyncio.sleep(10)


async def scheduler(cfg, conn, live: Live, pub: Publisher, port=None, lst=None):
    """Скан в :02 каждого часа, ведение сделок каждую минуту, ежедневная заметка, недельные посты."""
    last_scan_hour = settings.get(conn, "sched_last_scan_hour", 0)
    while True:
        now = int(time.time() * 1000)
        try:
            await live.manage(now)
        except Exception as e:
            log.exception("manage: %s", e)
        hour = now // HOUR
        if hour != last_scan_hour and now % HOUR >= 2 * 60_000:
            last_scan_hour = hour
            settings.put(conn, "sched_last_scan_hour", hour)
            try:
                await live.scan(hour * HOUR)
            except Exception as e:
                log.exception("scan: %s", e)
            eff = settings.effective(cfg, conn)
            sc = eff["schedule"]
            if port and eff.get("portfolio", {}).get("enabled") and not eff["publishing"].get("kill_switch") \
                    and (time.gmtime(now / 1000).tm_hour - eff["portfolio"].get("post_hour_utc", 0)) % 24 < 3:
                # три попытки: если часовая свеча 23:00 ещё не пришла, пересчёт через час (повтор за те же сутки не считается)
                syms = settings.get(conn, "symbols_manual") or settings.get(conn, "universe", [])
                try:
                    await port.run_daily(now, syms)
                except Exception as e:
                    log.exception("portfolio: %s", e)
            if lst and eff.get("listings", {}).get("enabled") and not eff["publishing"].get("kill_switch") \
                    and time.gmtime(now / 1000).tm_hour in (0, 6, 12, 18):
                try:
                    await lst.run_daily(now)
                except Exception as e:
                    log.exception("listings: %s", e)
            h_utc = time.gmtime(now / 1000).tm_hour
            wday = time.gmtime(now / 1000).tm_wday
            day = now // DAY
            if h_utc == sc["daily_note_hour_utc"] and settings.get(conn, "sched_daily") != day:
                settings.put(conn, "sched_daily", day)
                await live.daily_note()
            if wday == sc["weekly_day"] and h_utc == sc["weekly_hour_utc"] and settings.get(conn, "sched_weekly") != day:
                settings.put(conn, "sched_weekly", day)
                await live.weekly()
                await pub.notify_owner(postmortem.build(conn, now - 7 * DAY, now))
        await asyncio.sleep(60 - time.time() % 60 + 1)


async def run(cfg, conn):
    http = Http()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    channel = os.environ.get("TELEGRAM_CHANNEL_ID")
    owner = os.environ.get("TELEGRAM_OWNER_ID")
    tg = Telegram(token, http) if token else None
    lc = cfg["llm"]
    llm = Gemini(os.environ.get("GEMINI_API_KEY"), http, lc["model"], lc["fallback_model"], lc["timeout_s"])
    pub = Publisher(cfg, conn, tg, channel, owner)
    bl = Bloggers(cfg, conn)
    live = Live(cfg, conn, pub, llm, bl)
    from .portfolio import LivePortfolio
    port = LivePortfolio(cfg, conn, pub)
    from .listings import ListingShorts
    from .sources import Binance
    lst = ListingShorts(cfg, conn, pub, Binance(http))
    col = Collector(cfg, conn, http)

    runner = await dashboard.serve(cfg, conn)
    log.info("dashboard on http://%s:%s", cfg["dashboard"]["host"], cfg["dashboard"]["port"])
    tasks = [asyncio.create_task(_guard(fn.__name__, fn)) for fn in col.tasks()]
    tasks.append(asyncio.create_task(_guard("scheduler", lambda: scheduler(cfg, conn, live, pub, port, lst))))
    if tg and owner:
        from .admin import Admin
        admin = Admin(cfg, conn, tg, int(owner), pub, live, llm, bl, col)
        tasks.append(asyncio.create_task(_guard("admin", admin.loop)))
        await pub.notify_owner("sigbot started. /menu for settings.")
    try:
        await asyncio.gather(*tasks)
    finally:
        await runner.cleanup()
        await http.close()
