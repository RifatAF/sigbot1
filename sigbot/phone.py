"""Панель на телефоне без настроек роутера: бесплатный туннель Cloudflare (cloudflared, без аккаунта).

Кнопка «📱 Панель на телефоне» в /menu запускает туннель и присылает ссылку с секретным ключом.
Без ключа по ссылке ничего не открывается. Ссылка меняется при каждом перезапуске бота, старая перестаёт работать.
Дома в одной Wi-Fi сети работает и прямая ссылка на компьютер. Бот только показывает графики: торговать через панель нельзя."""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import secrets
import shutil
import socket
import stat
from pathlib import Path

from . import settings

log = logging.getLogger(__name__)
BIN = Path("bin")
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
LHR_RE = re.compile(r"https://[a-z0-9-]+\.lhr\.life")
STATE: dict = {"url": None, "proc": None, "log": []}


def token(conn) -> str:
    """Ключ панели: из .env (DASHBOARD_TOKEN) или созданный один раз и сохранённый в базе."""
    t = os.environ.get("DASHBOARD_TOKEN") or settings.get(conn, "dashboard_token")
    if not t:
        t = secrets.token_urlsafe(16)
        settings.put(conn, "dashboard_token", t)
    return t


def lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))                 # пакет не уходит, просто выбирается сетевая карта
            return s.getsockname()[0]
    except OSError:
        return None


def lan_ips() -> list[str]:
    """Адреса компьютера в домашней сети. Через VPN основной адрес бывает «VPN-ным», поэтому берём все частные."""
    ips = []
    try:
        ips += [a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    ip = lan_ip()
    if ip:
        ips.append(ip)
    priv = lambda x: x.startswith("192.168.") or x.startswith("10.") or \
        (x.startswith("172.") and 16 <= int(x.split(".")[1]) <= 31)
    out = [x for x in dict.fromkeys(ips) if priv(x)]
    return sorted(out, key=lambda x: (not x.startswith("192.168."), x))[:3]


async def check(url: str, tok: str, tries: int = 12) -> tuple[bool, str]:
    """Открывается ли адрес с этого компьютера (новому адресу нужно до полуминуты). Второе значение: причина отказа."""
    import aiohttp
    why = ""
    for _ in range(tries):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{url}/api/status?token={tok}", timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        return True, ""
                    why = f"ответ {r.status}: {(await r.text())[:80]}"
        except Exception as e:
            why = f"{type(e).__name__}: {str(e)[:120]}"
        await asyncio.sleep(2.5)
    return False, why


def tunnel_errors(n: int = 3) -> list[str]:
    """Последние ошибки cloudflared: почему туннель не соединился с Cloudflare."""
    return [x for x in STATE.get("log", []) if " ERR " in x or "error" in x.lower()][-n:]


def _asset() -> tuple[str, str]:
    sysname, arch = platform.system().lower(), platform.machine().lower()
    a = "arm64" if arch in ("arm64", "aarch64") else "amd64"
    if sysname == "windows":
        return f"cloudflared-windows-{a}.exe", "cloudflared.exe"
    if sysname == "darwin":
        return f"cloudflared-darwin-{a}.tgz", "cloudflared"
    return f"cloudflared-linux-{a}", "cloudflared"


async def _binary(http) -> str:
    found = shutil.which("cloudflared")
    if found:
        return found
    asset, name = _asset()
    p = BIN / name
    if p.exists():
        return str(p)
    BIN.mkdir(exist_ok=True)
    data = await http.get_bytes(f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}")
    if not data:
        raise RuntimeError("не скачался cloudflared с GitHub")
    if asset.endswith(".tgz"):
        import io
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            data = t.extractfile("cloudflared").read()
    p.write_bytes(data)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


async def start(http, port: int, wait: float = 40) -> str | None:
    """Запускает туннель (если ещё не запущен) и возвращает адрес https://….trycloudflare.com."""
    pr = STATE.get("proc")
    if pr and pr.returncode is None and STATE.get("url"):
        return STATE["url"]
    exe = await _binary(http)
    # http2 вместо QUIC: UDP часто режут провайдеры и VPN, тогда адрес выдаётся, но не открывается
    pr = await asyncio.create_subprocess_exec(exe, "tunnel", "--no-autoupdate", "--protocol", "http2",
                                              "--url", f"http://127.0.0.1:{port}",
                                              stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    STATE.update(proc=pr, url=None, log=[])

    async def read():
        async for line in pr.stdout:
            text = line.decode(errors="replace").strip()
            STATE["log"] = (STATE.get("log", []) + [text])[-60:]
            m = URL_RE.search(text)
            if m and not STATE["url"]:
                STATE["url"] = m.group(0)
    asyncio.create_task(read())
    # ждём не только адрес, но и соединение с Cloudflare: если открыть адрес раньше, компьютер и телефон
    # запоминают «такого сайта нет» на несколько минут, и ссылка не открывается, даже когда уже работает
    for _ in range(int(wait * 2)):
        if pr.returncode is not None or (STATE["url"] and any("Registered tunnel connection" in x for x in STATE["log"])):
            break
        await asyncio.sleep(0.5)
    if STATE["url"]:
        await asyncio.sleep(8)
    return STATE["url"]


async def start_lhr(port: int, wait: float = 30) -> str | None:
    """Запасной туннель localhost.run через ssh, который есть в Windows 10 и 11. Ничего не скачивает."""
    pr = STATE.get("lhr")
    if pr and pr.returncode is None and STATE.get("lhr_url"):
        return STATE["lhr_url"]
    ssh = shutil.which("ssh")
    if not ssh:
        return None
    pr = await asyncio.create_subprocess_exec(
        ssh, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=" + os.devnull, "-o", "ServerAliveInterval=30",
        "-o", "ExitOnForwardFailure=yes", "-R", f"80:127.0.0.1:{port}", "nokey@localhost.run",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    STATE.update(lhr=pr, lhr_url=None)

    async def read():
        async for line in pr.stdout:
            text = line.decode(errors="replace").strip()
            STATE["log"] = (STATE.get("log", []) + ["lhr: " + text])[-60:]
            m = LHR_RE.search(text)
            if m and not STATE["lhr_url"]:
                STATE["lhr_url"] = m.group(0)
    asyncio.create_task(read())
    for _ in range(int(wait * 2)):
        if STATE["lhr_url"] or pr.returncode is not None:
            break
        await asyncio.sleep(0.5)
    return STATE["lhr_url"]


def stop():
    pr = STATE.get("lhr")
    if pr and pr.returncode is None:
        pr.terminate()
    STATE.update(lhr=None, lhr_url=None)
    pr = STATE.get("proc")
    if pr and pr.returncode is None:
        pr.terminate()
    STATE.update(proc=None, url=None)
