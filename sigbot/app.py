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
            if port and eff.get("portfolio", {}).get("enabled"):
                try:
                    await port.manage(now)                  # цели и стопы открытых сигналов тренда по часовым свечам
                except Exception as e:
                    log.exception("trend targets: %s", e)
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
            if time.gmtime(now / 1000).tm_hour == 3 and settings.get(conn, "sched_feed") != now // DAY:
                settings.put(conn, "sched_feed", now // DAY)           # бэктест ленты обновляется раз в сутки
                try:
                    from . import autoprep
                    await asyncio.to_thread(autoprep._feed_sync, cfg)
                except Exception as e:
                    log.exception("feed: %s", e)
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


async def auto_update(pub):
    """Раз в 2 минуты: есть новые изменения на GitHub, значит пишем владельцу и выходим с кодом 3.
    Сторож (updater.supervise) скачивает их и сразу запускает бота заново. Выключить: AUTO_UPDATE=0 в .env."""
    from . import updater
    if os.environ.get("AUTO_UPDATE", "1") == "0" or not os.environ.get("SIGBOT_CHILD"):
        return
    while True:
        await asyncio.sleep(int(os.environ.get("SIGBOT_UPDATE_SECONDS", 120)))
        try:
            new = await asyncio.to_thread(updater.pending)
            if new:
                msg = "🔄 Ставлю обновление, бот перезапустится за 10–30 секунд:\n" + "\n".join("• " + x[:160] for x in new[:3])
                print("\n" + msg, flush=True)
                await pub.notify_owner(msg)
                os._exit(updater.UPDATE_EXIT)
        except Exception as e:
            log.warning("auto update check: %s", e)


async def heartbeat(conn, leader):
    """Раз в 5 минут короткая строка в консоль: видно, что бот жив и что он делает."""
    while True:
        await asyncio.sleep(300)
        now = int(time.time() * 1000)
        last = conn.execute("SELECT MAX(ts) FROM candles WHERE tf='1h' AND market='futures'").fetchone()[0]
        age = f"{(now - last) // 60000 - 60} мин назад" if last else "нет"
        scan = settings.get(conn, "last_scan") or {}
        posts = conn.execute("SELECT COUNT(*) FROM posts WHERE ts>?", (now - DAY,)).fetchone()[0]
        role = "основной" if leader and leader.is_main else "запасной" if leader else "без Telegram"
        print(f"{time.strftime('%H:%M')} работаю ({role}): последняя часовая свеча закрылась {age}, "
              f"последний скан {time.strftime('%H:%M', time.gmtime(scan['ts'] / 1000)) + ' UTC' if scan.get('ts') else 'ещё не было'}, "
              f"кандидатов {scan.get('candidates', 0)}, постов за сутки {posts}", flush=True)


async def run(cfg, conn):
    http = Http()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    channel = os.environ.get("TELEGRAM_CHANNEL_ID")
    owner = os.environ.get("TELEGRAM_OWNER_ID")
    tg = Telegram(token, http) if token else None
    lc = cfg["llm"]
    llm = Gemini(os.environ.get("GEMINI_API_KEY"), http, lc["model"], lc["fallback_model"], lc["timeout_s"])
    leader = None
    if tg and owner:
        import socket
        from .leader import Leader
        leader = Leader(tg, int(owner), os.environ.get("SIGBOT_NAME") or socket.gethostname().lower(), conn=conn)
        try:
            await leader.tick()
        except Exception as e:
            log.warning("leader: %s", e)
    pub = Publisher(cfg, conn, tg, channel, owner, leader)
    bl = Bloggers(cfg, conn)
    live = Live(cfg, conn, pub, llm, bl)
    from .portfolio import LivePortfolio
    port = LivePortfolio(cfg, conn, pub)
    from .listings import ListingShorts
    from .sources import Binance
    lst = ListingShorts(cfg, conn, pub, Binance(http))
    col = Collector(cfg, conn, http)

    say = lambda m: print(m, flush=True)
    say("\n=== sigbot запускается ===")
    try:
        runner = await dashboard.serve(cfg, conn)
    except OSError:
        say(f"❌ Порт {cfg['dashboard']['port']} занят: бот уже запущен в другом окне "
            "(run_sigbot.bat, автозапуск или второй VS Code). Закрой его и запусти снова.")
        await http.close()
        raise SystemExit(1)
    say(f"✅ Панель: http://{cfg['dashboard']['host']}:{cfg['dashboard']['port']}  (обзор: /overview)")
    say(f"✅ Telegram: {'основной экземпляр' if leader and leader.is_main else 'запасной, в канал не пишет' if leader else 'нет токена, посты не уходят'}")
    tasks = [asyncio.create_task(_guard(fn.__name__, fn)) for fn in col.tasks()]
    tasks.append(asyncio.create_task(_guard("auto_update", lambda: auto_update(pub))))
    tasks.append(asyncio.create_task(_guard("scheduler", lambda: scheduler(cfg, conn, live, pub, port, lst))))
    if tg and owner:
        from .admin import Admin
        admin = Admin(cfg, conn, tg, int(owner), pub, live, llm, bl, col, leader)
        tasks.append(asyncio.create_task(_guard("admin", admin.loop)))
        tasks.append(asyncio.create_task(_guard("leader", leader.loop)))
        upd = os.environ.pop("SIGBOT_UPDATED", None)
        await pub.notify_owner((f"{upd}\n\n" if upd else "") +
                               f"✅ Бот запущен на компьютере {leader.name}, он основной. Настройки: /menu")
    tasks.append(asyncio.create_task(_guard("heartbeat", lambda: heartbeat(conn, leader))))
    from . import autoprep
    asyncio.create_task(autoprep.run(cfg, conn, http))
    say("✅ Сборщик, сканер, портфель, листинги и админка работают.")
    say("Бот не завершается сам: это служба. Раз в 5 минут здесь строка «работаю». Остановить: Ctrl+C или Shift+F5.\n")
    try:
        await asyncio.gather(*tasks)
    finally:
        await runner.cleanup()
        await http.close()
